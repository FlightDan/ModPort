from __future__ import annotations

import json
import os
from pathlib import Path
import select
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from modport.evidence import atomic_json
from modport import rework_mcp


class ReworkMcpTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.session_dir = Path(self.temporary.name)
        self.session_path = self.session_dir / "session.json"
        if self._testMethodName == "test_eof_wait_is_bounded_by_session_deadline_and_reports_timeout":
            lifetime = 2
        elif self._testMethodName == "test_deadline_returns_mcp_tool_error":
            # Include interpreter startup; the deadline still expires during
            # this call and must publish cancellation, never a late success.
            lifetime = 2
        else:
            lifetime = 10
        self.write_session(deadline=time.time() + lifetime)
        environment = os.environ.copy()
        # Child processes must use the same tested installation, including
        # frozen-wheel validation rather than silently selecting editable src.
        python_path = str(Path(rework_mcp.__file__).resolve().parent.parent)
        if environment.get("PYTHONPATH"):
            python_path += os.pathsep + environment["PYTHONPATH"]
        environment["PYTHONPATH"] = python_path
        self.process = subprocess.Popen(
            [sys.executable, "-m", "modport.rework_mcp", "--session",
             str(self.session_path)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            bufsize=1,
            env=environment,
        )

    def tearDown(self):
        if self.process.poll() is None:
            assert self.process.stdin is not None
            self.process.stdin.close()
            try:
                self.process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=2)
        if self.process.stderr is not None:
            self.process.stderr.close()
        if self.process.stdout is not None:
            self.process.stdout.close()
        self.temporary.cleanup()

    def write_session(self, *, deadline: float):
        atomic_json(self.session_path, {
            "run_id": "run-17",
            "reviewer_execution_id": "reviewer:parallel-review:2",
            "deadline_epoch": deadline,
            "drain_pending_on_eof": True,
            "targets": [
                {"target_agent": "coder.alpha", "description": "Implementation owner"},
                {"target_agent": "author.beta", "description": "Document owner"},
            ],
        })

    def send(self, rpc_id, method, params=None):
        assert self.process.stdin is not None
        message = {"jsonrpc": "2.0", "id": rpc_id, "method": method}
        if params is not None:
            message["params"] = params
        self.process.stdin.write(json.dumps(message, ensure_ascii=False) + "\n")
        self.process.stdin.flush()

    def notify(self, method, params=None):
        assert self.process.stdin is not None
        message = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            message["params"] = params
        self.process.stdin.write(json.dumps(message, ensure_ascii=False) + "\n")
        self.process.stdin.flush()

    def receive(self, *, timeout=3):
        assert self.process.stdout is not None
        ready, _, _ = select.select([self.process.stdout], [], [], timeout)
        if not ready:
            error = ""
            if self.process.poll() is not None and self.process.stderr is not None:
                error = self.process.stderr.read()
            self.fail(f"timed out waiting for MCP response; stderr={error!r}")
        line = self.process.stdout.readline()
        self.assertTrue(line, "MCP server closed stdout")
        return json.loads(line)

    def wait_for_request(self, *, timeout=3):
        requests_dir = self.session_dir / "requests"
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            requests = list(requests_dir.glob("*.json")) if requests_dir.exists() else []
            requests = [path for path in requests if not path.name.endswith(".cancel.json")]
            if requests:
                self.assertEqual(len(requests), 1)
                return requests[0], json.loads(requests[0].read_text(encoding="utf-8"))
            time.sleep(0.02)
        self.fail("server did not publish a rework request")

    def initialize(self):
        self.send(1, "initialize", {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "test-client", "version": "1"},
        })
        response = self.receive()
        self.assertEqual(response["result"]["protocolVersion"], "2024-11-05")
        self.notify("notifications/initialized")

    def test_stdio_round_trip_lists_tools_and_returns_raw_target_response(self):
        self.initialize()
        self.send(2, "tools/list")
        listed = self.receive()
        self.assertEqual(
            [tool["name"] for tool in listed["result"]["tools"]],
            ["list_rework_targets", "request_rework"],
        )

        self.send(3, "tools/call", {
            "name": "list_rework_targets", "arguments": {}})
        targets = self.receive()["result"]
        self.assertEqual(
            [item["target_agent"] for item in targets["structuredContent"]["targets"]],
            ["coder.alpha", "author.beta"],
        )

        instructions = "Fix `Thing<T>`; keep commas, quotes \"exact\", and 中文."
        self.send(4, "tools/call", {"name": "request_rework", "arguments": {
            "target_agent": "coder.alpha", "instructions": instructions}})
        request_path, request = self.wait_for_request()
        self.assertEqual(set(request), {
            "request_id", "run_id", "reviewer_execution_id", "target_agent",
            "instructions", "created_at", "response_deadline_epoch",
        })
        self.assertEqual(request["run_id"], "run-17")
        self.assertEqual(request["reviewer_execution_id"], "reviewer:parallel-review:2")
        self.assertEqual(request["target_agent"], "coder.alpha")
        self.assertEqual(request["instructions"], instructions)
        self.assertGreater(request['response_deadline_epoch'], time.time())
        self.assertLessEqual(request['response_deadline_epoch'], time.time() + 1200)
        self.assertEqual(request_path.name, request["request_id"] + ".json")

        self.send(5, "ping")
        self.assertEqual(self.receive(), {"jsonrpc": "2.0", "id": 5, "result": {}})

        report = "Done. Kept {braces}, `ticks`,\nnewlines; and 中文 exactly!"
        atomic_json(self.session_dir / "responses" / request_path.name, {
            "request_id": request["request_id"],
            "status": "completed",
            "text": report,
        })
        response = self.receive()
        self.assertEqual(response["id"], 4)
        self.assertEqual(response["result"]["content"], [{"type": "text", "text": report}])
        self.assertNotIn("isError", response["result"])

    def test_cancel_notification_writes_marker_and_unblocks_call(self):
        self.initialize()
        self.send("waiting-call", "tools/call", {"name": "request_rework", "arguments": {
            "target_agent": "author.beta", "instructions": "Correct the cited claim."}})
        _, request = self.wait_for_request()
        self.notify("notifications/cancelled", {
            "requestId": "waiting-call", "reason": "reviewer changed direction"})
        response = self.receive()
        self.assertEqual(response["id"], "waiting-call")
        self.assertTrue(response["result"]["isError"])
        self.assertEqual(response["result"]["content"][0]["text"],
                         "reviewer changed direction")

        marker_path = self.session_dir / "requests" / (
            request["request_id"] + ".cancel.json")
        deadline = time.monotonic() + 2
        while not marker_path.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        marker = json.loads(marker_path.read_text(encoding="utf-8"))
        self.assertEqual(marker["request_id"], request["request_id"])
        self.assertEqual(marker["reason"], "reviewer changed direction")
        self.assertEqual(marker["run_id"], "run-17")
        self.assertEqual(marker["reviewer_execution_id"], "reviewer:parallel-review:2")

    def test_completion_and_cancellation_leave_consistent_terminal_artifacts(self):
        self.send(12, "tools/call", {"name": "request_rework", "arguments": {
            "target_agent": "coder.alpha", "instructions": "Resolve one race."}})
        request_path, request = self.wait_for_request()
        atomic_json(self.session_dir / "responses" / request_path.name, {
            "request_id": request["request_id"], "status": "completed", "text": "finished"})
        self.notify("notifications/cancelled", {"requestId": 12, "reason": "stop"})
        result = self.receive()["result"]
        marker = self.session_dir / "requests" / (request["request_id"] + ".cancel.json")
        time.sleep(0.2)
        if result.get("isError"):
            self.assertEqual(result["content"][0]["text"], "stop")
            self.assertTrue(marker.is_file())
        else:
            self.assertEqual(result["content"][0]["text"], "finished")
            self.assertFalse(marker.exists())

    def test_duplicate_active_rpc_id_does_not_replace_pending_call(self):
        call = {"name": "request_rework", "arguments": {
            "target_agent": "coder.alpha", "instructions": "Only once."}}
        self.send("duplicate", "tools/call", call)
        _, request = self.wait_for_request()
        self.send("duplicate", "tools/call", call)
        duplicate = self.receive()
        self.assertEqual(duplicate["error"]["code"], -32600)
        self.notify("notifications/cancelled", {"requestId": "duplicate"})
        cancelled = self.receive()["result"]
        self.assertTrue(cancelled["isError"])
        requests = [path for path in (self.session_dir / "requests").glob("*.json")
                    if not path.name.endswith(".cancel.json")]
        self.assertEqual(len(requests), 1)
        self.assertTrue((self.session_dir / "requests" /
                         (request["request_id"] + ".cancel.json")).is_file())

    def test_deadline_returns_mcp_tool_error(self):
        self.initialize()
        self.send(20, "tools/call", {"name": "request_rework", "arguments": {
            "target_agent": "coder.alpha", "instructions": "Wait for no response."}})
        _, request = self.wait_for_request()
        result = self.receive(timeout=3)["result"]
        self.assertTrue(result["isError"])
        self.assertIn("deadline exceeded", result["content"][0]["text"])
        marker = self.session_dir / "requests" / (request["request_id"] + ".cancel.json")
        self.assertEqual(json.loads(marker.read_text(encoding="utf-8"))["reason"],
                         result["content"][0]["text"])
        # A host response remains evidence, but cannot turn the expired call
        # into a second successful reply or cause an implicit retry.
        atomic_json(self.session_dir / "responses" / (request["request_id"] + ".json"), {
            "request_id": request["request_id"], "status": "completed", "text": "late result"})
        self.send(21, "ping")
        self.assertEqual(self.receive(), {"jsonrpc": "2.0", "id": 21, "result": {}})
        requests = [path for path in (self.session_dir / "requests").glob("*.json")
                    if not path.name.endswith(".cancel.json")]
        self.assertEqual(len(requests), 1)

    def test_single_response_wait_is_shorter_than_session_budget(self):
        session = rework_mcp.Session.load(self.session_path)
        server = rework_mcp.ReworkServer(session, poll_interval=0.01)
        pending = rework_mcp.PendingCall(24)
        started = time.monotonic()
        with patch.object(rework_mcp, "MAX_RESPONSE_WAIT_SECONDS", 0.12):
            result = server._request_rework(pending, {
                "target_agent": "coder.alpha", "instructions": "Resolve the issue."})
        self.assertLess(time.monotonic() - started, 1)
        self.assertTrue(result["isError"])
        self.assertIn("response wait deadline exceeded", result["content"][0]["text"])
        request = json.loads((session.requests_dir / (pending.request_id + ".json")).read_text())
        marker = json.loads((session.requests_dir / (pending.request_id + ".cancel.json")).read_text())
        self.assertEqual(marker["request_id"], request["request_id"])
        self.assertEqual(marker["reason"], result["content"][0]["text"])
        self.assertGreater(session.deadline_epoch - time.time(), 5)

    def test_v26_response_wait_uses_reviewer_deadline(self):
        document = json.loads(self.session_path.read_text())
        document.update(workflow_version=26, deadline_epoch=time.time() + 3600)
        atomic_json(self.session_path, document)
        session = rework_mcp.Session.load(self.session_path)
        server = rework_mcp.ReworkServer(session, poll_interval=0.01)
        pending = rework_mcp.PendingCall(42)
        results = []
        worker = threading.Thread(target=lambda: results.append(server._request_rework(
            pending, {"target_agent": "coder.alpha", "instructions": "Repair the source."})))
        worker.start()
        request_path, request = self.wait_for_request()
        self.assertGreater(request['response_deadline_epoch'], time.time() + 3000)
        self.assertAlmostEqual(session.deadline_epoch - rework_mcp.SETTLEMENT_ROOM_SECONDS,
                               request['response_deadline_epoch'], delta=1)
        atomic_json(session.responses_dir / request_path.name,
                    {"request_id": pending.request_id, "status": "completed", "text": "done"})
        worker.join(timeout=2)
        self.assertFalse(worker.is_alive())
        self.assertEqual([{"type": "text", "text": "done"}], results[0]["content"])

    def test_failed_response_keeps_raw_target_text_and_host_error(self):
        self.send(25, "tools/call", {"name": "request_rework", "arguments": {
            "target_agent": "coder.alpha", "instructions": "Attempt the revision."}})
        request_path, request = self.wait_for_request()
        raw = "Partial result: kept {braces},\nquotes \"as written\"."
        error = "Fresh verification failed: task :test returned 1."
        atomic_json(self.session_dir / "responses" / request_path.name, {
            "request_id": request["request_id"], "status": "failed",
            "text": raw, "error": error,
        })
        result = self.receive()["result"]
        self.assertTrue(result["isError"])
        self.assertEqual(result["content"], [
            {"type": "text", "text": raw},
            {"type": "text", "text": error},
        ])

    def test_unknown_target_is_rejected_without_creating_request(self):
        self.send(30, "tools/call", {"name": "request_rework", "arguments": {
            "target_agent": "intruder", "instructions": "Do work."}})
        response = self.receive()["result"]
        self.assertTrue(response["isError"])
        self.assertEqual(response["content"][0]["text"], "unknown rework target")
        requests = list((self.session_dir / "requests").glob("*.json"))
        self.assertEqual(requests, [])

    def test_closed_session_returns_mcp_tool_error(self):
        atomic_json(self.session_dir / "closed.json", {"reason": "review stage closed"})
        self.send(35, "tools/call", {"name": "request_rework", "arguments": {
            "target_agent": "coder.alpha", "instructions": "Too late."}})
        result = self.receive()["result"]
        self.assertTrue(result["isError"])
        self.assertEqual(result["content"][0]["text"], "review stage closed")
        self.assertEqual(list((self.session_dir / "requests").glob("*.json")), [])

    def test_eof_waits_for_published_request_to_finish(self):
        self.send(40, "tools/call", {"name": "request_rework", "arguments": {
            "target_agent": "coder.alpha", "instructions": "Remain pending."}})
        request_path, request = self.wait_for_request()
        started = time.monotonic()
        self.process.stdin.close()
        time.sleep(0.15)
        self.assertIsNone(self.process.poll())
        atomic_json(self.session_dir / "responses" / request_path.name, {
            "request_id": request["request_id"], "status": "completed",
            "text": "late but authoritative",
        })
        response = self.receive()
        self.assertEqual(response["id"], 40)
        self.assertEqual(response["result"]["content"][0]["text"],
                         "late but authoritative")
        self.process.wait(timeout=1)
        self.assertGreaterEqual(time.monotonic() - started, 0.15)

    def test_eof_wait_is_bounded_by_session_deadline_and_reports_timeout(self):
        self.send(45, "tools/call", {"name": "request_rework", "arguments": {
            "target_agent": "coder.alpha", "instructions": "Wait only until deadline."}})
        self.wait_for_request()
        started = time.monotonic()
        self.process.stdin.close()
        response = self.receive(timeout=3)
        self.assertEqual(response["id"], 45)
        self.assertTrue(response["result"]["isError"])
        self.assertIn("deadline exceeded", response["result"]["content"][0]["text"])
        _, request = self.wait_for_request()
        marker = self.session_dir / "requests" / (request["request_id"] + ".cancel.json")
        self.assertTrue(marker.is_file())
        self.process.wait(timeout=1)
        self.assertLess(time.monotonic() - started, 3)

    def test_legacy_session_eof_does_not_drain_pending_call(self):
        document = json.loads(self.session_path.read_text(encoding="utf-8"))
        document["drain_pending_on_eof"] = False
        atomic_json(self.session_path, document)
        self.send(50, "tools/call", {"name": "request_rework", "arguments": {
            "target_agent": "coder.alpha", "instructions": "Legacy shutdown."}})
        self.wait_for_request()
        started = time.monotonic()
        self.process.stdin.close()
        self.process.wait(timeout=1)
        self.assertLess(time.monotonic() - started, 1)


if __name__ == "__main__":
    unittest.main()
