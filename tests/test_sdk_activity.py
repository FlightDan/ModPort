"""Real loopback OpenCode wire capture and explicit activity boundaries."""
from __future__ import annotations

from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from modport.execution_budget import current_sdk_context, execution_budget
from modport.opencode_agent import _TurnEventReader
from modport.opencode_runtime import OpenCodeHTTPError, OpenCodeServer
from modport.telemetry import (OpenCodeActivity, report_sdk_byte_count,
                              sdk_activity_wait)
from test_sdk_observability_integration import Context


def event(kind, **properties):
    return {"type": kind, "properties": {"sessionID": "ses-local", **properties}}


def wire_events():
    rows = [event("server.connected"), event("server.heartbeat"),
        event("message.updated", info={"id": "msg-user", "role": "user"}),
        event("message.part.updated", part={"id": "user-text", "messageID": "msg-user",
            "type": "text", "text": "private-prompt"}),
        event("message.updated", info={"id": "msg-assistant", "role": "assistant"}),
        event("message.part.updated", part={"id": "assistant-text", "messageID": "msg-assistant",
            "type": "text", "text": "private-response"})]
    for status in ("pending", "running", "completed", "completed"):
        rows.append(event("message.part.updated", part={"id": "tool-part", "messageID": "msg-assistant",
            "type": "tool", "tool": "read", "state": {"status": status,
                "input": {"private": "private-tool-input"}, "output": "private-tool-output"}}))
    rows.extend([event("message.updated", info={"id": "msg-assistant", "role": "assistant",
        "time": {"completed": 1}}), event("session.idle")])
    return b"".join(b"data: " + json.dumps(row).encode("utf-8") + b"\n\n" for row in rows)


@contextmanager
def loopback_server(test, *, status=200):
    wire = wire_events()
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", str(len(wire)))
            self.end_headers()
            self.wfile.write(wire)
            self.wfile.flush()
        def do_POST(self):
            self.rfile.read(int(self.headers["Content-Length"]))
            body = json.dumps({"info": {"role": "assistant", "id": "msg-assistant"},
                               "parts": [{"type": "text", "text": "private-response"}]}).encode()
            self.send_response(status)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
    try:
        listener = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    except PermissionError:
        test.skipTest("loopback socket binding unavailable in this namespace")
    serving = threading.Thread(target=listener.serve_forever, daemon=True)
    serving.start()
    server = object.__new__(OpenCodeServer)
    server.base_url = "http://127.0.0.1:" + str(listener.server_port)
    server.env = {}
    server.cwd = str(Path.cwd())
    server._session_directories = {}
    try:
        yield server, wire
    finally:
        listener.shutdown()
        listener.server_close()
        serving.join(2)


class SDKActivityTests(unittest.TestCase):
    def test_real_http_event_reader_captures_thread_context_and_actual_lifecycle(self):
        context = Context()
        with loopback_server(self) as (server, wire), tempfile.TemporaryDirectory() as directory:
            with execution_budget(context), patch.object(context.activity, "wait", wraps=context.activity.wait) as wait:
                reader = _TurnEventReader(server, cwd=Path(directory), session_id="ses-local",
                    message_id="msg-user", deadline=time.monotonic() + 3)
                try:
                    reader.start()
                    response = server.send_message("ses-local", "private-prompt", cwd=Path(directory),
                        message_id="msg-user", deadline=time.monotonic() + 2)
                    reader.expect_final("msg-assistant")
                    reader.wait_for_final("msg-assistant")
                finally:
                    reader.close()
                self.assertEqual(response["parts"][0]["text"], "private-response")
                wait.assert_called_once_with("tool_response", target="tool-part")
            snapshot = context.activity.snapshot()
            metrics = snapshot["metrics"]
            self.assertEqual(metrics["stdout_bytes"]["count"], len(wire))
            self.assertEqual(metrics["model_requests"]["count"], 1)
            self.assertEqual(metrics["model_events"]["count"], 6)
            self.assertEqual(metrics["model_text"]["count"], 1)
            self.assertEqual(metrics["tool_requests"]["count"], 1)
            self.assertEqual(metrics["tool_responses"]["count"], 1)
            self.assertNotIn("progress", metrics)
            self.assertEqual(snapshot["tails"], {})
            self.assertNotIn("private-", str(snapshot))
            self.assertIsNone(current_sdk_context())

    def test_http_authentication_failure_is_preserved_without_normal_wait(self):
        context = Context()
        with loopback_server(self, status=401) as (server, _):
            with execution_budget(context), patch.object(context.activity, "wait", wraps=context.activity.wait) as wait:
                with self.assertRaises(OpenCodeHTTPError) as failure:
                    server.send_message("ses-local", "private-prompt", cwd=Path.cwd(),
                                        deadline=time.monotonic() + 2)
                self.assertEqual(failure.exception.status, 401)
                wait.assert_not_called()
        metrics = context.activity.snapshot()["metrics"]
        self.assertEqual(metrics["model_requests"]["count"], 1)
        self.assertNotIn("model_events", metrics)

    def test_native_session_tools_and_measured_byte_counts_are_not_progress(self):
        context = Context()
        tracker = OpenCodeActivity()
        with execution_budget(context):
            report_sdk_byte_count("stdout", 23)
            for status in ("running", "error", "error"):
                tracker.observe({"type": "message.part.updated", "properties": {"part": {
                    "sessionID": "ses-native-child", "id": "native-tool", "type": "tool",
                    "state": {"status": status}}}})
            tracker.close()
        metrics = context.activity.snapshot()["metrics"]
        self.assertEqual(metrics["stdout_bytes"]["count"], 23)
        self.assertEqual(metrics["tool_requests"]["count"], 1)
        self.assertEqual(metrics["tool_responses"]["count"], 1)
        self.assertNotIn("progress", metrics)

    def test_wait_telemetry_failure_preserves_body_error_and_context_nesting(self):
        outer, inner = Context(), Context()
        with execution_budget(outer):
            with execution_budget(inner):
                self.assertIs(current_sdk_context(), inner)
            self.assertIs(current_sdk_context(), outer)
            with patch.object(outer.activity, "wait", side_effect=OSError("private-error")):
                with self.assertRaisesRegex(ValueError, "budget identity"):
                    with sdk_activity_wait("tool_response"):
                        raise ValueError("budget identity")
        self.assertIsNone(current_sdk_context())

    def test_reconnect_keeps_tool_identity_and_does_not_invent_another_request(self):
        context = Context()
        tracker = OpenCodeActivity()
        def tool(status):
            return event("message.part.updated", part={"id": "reconnected-tool", "type": "tool",
                "state": {"status": status}})
        with execution_budget(context):
            tracker.observe(tool("running"))
            tracker.close()
            tracker.observe(tool("running"))
            tracker.observe(tool("completed"))
            tracker.close()
            tracker.observe(tool("running"))
            tracker.observe(tool("completed"))
            tracker.close()
        metrics = context.activity.snapshot()["metrics"]
        self.assertEqual(metrics["tool_requests"]["count"], 1)
        self.assertEqual(metrics["tool_responses"]["count"], 1)
        self.assertEqual(metrics["model_events"]["count"], 2)


if __name__ == "__main__":
    unittest.main()
