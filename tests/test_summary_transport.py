from __future__ import annotations

import json
import queue
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from modport import opencode_runtime
from modport import summary_transport as transport
from modport.prompt_compressor import PromptCompressor, PromptCompressionError, SummaryRequest


class FakeOpenCodeServer:
    version = "1.18.32"
    executable_sha256 = "a" * 64

    def __init__(self, *, response=None, delay=0, require_error=None, events=True,
                 progress=False, delta=None):
        self.response = response or {
            "info": {"role": "assistant", "providerID": "openai", "modelID": "gpt-6-luna",
                     "variant": "max", "finish": "stop",
                     "tokens": {"input": 9, "output": 7}},
            "parts": [{"type": "text", "text": '{"summary":"fixed"}'}],
        }
        self.delay = delay
        self.require_error = require_error
        self.events_enabled = events
        self.progress = progress
        self.delta = delta
        self.closed = False
        self.created_config = None
        self.current_session = None
        self.event_queue = queue.Queue()
        self.event_queue.put({"id": "event-1", "type": "server.connected", "properties": {}})
        self.sent = None
        self.deleted = []
        self.aborted = []
        self.required = []
        self.providers_value = {
            "all": [{"id": "openai", "models": {
                "gpt-6-luna": {"limit": {"context": 272000, "output": 32000},
                               "variants": {"max": {"reasoningEffort": "max"}}},
            }}],
            "connected": ["openai"], "default": {},
        }
        self.providers_responses = None
        self.provider_deadlines = []

    def providers(self, *, cwd, deadline=None):
        self.provider_deadlines.append(deadline)
        if self.providers_responses is not None:
            value = self.providers_responses.pop(0)
            if isinstance(value, BaseException):
                raise value
            return value
        return self.providers_value

    def tool_ids(self, *, cwd, deadline=None):
        return ["read", "bash", "mcp_docs_search"]

    def require_model(self, *, cwd, model, variant=None, deadline=None):
        self.required.append((model, variant))
        if self.require_error:
            raise RuntimeError(self.require_error)
        return {"providerID": "openai", "modelID": "gpt-6-luna", "variant": variant}

    def create_session(self, **kwargs):
        self.current_session = "ses-summary"
        self.create_args = kwargs
        return {"id": self.current_session}

    def events(self, *, cwd, deadline, stop_event=None):
        while (not self.closed and not (stop_event and stop_event.is_set())
               and time.monotonic() < deadline):
            try:
                yield self.event_queue.get(timeout=0.02)
            except queue.Empty:
                continue

    def send_message(self, session_id, text, **kwargs):
        self.sent = (session_id, text, kwargs)
        message_id = kwargs.get("message_id", "msg-assistant")
        assistant_id = "msg-summary-assistant"
        if self.events_enabled:
            self.event_queue.put({"id": "event-2", "type": "message.updated", "properties": {
                "sessionID": session_id,
                "info": {"id": assistant_id, "parentID": message_id,
                         "role": "assistant"},
            }})
            text_part = self.response.get("parts", [{}])[0]
            if self.delta is not None:
                self.event_queue.put({"id": "event-3", "type": "message.part.delta", "properties": {
                    "sessionID": session_id, "messageID": assistant_id,
                    "partID": "part-text", "field": "text", "delta": self.delta,
                }})
            elif text_part.get("type") == "text":
                self.event_queue.put({"id": "event-3", "type": "message.part.updated", "properties": {
                    "sessionID": session_id,
                    "part": {"sessionID": session_id, "messageID": assistant_id,
                             "id": "part-text", "type": "text", "text": text_part.get("text", "")},
                }})
        if self.delay:
            end = time.monotonic() + self.delay
            while self.progress and time.monotonic() < end:
                self.event_queue.put({"id": "event-progress", "type": "message.updated", "properties": {
                    "sessionID": session_id,
                    "info": {"id": assistant_id, "parentID": message_id,
                             "role": "assistant"},
                }})
                time.sleep(0.01)
            time.sleep(max(0, end - time.monotonic()))
        return self.response

    def abort_session(self, session_id, *, cwd=None, deadline=None):
        self.aborted.append(session_id)
        return True

    def delete_session(self, session_id, *, cwd=None, deadline=None):
        self.deleted.append(session_id)
        return True

    def close(self, **kwargs):
        self.closed = True
        return {"classification": "host_terminated", "returncode": -9,
                "cleanup_confirmed": True}


