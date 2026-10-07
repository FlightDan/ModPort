"""Current provider transport admission, cumulative usage and recovery evidence."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import io
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from modport.models import Budget, MigrationRequest
from modport.opencode_runtime import OpenCodeServer
from modport.token_budget import (
    TokenBudgetExceeded, admit_token_call, bind_token_budget,
    initialize_token_budget, read_token_budget, record_token_message,
)


def assistant(session, message, parent='msg_user', total=None):
    tokens = {'input': 10, 'output': 3, 'reasoning': 2,
              'cache': {'read': 7, 'write': 1}}
    if total is not None:
        tokens['total'] = total
    return {'info': {'id': message, 'sessionID': session, 'role': 'assistant',
                     'parentID': parent, 'providerID': 'openai', 'modelID': 'model',
                     'time': {'completed': 1}, 'tokens': tokens}, 'parts': []}


class TokenBudgetTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.set_limit(50)

    def set_limit(self, limit):
        (self.root / 'run.json').write_text(json.dumps({'request': {'budget': {'max_tokens': limit}}}))

    def server(self, responses):
        # Execute the production HTTP producer/consumer methods with only the
        # HTTP I/O replaced. There is no alternate fixture accounting path.
        server = object.__new__(OpenCodeServer)
        server._session_directories = {'ses_parent': self.root}
        requests = []
        def request(method, path, **kwargs):
            requests.append((method, path, kwargs))
            if method == 'POST' and path.endswith('/message'):
                return responses['ses_parent'][-1]
            if method == 'GET' and path.endswith('/message'):
                return responses[path.split('/')[2]]
            if path.endswith('/children'):
                return [{'id': 'ses_child'}] if path.split('/')[2] == 'ses_parent' and 'ses_child' in responses else []
            if path.endswith('/abort'):
                return True
            raise AssertionError(path)
        server._request = request
        bind_token_budget(server, self.root)
        return server, requests

    def test_budget_roundtrip_and_frozen_limit(self):
        request = MigrationRequest('mod', 'repo', '1.20.1', '1.21.1', budget=Budget(max_tokens=123))
        self.assertEqual(MigrationRequest.from_mapping(request.to_dict()).budget.max_tokens, 123)
        for value in (-1, True, 1.5, '20'):
            with self.assertRaises(ValueError):
                replace(request.budget, max_tokens=value).validate()
        initialize_token_budget(self.root, 50)
        with self.assertRaisesRegex(ValueError, 'frozen'):
            initialize_token_budget(self.root, 51)

    def test_parent_and_child_transport_usage_blocks_next_http_post(self):
        self.set_limit(40)
        responses = {'ses_parent': [assistant('ses_parent', 'msg_parent')],
                     'ses_child': [assistant('ses_child', 'msg_child')]}
        server, requests = self.server(responses)
        server.send_message('ses_parent', 'work', cwd=self.root, message_id='msg_user')
        snapshot = read_token_budget(self.root)
        self.assertEqual(snapshot['used_tokens'], 46)
        self.assertEqual(snapshot['overshoot_tokens'], 6)
        self.assertTrue(snapshot['usage_complete'])
        self.assertFalse(snapshot['hard_stream_cap'])
        count = len(requests)
        with self.assertRaises(TokenBudgetExceeded):
            server.send_message('ses_parent', 'more', cwd=self.root, message_id='msg_next')
        self.assertEqual(len(requests), count)
        # Reads and replay cannot charge either the parent or child twice.
        server.messages('ses_parent', cwd=self.root)
        server.messages('ses_child', cwd=self.root)
        self.assertEqual(read_token_budget(self.root)['used_tokens'], 46)

    def test_provider_total_is_authoritative_and_replay_is_concurrent_safe(self):
        info = assistant('ses_parent', 'msg_a', total=20)['info']
        initialize_token_budget(self.root, 50)
        with ThreadPoolExecutor(max_workers=6) as pool:
            list(pool.map(lambda _: record_token_message(self.root, info), range(20)))
        self.assertEqual(read_token_budget(self.root)['used_tokens'], 20)
        older = {**info, 'tokens': {'input': 1}, 'time': {}}
        record_token_message(self.root, older)
        self.assertEqual(read_token_budget(self.root)['used_tokens'], 20)
        self.assertTrue(read_token_budget(self.root)['usage_complete'])

    def test_interrupted_call_remains_unknown_until_its_usage_is_read_on_recovery(self):
        server, _ = self.server({'ses_parent': []})
        def broken(*args, **kwargs):
            raise TimeoutError('lost provider response')
        server._request = broken
        with self.assertRaises(TimeoutError):
            server.send_message('ses_parent', 'work', cwd=self.root, message_id='msg_user')
        snapshot = read_token_budget(self.root)
        self.assertFalse(snapshot['usage_complete'])
        self.assertEqual(snapshot['unknown_calls'], 1)
        self.assertTrue(snapshot['coverage_gaps'])
        recovered, _ = self.server({'ses_parent': [assistant('ses_parent', 'msg_final')]})
        from modport.token_budget import reconcile_token_session
        self.assertTrue(reconcile_token_session(recovered, self.root, 'ses_parent', cwd=self.root, deadline=time.monotonic()+2))
        self.assertTrue(read_token_budget(self.root)['usage_complete'])
        self.assertEqual(read_token_budget(self.root)['unknown_calls'], 0)
        self.assertEqual(read_token_budget(self.root)['used_tokens'], 23)

    def test_recovery_with_only_previous_turn_usage_is_not_current_call_completion(self):
        server, _ = self.server({'ses_parent': [assistant('ses_parent', 'msg_previous', parent='msg_previous_user')]})
        with patch.object(server, '_request', side_effect=TimeoutError('lost response')):
            with self.assertRaises(TimeoutError):
                server.send_message('ses_parent', 'work', cwd=self.root, message_id='msg_current')
        server.messages('ses_parent', cwd=self.root)
        self.assertEqual(read_token_budget(self.root)['unknown_calls'], 1)
        self.assertFalse(read_token_budget(self.root)['usage_complete'])

    def test_missing_provider_usage_and_uninitialized_ledger_are_unknown(self):
        self.assertIsNone(read_token_budget(self.root)['used_tokens'])
        initialize_token_budget(self.root, 50)
        record_token_message(self.root, {'role': 'assistant', 'id': 'msg_missing', 'sessionID': 'ses_parent'})
        self.assertFalse(read_token_budget(self.root)['usage_complete'])
        self.assertEqual(read_token_budget(self.root)['unknown_messages'], 1)

    def test_sse_budget_observation_requests_public_abort(self):
        self.set_limit(20)
        server, requests = self.server({'ses_parent': []})
        server.base_url = 'http://localhost:1'
        server.env = {}
        event = {'type': 'message.updated', 'properties': {'info': assistant('ses_parent', 'msg_a')['info']}}
        response = io.BytesIO(('data: '+json.dumps(event)+'\n\n').encode())
        with patch('modport.opencode_runtime.urlopen', return_value=response):
            self.assertEqual(list(server.events(cwd=self.root, deadline=time.monotonic()+2)), [event])
        self.assertTrue(any(path.endswith('/abort') for _, path, _ in requests))
        self.assertTrue(read_token_budget(self.root)['exhausted'])

    def test_native_child_event_is_included_in_budget_cancellation(self):
        self.set_limit(20)
        server, requests = self.server({'ses_parent': []})
        server.base_url = 'http://localhost:1'
        server.env = {}
        event = {'type': 'message.updated', 'properties': {'info': assistant('ses_native_child', 'msg_child')['info']}}
        response = io.BytesIO(('data: ' + json.dumps(event) + '\n\n').encode())
        with patch('modport.opencode_runtime.urlopen', return_value=response):
            list(server.events(cwd=self.root, deadline=time.monotonic() + 2))
        aborted = {path.split('/')[2] for _, path, _ in requests if path.endswith('/abort')}
        self.assertEqual({'ses_parent', 'ses_native_child'}, aborted)

    def test_summary_transport_charges_usage_and_preserves_exhaustion_reason(self):
        from test_summary_transport import FakeOpenCodeServer
        from modport.summary_transport import OpenCodeSummaryScope, SummaryTransportError
        from modport.prompt_compressor import SummaryRequest
        self.set_limit(20)
        class SummaryServer(FakeOpenCodeServer, OpenCodeServer):
            send_message = OpenCodeServer.send_message
            def __init__(self):
                response = assistant('ses-summary', 'msg-summary-assistant')
                response['info'].update(modelID='gpt-6-luna', variant='max', finish='stop')
                response['parts'] = [{'type': 'text', 'text': '{"summary":"fixed"}'}]
                super().__init__(response=response)
                self._session_directories = {'ses-summary': self_root}
            def _request(self, method, path, **kwargs):
                if method == 'POST' and path.endswith('/message'):
                    body = kwargs['body']
                    self.response['info']['parentID'] = body['messageID']
                    return FakeOpenCodeServer.send_message(self, 'ses-summary', body['parts'][0]['text'], message_id=body['messageID'])
                if path.endswith('/children'):
                    return []
                if method == 'GET' and path.endswith('/message'):
                    return [self.response]
                raise AssertionError(path)
        self_root = self.root
        server = SummaryServer()
        scope = OpenCodeSummaryScope(command=object(), root=self.root, worktree=self.root)
        request = SummaryRequest('history', 'gpt-6-luna', 'max', 100, 'stage', timeout=5, idle_timeout=1, output_byte_limit=100)
        with patch('modport.opencode_runtime.OpenCodeServer.start', return_value=server):
            with scope:
                self.assertEqual(scope.summarize(request, log_path=self.root/'summary.json'), '{"summary":"fixed"}')
                self.assertEqual(read_token_budget(self.root)['used_tokens'], 23)
                with self.assertRaises(SummaryTransportError) as caught:
                    scope.summarize(request, log_path=self.root/'summary-denied.json')
                self.assertEqual(caught.exception.code, 'token_budget_exhausted')
        self.assertTrue(read_token_budget(self.root)['usage_complete'])

    def test_tool_free_assignment_uses_real_usage_consumer_with_accounting_root(self):
        from test_opencode_agent import _Server
        from modport.opencode_agent import run_agent
        class AssignmentServer(_Server, OpenCodeServer):
            send_message = OpenCodeServer.send_message
            messages = OpenCodeServer.messages
            def __init__(self):
                super().__init__()
                self._session_directories = {'ses_test_parity': self_root}
            def _request(self, method, path, **kwargs):
                if method == 'POST' and path.endswith('/message'):
                    body = kwargs['body']
                    result = _Server.send_message(self, 'ses_test_parity', body['parts'][0]['text'], message_id=body['messageID'])
                    for message in self._messages.values():
                        message['info']['tokens'] = {'total': 11}
                    return result
                if method == 'GET' and path.endswith('/message'):
                    return list(self._messages.values())
                if path.endswith('/children'):
                    return []
                raise AssertionError(path)
        self_root = self.root
        server = AssignmentServer()
        with patch('modport.opencode_agent.OpenCodeServer.start', return_value=server):
            result = run_agent(prompt='review', cwd=self.root, log=self.root/'agent.jsonl', model='gpt-6-luna', variant='max', timeout=5, no_tools=True, token_budget_root=self.root)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(read_token_budget(self.root)['used_tokens'], 11)
        self.assertTrue(read_token_budget(self.root)['usage_complete'])

    def test_current_host_denies_supervisor_assignment_and_settles_exhaustion(self):
        from modport.contracts import OperationInput
        from modport.operations import MigrationOperations
        from modport.workflow import compile_migration_workflow
        self.set_limit(0)
        request = MigrationRequest('mod', 'repo', '1.20.1', '1.21.1', budget=Budget(max_tokens=0))
        header = {'request': request.to_dict(), 'definition': compile_migration_workflow(request).to_dict(),
                  'run_dir': str(self.root), 'deadline_epoch': None}
        host = MigrationOperations()
        app = host._new_application()
        snapshot = {'run_id': 'current', 'state': 'running', 'tasks': {}, 'waits': {}, 'application_state': app}
        self.assertEqual(host._schedule(snapshot, header, app, 'supervisor'), [])
        self.assertEqual(app['agent_assignments'], 0)
        self.assertEqual(app['stop_reason'], 'token_budget_exhausted')
        operations, _ = host._decision(snapshot, header)
        self.assertEqual(next(op for op in operations if op['kind']=='finish')['state'], 'failed')
        command = OperationInput('current', 'coder', 'coder', 'current:coder:1', str(self.root))
        snapshot['tasks'] = {'coder': {'attempts': [{'state': 'running', 'command': {'execution_id': command.command_id, 'payload': command.to_dict()}}]}}
        operations, _ = host._decision(snapshot, header)
        cancellation = next(op for op in operations if op['kind']=='cancel')
        self.assertEqual(cancellation['task_id'], 'coder')
        self.assertEqual(cancellation['reason'], 'token_budget_exhausted')

    def test_simultaneous_admission_keeps_inflight_overshoot_visible(self):
        initialize_token_budget(self.root, 50)
        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(lambda i: admit_token_call(self.root, 'ses_parent', 'msg_'+str(i)), range(4)))
        snapshot = read_token_budget(self.root)
        self.assertEqual(snapshot['in_flight_calls'], 4)
        self.assertFalse(snapshot['usage_complete'])
        self.assertFalse(snapshot['hard_stream_cap'])


if __name__ == '__main__':
    unittest.main()