class SummaryTransportTests(unittest.TestCase):
    def test_scope_close_wakes_blocked_summary_event_reader(self):
        class BlockingEventServer(FakeOpenCodeServer):
            def __init__(self):
                super().__init__()
                self.reader_started = threading.Event()
                self.stop_handle = None

            def events(self, *, cwd, deadline, stop_event=None):
                self.stop_handle = stop_event
                self.reader_started.set()
                stop_event.wait(timeout=5)
                if False:
                    yield None

        server = BlockingEventServer()
        self._scope(server)
        scope = transport.OpenCodeSummaryScope(
            command=self.command, root=self.root, worktree=self.root)
        scope._start_event_reader()
        self.assertTrue(server.reader_started.wait(timeout=1))
        scope.close()
        self.assertTrue(server.stop_handle.is_set())
        self.assertIsNone(scope._events_thread)

    def test_unconfirmed_cleanup_fails_and_keeps_only_allowlisted_record(self):
        server = FakeOpenCodeServer()
        self._scope(server)
        scope = transport.OpenCodeSummaryScope(
            command=self.command, root=self.root, worktree=self.root)
        scope._ensure_server()
        temporary = Path(scope._temporary.name)
        (temporary / 'private-config').write_text('fixture-secret')
        with patch.object(server, 'close', return_value={
                'classification': 'unknown', 'cleanup_confirmed': False,
                'detail': 'fixture-secret', 'target_pid': 900001}):
            with self.assertRaises(transport.SummaryTransportError) as caught:
                scope.close()
        self.assertEqual('cleanup_unconfirmed', caught.exception.code)
        self.assertFalse(temporary.exists())
        records = list((self.root / 'logs').glob('summary-cleanup-*.json'))
        self.assertEqual(1, len(records))
        self.assertNotIn('fixture-secret', records[0].read_text())
        record = json.loads(records[0].read_text())
        self.assertFalse(record['cleanup_confirmed'])
        self.assertEqual(900001, record['server']['target_pid'])

    def test_failed_startup_cleanup_keeps_allowlisted_record(self):
        with patch.object(opencode_runtime.OpenCodeServer, 'start',
                          side_effect=opencode_runtime.OpenCodeCleanupError({
                              'cleanup_confirmed': False, 'target_pid': 900003,
                              'detail': 'fixture-secret'})):
            scope = transport.OpenCodeSummaryScope(
                command=self.command, root=self.root, worktree=self.root)
            with self.assertRaises(transport.SummaryTransportError) as caught:
                scope._ensure_server()
        self.assertEqual('cleanup_unconfirmed', caught.exception.code)
        self.assertIsNone(scope._temporary)
        records = list((self.root / 'logs').glob('summary-cleanup-*.json'))
        self.assertEqual(1, len(records))
        self.assertEqual(900003, json.loads(records[0].read_text())['server']['target_pid'])
        self.assertNotIn('fixture-secret', records[0].read_text())

    def test_provider_auth_failure_is_classified_without_provider_message(self):
        server = FakeOpenCodeServer()
        error = opencode_runtime.OpenCodeResponseError(
            {'name': 'ProviderAuthError', 'data': {'message': 'private credential text'}},
            {'info': {}})
        with patch.object(server, 'send_message', side_effect=error):
            with self._scope(server) as scope:
                with self.assertRaises(transport.SummaryTransportError) as caught:
                    scope.summarize(SummaryRequest(
                        'historical fact', 'gpt-6-luna', 'max', 100, 'stage',
                        timeout=5, idle_timeout=1, output_byte_limit=200),
                        log_path=self.root / 'auth.json')
        self.assertEqual('auth_failed', caught.exception.code)
        self.assertNotIn('private credential text', (self.root / 'auth.json').read_text())

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.command = type("Command", (), {"run_id": "run", "task_id": "task", "stage_id": "stage"})()

    def _scope(self, server):
        runtime_start = patch.object(opencode_runtime.OpenCodeServer, "start", return_value=server)
        runtime_start.start()
        self.addCleanup(runtime_start.stop)
        return transport.compression_scope(command=self.command, root=self.root, worktree=self.root)

    def test_summary_uses_live_opencode_catalog_and_preserves_variant(self):
        server = FakeOpenCodeServer()
        with self._scope(server) as scope:
            profile = scope.model_profile("gpt-6-luna")
            self.assertEqual("openai/gpt-6-luna", profile.model)
            self.assertEqual(272000, profile.context_window)
            self.assertIn("max", profile.variants)
            request = SummaryRequest(
                "private source", "gpt-6-luna", "max", 100, "stage",
                timeout=5, idle_timeout=1, output_byte_limit=100)
            audit_path = self.root / "summary.json"
            result = scope.summarize(request, log_path=audit_path)

        self.assertEqual('{"summary":"fixed"}', result)
        session_id, text, sent = server.sent
        self.assertEqual("ses-summary", session_id)
        self.assertEqual(request.prompt, text)
        self.assertEqual("openai/gpt-6-luna", sent["model"])
        self.assertEqual("max", sent["variant"])
        self.assertEqual({"read": False, "bash": False, "mcp_docs_search": False}, sent["tools"])
        self.assertEqual(("openai/gpt-6-luna", "max"), server.required[0])
        self.assertTrue(server.deleted)
        audit = __import__("json").loads(audit_path.read_text())
        self.assertEqual("opencode_http", audit["transport"])
        self.assertEqual("completed", audit["status"])
        self.assertEqual({"input_tokens": 9, "output_tokens": 7}, audit["usage"])
        self.assertNotIn("private source", audit_path.read_text())

    def test_model_profile_requeries_missing_model_once_with_same_deadline(self):
        server = FakeOpenCodeServer()
        server.providers_responses = [
            {"all": [{"id": "openai", "models": {"gpt-6-luna": {
                "limit": {"context": 272000, "output": 32000}}}}]},
            {"all": [{"id": "openai", "models": {"gpt-6-sol": {
                "limit": {"context": 196000, "output": 16000}}}}]},
        ]
        with patch.object(transport.time, "sleep") as pause:
            with self._scope(server) as scope:
                profile = scope.model_profile("gpt-6-sol")
                diagnostic = scope.model_catalog_diagnostics()
        pause.assert_called_once_with(0.5)

        self.assertEqual("openai/gpt-6-sol", profile.model)
        self.assertEqual(196000, profile.context_window)
        self.assertEqual(2, len(server.provider_deadlines))
        self.assertEqual(server.provider_deadlines[0], server.provider_deadlines[1])
        queries = diagnostic["queries"]
        self.assertEqual([False, True], [query["target_present"] for query in queries])
        self.assertEqual([1, 2], [query["attempt"] for query in queries])
        self.assertNotEqual(queries[0]["catalog_sha256"], queries[1]["catalog_sha256"])
        self.assertEqual("OpenCode GET /provider", queries[0]["catalog_identity"]["source"])
        self.assertNotIn("options", json.dumps(diagnostic))

    def test_catalog_diagnostics_locate_target_provider_change_without_secrets(self):
        server = FakeOpenCodeServer()
        first = {"all": [
            {"id": "openai", "models": {"gpt-6-luna": {
                "limit": {"context": 272000, "output": 32000},
                "options": {"apiKey": "never-persist-this"}}}},
            {"id": "other", "models": {"old": {"limit": {"context": 1000}}}},
        ]}
        unrelated_change = {"all": [first["all"][0],
                                    {"id": "other", "models": {"new": {"limit": {"context": 1000}}}}]}
        target_change = {"all": [
            {"id": "openai", "models": {"gpt-6-sol": {"limit": {"context": 196000}}}},
            unrelated_change["all"][1],
        ]}
        with self._scope(server) as scope:
            for attempt, catalog in enumerate((first, unrelated_change, target_change), 1):
                scope._record_catalog_query(catalog, target_model="openai/gpt-6-sol",
                                            attempt=attempt, status="ok")
            queries = scope.model_catalog_diagnostics()["queries"]

        self.assertNotEqual(queries[0]["catalog_sha256"], queries[1]["catalog_sha256"])
        self.assertEqual(queries[0]["target_provider_catalog_sha256"],
                         queries[1]["target_provider_catalog_sha256"])
        self.assertNotEqual(queries[1]["target_provider_catalog_sha256"],
                            queries[2]["target_provider_catalog_sha256"])
        self.assertEqual([1, 1, 1], [query["target_provider_model_count"] for query in queries])
        self.assertEqual([False, False, True], [query["target_present"] for query in queries])
        self.assertNotIn("never-persist-this", json.dumps(queries))

    def test_model_profile_waits_for_third_catalog_without_changing_model(self):
        server = FakeOpenCodeServer()
        missing = {"all": [{"id": "openai", "models": {"gpt-6-luna": {
            "limit": {"context": 272000, "output": 32000}}}}]}
        present = {"all": [{"id": "openai", "models": {"gpt-6-sol": {
            "limit": {"context": 196000, "output": 16000},
            "variants": {"high": {"reasoningEffort": "high"}}}}}]}
        server.providers_responses = [missing, missing, present]
        with patch.object(transport.time, "sleep") as pause:
            with self._scope(server) as scope:
                profile = scope.model_profile("gpt-6-sol")
                diagnostic = scope.model_catalog_diagnostics()
        self.assertEqual(196000, profile.context_window)
        self.assertEqual("openai/gpt-6-sol", profile.model)
        self.assertEqual([0.5, 1.0], [call.args[0] for call in pause.call_args_list])
        self.assertEqual([server.provider_deadlines[0]] * 3, server.provider_deadlines)
        self.assertEqual([False, False, True],
                         [query["target_present"] for query in diagnostic["queries"]])

    def test_compression_fails_clearly_and_persists_sanitized_catalog_queries(self):
        server = FakeOpenCodeServer()
        missing = {"all": [{"id": "openai", "models": {"gpt-6-luna": {
            "limit": {"context": 272000, "output": 32000},
            "options": {"apiKey": "never-persist-this"},
        }}}]}
        server.providers_responses = [missing, missing, missing]
        command = type("Command", (), {
            "run_id": "run", "task_id": "task", "stage_id": "migration_plan",
            "command_id": "migration-plan-command", "options": {},
        })()
        with patch.object(opencode_runtime.OpenCodeServer, "start", return_value=server), patch.object(transport.time, "sleep"):
            compressor = PromptCompressor.from_environment()
            with self.assertRaisesRegex(PromptCompressionError, "absent.*after 3 queries") as caught:
                compressor.compress("frozen migration plan prompt", model="gpt-6-sol", command=command,
                                    root=self.root, worktree=self.root)

        self.assertIn("model-catalog-diagnostics.json", str(caught.exception))
        path = (self.root / "artifacts" / "executions" / command.command_id / "prompt-compression"
                / "model-catalog-diagnostics.json")
        diagnostic_text = path.read_text()
        diagnostic = json.loads(diagnostic_text)
        self.assertEqual(["openai/gpt-6-sol"] * 3,
                         [query["target_model"] for query in diagnostic["queries"]])
        self.assertEqual([False] * 3, [query["target_present"] for query in diagnostic["queries"]])
        self.assertNotIn("never-persist-this", diagnostic_text)
        self.assertNotIn('"options"', diagnostic_text)

    def test_catalog_requery_transport_failure_is_a_blocked_compression_error(self):
        server = FakeOpenCodeServer()
        server.providers_responses = [
            {"all": [{"id": "openai", "models": {"gpt-6-luna": {
                "limit": {"context": 272000, "output": 32000}}}}]},
            RuntimeError("private provider configuration"),
        ]
        command = type("Command", (), {
            "run_id": "run", "task_id": "task", "stage_id": "migration_plan",
            "command_id": "migration-plan-command", "options": {},
        })()
        with patch.object(opencode_runtime.OpenCodeServer, "start", return_value=server), patch.object(transport.time, "sleep"):
            compressor = PromptCompressor.from_environment()
            with self.assertRaisesRegex(PromptCompressionError, "requery failed.*model_catalog_unavailable") as caught:
                compressor.compress("frozen migration plan prompt", model="gpt-6-sol", command=command,
                                    root=self.root, worktree=self.root)

        self.assertNotIn("private provider configuration", str(caught.exception))
        path = (self.root / "artifacts" / "executions" / command.command_id / "prompt-compression"
                / "model-catalog-diagnostics.json")
        diagnostic = json.loads(path.read_text())
        self.assertEqual(["ok", "unavailable"],
                         [query["status"] for query in diagnostic["queries"]])

    def test_large_prompt_uses_http_body_and_not_a_cli_argument(self):
        server = FakeOpenCodeServer()
        original = "history-record " * 20000
        request = SummaryRequest(original, "gpt-6-luna", "max", 100, "stage",
                                 timeout=5, idle_timeout=1, output_byte_limit=100)
        with self._scope(server) as scope:
            scope.summarize(request, log_path=self.root / "summary.json")
        self.assertEqual(request.prompt, server.sent[1])
        self.assertGreater(len(server.sent[1].encode()), 128 * 1024)

    def test_missing_provider_or_variant_fails_closed_and_writes_sanitized_evidence(self):
        server = FakeOpenCodeServer(require_error="private key not configured for OpenAI")
        path = self.root / "summary.json"
        request = SummaryRequest("private source", "gpt-6-luna", "max", 100, "stage",
                                 timeout=5, idle_timeout=1, output_byte_limit=100)
        with self._scope(server) as scope:
            with self.assertRaisesRegex(transport.SummaryTransportError, "provider, model") as result:
                scope.summarize(request, log_path=path)
        self.assertNotIn("private key", str(result.exception))
        audit_text = path.read_text()
        self.assertIn('"error_code": "model_unavailable"', audit_text)
        self.assertNotIn("private key", audit_text)
        self.assertNotIn("private source", audit_text)

    def test_tool_attempt_and_identity_mismatch_are_rejected(self):
        cases = [
            ({"info": {"role": "assistant", "providerID": "openai", "modelID": "gpt-6-luna",
                       "variant": "max", "finish": "stop"},
              "parts": [{"type": "tool", "callID": "hidden"}]}, "tool use"),
            ({"info": {"role": "assistant", "providerID": "openai", "modelID": "gpt-6-sol",
                       "variant": "max", "finish": "stop"},
              "parts": [{"type": "text", "text": "wrong model"}]}, "identity changed"),
        ]
        for response, expected in cases:
            with self.subTest(expected=expected):
                server = FakeOpenCodeServer(response=response)
                request = SummaryRequest("private source", "gpt-6-luna", "max", 100, "stage",
                                         timeout=5, idle_timeout=1, output_byte_limit=100)
                with self._scope(server) as scope:
                    with self.assertRaisesRegex(transport.SummaryTransportError, expected):
                        scope.summarize(request, log_path=self.root / "summary.json")

    def test_idle_and_output_limits_abort_the_session(self):
        request = SummaryRequest("private source", "gpt-6-luna", "max", 100, "stage",
                                 timeout=5, idle_timeout=0.05, output_byte_limit=100)
        server = FakeOpenCodeServer(delay=0.2, events=False)
        with self._scope(server) as scope:
            with self.assertRaisesRegex(transport.SummaryTransportError, "idle timed out"):
                scope.summarize(request, log_path=self.root / "idle.json")
        self.assertTrue(server.aborted)
        self.assertIn('"error_code": "stream_idle"', (self.root / "idle.json").read_text())

        response = {"info": {"role": "assistant", "providerID": "openai", "modelID": "gpt-6-luna",
                             "variant": "max", "finish": "stop"},
                    "parts": [{"type": "text", "text": "x" * 6}]}
        server = FakeOpenCodeServer(response=response)
        small = SummaryRequest("private source", "gpt-6-luna", "max", 100, "stage",
                               timeout=5, idle_timeout=1, output_byte_limit=5)
        with self._scope(server) as scope:
            with self.assertRaisesRegex(transport.SummaryTransportError, "byte ceiling"):
                scope.summarize(small, log_path=self.root / "limit.json")
        self.assertIn('"error_code": "output_limit"', (self.root / "limit.json").read_text())

    def test_progress_events_reset_idle_and_text_deltas_enforce_output_ceiling(self):
        request = SummaryRequest("private source", "gpt-6-luna", "max", 100, "stage",
                                 timeout=5, idle_timeout=0.05, output_byte_limit=100)
        server = FakeOpenCodeServer(delay=0.2, progress=True)
        with self._scope(server) as scope:
            result = scope.summarize(request, log_path=self.root / "progress.json")
        self.assertEqual('{"summary":"fixed"}', result)

        response = {"info": {"role": "assistant", "providerID": "openai", "modelID": "gpt-6-luna",
                             "variant": "max", "finish": "stop"},
                    "parts": [{"type": "text", "text": "ok"}]}
        server = FakeOpenCodeServer(response=response, delta="x" * 6, delay=0.1)
        small = SummaryRequest("private source", "gpt-6-luna", "max", 100, "stage",
                               timeout=5, idle_timeout=1, output_byte_limit=5)
        with self._scope(server) as scope:
            with self.assertRaisesRegex(transport.SummaryTransportError, "byte ceiling"):
                scope.summarize(small, log_path=self.root / "delta-limit.json")
        self.assertTrue(server.aborted)
        self.assertIn('"error_code": "output_limit"',
                      (self.root / "delta-limit.json").read_text())

    def test_assistant_deltas_before_message_update_refresh_idle_timeout(self):
        class EarlyDeltaServer(FakeOpenCodeServer):
            def send_message(self, session_id, text, **kwargs):
                self.sent = (session_id, text, kwargs)
                for number in range(4):
                    self.event_queue.put({
                        "id": f"early-delta-{number}", "type": "message.part.delta",
                        "properties": {"sessionID": session_id,
                                       "messageID": "msg-early-assistant",
                                       "partID": "part-text", "field": "text",
                                       "delta": "x"},
                    })
                    time.sleep(0.03)
                return self.response

        server = EarlyDeltaServer()
        request = SummaryRequest("private source", "gpt-6-luna", "max", 100,
                                 "stage", timeout=5, idle_timeout=0.07,
                                 output_byte_limit=100)
        with self._scope(server) as scope:
            result = scope.summarize(request, log_path=self.root / "early-delta.json")
        self.assertEqual('{"summary":"fixed"}', result)
        self.assertFalse(server.aborted)


if __name__ == "__main__":
    unittest.main()
