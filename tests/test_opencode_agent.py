"""Current assignment contract for the managed OpenCode transport."""
import json
from pathlib import Path
import queue
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from modport.opencode_agent import _TurnEventReader, _public_events, run_agent
from modport.agent_dialogue import AgentDialogueError
from modport.opencode_runtime import (OpenCodeCleanupError, OpenCodeEventConnectTimeout,
                                     OpenCodeServer)
from modport.telemetry import public_last_message


class _Server:
    def __init__(self):
        self.turns = []
        self._event_batches = queue.Queue()
        self._messages = {}
        self._post_started = threading.Event()
        self.drop_once_after_post = False
        self.end_stream_after_connect = False
        self._dropped = False
        self._event_stream_number = 0

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return None

    def require_model(self, *, cwd, model, variant, deadline):
        assert model in ('gpt-6-luna', 'openai/gpt-6-luna') and variant == 'max'
        return {'providerID': 'openai', 'modelID': model, 'variant': variant}

    def providers(self, *, cwd, deadline):
        return {'all': [{'id': 'openai', 'models': {
            'gpt-6-luna': {'limit': {'context': 10000}}}}]}

    def create_session(self, *, cwd, title, model, variant, deadline):
        return {'id': 'ses_test_parity'}

    def tool_ids(self, *, cwd, deadline):
        return ['bash', 'read', 'edit', 'write', 'apply_patch', 'patch', 'multiedit',
                'modport_sandbox_read_run_artifact']

    def mcp_status(self, *, cwd, deadline):
        return {'modport_rework': {'status': 'failed'}}

    def send_message(self, session_id, prompt, **kwargs):
        self.turns.append((session_id, prompt, kwargs))
        turn = len(self.turns)
        user_message_id = kwargs['message_id']
        user_info = {'id': user_message_id, 'role': 'user', 'sessionID': session_id}
        assistant_id = f'msg_test_{turn}'
        info = {'id': assistant_id, 'role': 'assistant', 'sessionID': session_id,
                'parentID': user_message_id, 'time': {'created': turn, 'completed': turn}}
        if turn == 1:
            parts = [{'type': 'text', 'text': 'Plan fact: nonce-83749'}]
        elif kwargs.get('output_format') is not None:
            info['structured'] = {'review': 'request_rework', 'reason': 'synthetic issue'}
            parts = []
        else:
            intermediate_info = {'id': 'msg_intermediate', 'role': 'assistant',
                                 'sessionID': session_id, 'parentID': user_message_id,
                                 'time': {'created': turn, 'completed': turn}}
            intermediate = {'info': intermediate_info, 'parts': [
                {'type': 'tool', 'tool': 'modport_sandbox_run_project_command',
                 'callID': 'call_intermediate', 'state': {'status': 'completed',
                 'output': '{"artifact_path":"artifacts/executions/c/opencode-shell/e.json"}'}}]}
            self._messages[intermediate_info['id']] = intermediate
            parts = [{'type': 'text', 'text': 'Used prior plan fact nonce-83749'}]
        response = {'info': info, 'parts': parts}
        self._messages[assistant_id] = response
        self._post_started.set()
        events = [
            {'id': f'evt_user_{turn}', 'type': 'message.updated',
             'properties': {'sessionID': session_id, 'info': user_info}},
        ]
        if turn != 1 and kwargs.get('output_format') is None:
            events.extend([
                {'id': f'evt_mid_{turn}', 'type': 'message.updated',
                 'properties': {'sessionID': session_id, 'info': intermediate_info}},
                {'id': f'evt_part_{turn}', 'type': 'message.part.updated',
                 'properties': {'sessionID': session_id, 'part': {
                     'id': 'prt_tool', 'messageID': intermediate_info['id'], 'type': 'tool'}}},
            ])
        events.extend([
            {'id': f'evt_assistant_{turn}', 'type': 'message.updated',
             'properties': {'sessionID': session_id, 'info': info}},
            {'id': f'evt_idle_{turn}', 'type': 'session.idle',
             'properties': {'sessionID': session_id}},
        ])
        self._event_batches.put(events)
        return response

    def events(self, *, cwd, deadline, stop_event):
        self._event_stream_number += 1
        yield {'id': f'evt_connected_{self._event_stream_number}',
               'type': 'server.connected', 'properties': {}}
        if self.end_stream_after_connect:
            return
        if self.drop_once_after_post and not self._dropped:
            self._post_started.wait(timeout=max(0, deadline - time.monotonic()))
            if self._post_started.is_set():
                self._dropped = True
                return
        while not stop_event.is_set():
            try:
                events = self._event_batches.get(timeout=0.05)
            except queue.Empty:
                continue
            for event in events:
                yield event

    def get_message(self, session_id, message_id, **kwargs):
        return self._messages[message_id]


class OpenCodeAgentTests(unittest.TestCase):
    def test_successful_turn_cannot_hide_unconfirmed_teardown(self):
        class CleanupServer(_Server):
            __exit__ = OpenCodeServer.__exit__

            def close(self, **_kwargs):
                return {'cleanup_confirmed': False, 'target_pid': 900007}

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            workspace = root / 'worktree'
            workspace.mkdir()
            server = CleanupServer()
            with patch('modport.opencode_agent.OpenCodeServer.start', return_value=server):
                with self.assertRaises(OpenCodeCleanupError) as caught:
                    run_agent(prompt='Return one result', cwd=workspace,
                              log=root / 'agent.log', model='gpt-6-luna',
                              variant='max', timeout=5)
        self.assertEqual(900007, caught.exception.cleanup_diagnostic['target_pid'])

    def test_two_turn_context_preflight_preserves_plan_without_sending_execution(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            workspace = root / 'worktree'
            workspace.mkdir()
            plan = root / 'plan.md'
            server = _Server()
            budget = {'context_window': 400, 'input_token_budget': 280,
                      'output_token_reserve': 80, 'tool_token_reserve': 40}
            with patch('modport.opencode_agent.OpenCodeServer.start', return_value=server):
                with self.assertRaises(AgentDialogueError) as caught:
                    run_agent(prompt='Execute plan', planning_prompt='Make plan',
                              cwd=workspace, log=root / 'agent.log', plan_path=plan,
                              model='gpt-6-luna', variant='max', timeout=10,
                              session_context_budget=budget)
            self.assertEqual('session_context_budget_exceeded', caught.exception.code)
            self.assertEqual(1, len(server.turns))
            self.assertIn('nonce-83749', plan.read_text())
            self.assertTrue((root / 'agent.plan.log').is_file())
            self.assertEqual('exceeded', json.loads(
                (root / 'session-context-preflight.json').read_text())['status'])

    def test_two_turn_context_preflight_allows_available_budget(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            server = _Server()
            budget = {'context_window': 10000, 'input_token_budget': 7000,
                      'output_token_reserve': 2000, 'tool_token_reserve': 1000}
            with patch('modport.opencode_agent.OpenCodeServer.start', return_value=server):
                result = run_agent(prompt='Execute plan', planning_prompt='Make plan',
                                   cwd=root, log=root / 'agent.log', plan_path=root / 'plan.md',
                                   model='gpt-6-luna', variant='max', timeout=10,
                                   session_context_budget=budget)
            self.assertEqual(2, len(server.turns))
            self.assertEqual(0, result.returncode)
            evidence = json.loads((root / 'session-context-preflight.json').read_text())
            self.assertEqual('passed', evidence['status'])
            self.assertEqual('transcript_byte_upper_bound',
                             evidence['capacity']['prior_source'])

    def test_two_turn_skill_route_resolves_catalog_budget(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            server = _Server()
            with patch('modport.opencode_agent.OpenCodeServer.start', return_value=server):
                result = run_agent(prompt='Execute plan', planning_prompt='Make plan',
                                   cwd=root, log=root / 'agent.log', plan_path=root / 'plan.md',
                                   model='gpt-6-luna', variant='max', timeout=10,
                                   auto_context_budget=True)
            self.assertEqual(0, result.returncode)
            self.assertEqual(2, len(server.turns))
            self.assertEqual('OpenCode provider catalog', json.loads(
                (root / 'session-context-preflight.json').read_text())['profile_source'])

    def test_host_artifact_read_content_is_not_copied_to_public_log(self):
        events = [json.loads(line) for line in _public_events('ses_test', {
            'parts': [{'type': 'tool', 'tool': 'modport_sandbox_read_run_artifact',
                       'state': {'status': 'completed', 'output': 'private fact 123'}}],
        }).splitlines()]
        self.assertNotIn('private fact 123', json.dumps(events))
        self.assertEqual('read_run_artifact', events[-1]['item']['tool'])
        self.assertIn('output_sha256', events[-1]['item'])

    def test_read_artifact_audit_keeps_safe_location_without_content(self):
        value = {'path': 'artifacts/fact.txt', 'chunk_sha256': 'a' * 64,
                 'verified_sha256': 'b' * 64,
                 'offset': 3, 'next_offset': 9, 'total_bytes': 20,
                 'content_utf8': 'private fact 123'}
        events = [json.loads(line) for line in _public_events('ses_test', {
            'parts': [{'type': 'tool', 'tool': 'modport_sandbox_read_run_artifact',
                       'state': {'status': 'completed', 'output': json.dumps(value)}}],
        }).splitlines()]
        item = events[-1]['item']
        self.assertEqual('artifacts/fact.txt', item['artifact_path'])
        self.assertEqual(3, item['offset'])
        self.assertEqual('b' * 64, item['verified_sha256'])
        self.assertNotIn('private fact 123', json.dumps(events))

    def test_registered_mcp_must_be_connected_before_model_turn(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            with patch('modport.opencode_agent.OpenCodeServer.start', return_value=_Server()):
                with self.assertRaisesRegex(RuntimeError, 'MCP tools are unavailable'):
                    run_agent(prompt='Review', cwd=root, log=root / 'agent.log',
                              model='gpt-6-luna', variant='max', timeout=10,
                              mcp={'modport_rework': {'type': 'local'}})

    def test_sandbox_tool_result_remains_addressable_in_agent_audit(self):
        events = [json.loads(line) for line in _public_events('ses_test', {
            'info': {'id': 'msg_test'},
            'parts': [{'type': 'tool', 'tool': 'modport_sandbox_run_project_command',
                       'callID': 'call_test', 'state': {'status': 'completed',
                       'output': '{"artifact_path":"artifacts/executions/c/opencode-shell/e.json"}'}},
                      {'type': 'text', 'text': 'Observed exit code 0'}],
        }).splitlines()]
        tool = next(row['item'] for row in events if row.get('item', {}).get('type') == 'mcp_tool_call')
        self.assertEqual(tool['server'], 'modport_sandbox')
        self.assertEqual(tool['tool'], 'run_project_command')
        self.assertEqual(tool['artifact_path'],
                         'artifacts/executions/c/opencode-shell/e.json')
        self.assertIn('output_sha256', tool)
        self.assertNotIn('output', tool)

    def test_tool_result_content_is_not_copied_to_public_log(self):
        events = _public_events('ses_test', {
            'parts': [{'type': 'tool', 'tool': 'modport_rework_request_rework',
                       'state': {'status': 'completed',
                                 'output': {'api_key': 'MCP_SECRET_SENTINEL'},
                                 'error': 'private error detail'}},
                      {'type': 'text', 'text': 'I saw MCP_SECRET_SENTINEL'}],
        })
        self.assertNotIn('MCP_SECRET_SENTINEL', events)
        self.assertNotIn('private error detail', events)
        item = next(row['item'] for row in map(json.loads, events.splitlines())
                    if row.get('item', {}).get('type') == 'mcp_tool_call')
        self.assertIn('output_sha256', item)
        self.assertIn('error_sha256', item)

        short_secret_events = [json.loads(line) for line in _public_events('ses_test', {
            'parts': [{'type': 'tool', 'tool': 'example',
                       'state': {'status': 'completed',
                                 'output': {'password': '123'}}},
                      {'type': 'text', 'text': 'Echo 123'}],
        }).splitlines()]
        self.assertEqual(short_secret_events[-1]['item']['text'], '[REDACTED]')

    def test_read_only_explicitly_disables_all_edit_tools(self):
        from modport.opencode_agent import _tool_policy
        with tempfile.TemporaryDirectory() as temp:
            policy = _tool_policy(_Server(), Path(temp), read_only=True,
                                  deadline=time.monotonic() + 1)
        for tool in ('edit', 'write', 'apply_patch', 'patch', 'multiedit'):
            self.assertIs(policy[tool], False)

    def test_host_network_deny_keeps_local_edit_and_artifact_read_tools(self):
        from modport.opencode_agent import _tool_policy
        with tempfile.TemporaryDirectory() as temp, patch.dict(
                'os.environ', {'MODPORT_DENY_NETWORK_TOOLS': '1'}), patch.object(
                    _Server, 'tool_ids', return_value=[
                        'bash', 'edit', 'modport_sandbox_read_run_artifact',
                        'modport_sandbox_run_project_command', 'webfetch', 'websearch']):
            policy = _tool_policy(_Server(), Path(temp), read_only=False,
                                  deadline=time.monotonic() + 1)
        for name in ('bash', 'modport_sandbox_run_project_command',
                     'webfetch', 'websearch'):
            self.assertIs(policy[name], False)
        self.assertNotIn('edit', policy)
        self.assertNotIn('modport_sandbox_read_run_artifact', policy)

    def test_failed_tool_is_not_recorded_as_running_and_truncation_is_marked(self):
        events = [json.loads(line) for line in _public_events('ses_test', {
            'parts': [{'type': 'tool', 'tool': 'modport_sandbox_run_project_command',
                       'callID': 'call_failure',
                       'state': {'status': 'error', 'error': 'x' * 9000}}],
        }).splitlines()]
        self.assertEqual(events[-1]['type'], 'item.failed')
        self.assertTrue(events[-1]['item']['error_truncated'])

    def test_two_turns_share_session_and_persist_plan(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            workspace = root / 'worktree'
            workspace.mkdir()
            log = root / 'logs' / 'agent.log'
            plan = root / 'artifacts' / 'plan.md'
            server = _Server()
            with (patch('modport.opencode_agent.OpenCodeServer.start', return_value=server) as start,
                  patch.object(server, 'mcp_status', return_value={
                      'modport_sandbox': {'status': 'connected'}})):
                result = run_agent(prompt='Execute plan', planning_prompt='Make plan',
                                   cwd=workspace, log=log, plan_path=plan,
                                   model='gpt-6-luna', variant='max', timeout=10,
                                   run_root=root, command_id='plan-task')
            self.assertIn('modport_sandbox', start.call_args.kwargs['config'].mcp)
            self.assertEqual([turn[0] for turn in server.turns], ['ses_test_parity'] * 2)
            self.assertEqual(plan.read_text(), 'Plan fact: nonce-83749\n')
            self.assertEqual(public_last_message(result.stdout),
                             'Used prior plan fact nonce-83749')
            self.assertIn('call_intermediate', result.stdout)
            metadata = json.loads((plan.parent / 'session.json').read_text())
            self.assertEqual(metadata['thread_id'], 'ses_test_parity')
            self.assertNotIn('call_intermediate', Path(metadata['planning_log']).read_text())
            self.assertEqual(result.dialogue_metadata['turns'], 2)
            self.assertFalse(server.turns[1][2]['tools']['bash'])
            self.assertFalse(server.turns[0][2]['tools']['modport_rework_request_rework'])
            self.assertFalse(server.turns[0][2]['tools']['edit'])
            self.assertFalse(server.turns[0][2]['tools']['write'])
            self.assertFalse(server.turns[0][2]['tools']['apply_patch'])
            self.assertFalse(server.turns[0][2]['tools']['patch'])
            self.assertFalse(server.turns[0][2]['tools']['multiedit'])
            self.assertFalse(server.turns[0][2]['tools']['modport_sandbox_run_project_command'])
            self.assertNotIn('modport_sandbox_read_run_artifact', server.turns[0][2]['tools'])
            self.assertNotIn('modport_rework_request_rework', server.turns[1][2]['tools'])

    def test_diagnostic_edits_keep_project_execution_disabled_in_both_turns(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            workspace = root / 'workspaces' / 'diagnosis'
            workspace.mkdir(parents=True)
            server = _Server()
            with (patch('modport.opencode_agent.OpenCodeServer.start', return_value=server) as start,
                  patch.object(server, 'mcp_status', return_value={
                      'modport_sandbox': {'status': 'connected'}})):
                run_agent(prompt='Fix the isolated typo', planning_prompt='Inspect the cause',
                          cwd=workspace, log=root / 'logs' / 'diagnostic.log',
                          plan_path=root / 'artifacts' / 'plan.md',
                          model='gpt-6-luna', variant='max', timeout=10,
                          read_only=False, allow_project_commands=False,
                          run_root=root, command_id='diagnostic')
            self.assertEqual('deny', start.call_args.kwargs['config'].permission[
                'modport_sandbox_run_project_command'])
            for _, _, turn in server.turns:
                self.assertFalse(turn['tools']['modport_sandbox_run_project_command'])
            self.assertFalse(server.turns[0][2]['tools']['edit'])
            self.assertNotIn('edit', server.turns[1][2]['tools'])
            self.assertNotIn('modport_sandbox_read_run_artifact', server.turns[1][2]['tools'])
            descriptor = root / 'artifacts/executions/diagnostic/opencode-shell/session.json'
            self.assertFalse(json.loads(descriptor.read_text())['allow_project_command'])

    def test_structured_output_becomes_public_final_message(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            workspace = root / 'worktree'
            workspace.mkdir()
            schema_path = root / 'report.schema.json'
            schema_path.write_text('{"type":"object"}', encoding='utf-8')
            server = _Server()
            with patch('modport.opencode_agent.OpenCodeServer.start', return_value=server):
                result = run_agent(prompt='Return report', planning_prompt='Plan report',
                                   cwd=workspace, log=root / 'agent.log',
                                   plan_path=root / 'plan.md', schema_path=schema_path,
                                   model='gpt-6-luna', variant='max', timeout=10)
            output = json.loads(public_last_message(result.stdout))
            self.assertEqual(output, {'review': 'request_rework', 'reason': 'synthetic issue'})
            self.assertEqual(server.turns[1][2]['output_format'], {
                'type': 'json_schema', 'schema': {'type': 'object'}})
            self.assertTrue(server.turns[1][2]['message_id'].startswith('msg'))

    def test_wait_for_final_requires_completion_not_just_message_observation(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            server = _Server()
            user_id = 'msg_current_turn'
            final_id = 'msg_delayed_completion'
            reader = _TurnEventReader(
                server, cwd=root, session_id='ses_test_parity',
                message_id=user_id, deadline=time.monotonic() + 2)
            reader.start()
            reader.mark_turn_posted()
            partial_info = {'id': final_id, 'role': 'assistant',
                            'sessionID': 'ses_test_parity', 'parentID': user_id,
                            'time': {'created': 1}}
            server._event_batches.put([
                {'id': 'evt_user_wait', 'type': 'message.updated',
                 'properties': {'sessionID': 'ses_test_parity',
                                'info': {'id': user_id, 'role': 'user',
                                         'sessionID': 'ses_test_parity'}}},
                {'id': 'evt_final_partial', 'type': 'message.updated',
                 'properties': {'sessionID': 'ses_test_parity', 'info': partial_info}},
            ])
            try:
                observed_deadline = time.monotonic() + 1
                while (final_id not in reader.assistant_message_ids
                       and time.monotonic() < observed_deadline):
                    time.sleep(0.005)
                self.assertIn(final_id, reader.assistant_message_ids)
                finished = threading.Event()
                errors = []

                def wait_for_completion():
                    try:
                        reader.wait_for_final(final_id)
                    except Exception as exc:
                        errors.append(exc)
                    finally:
                        finished.set()

                waiter = threading.Thread(target=wait_for_completion)
                waiter.start()
                time.sleep(0.05)
                self.assertFalse(finished.is_set())
                complete_info = {**partial_info,
                                 'time': {'created': 1, 'completed': 2}}
                server._event_batches.put([{
                    'id': 'evt_final_completed', 'type': 'message.updated',
                    'properties': {'sessionID': 'ses_test_parity', 'info': complete_info},
                }])
                self.assertTrue(finished.wait(1))
                waiter.join(timeout=1)
                self.assertEqual(errors, [])
            finally:
                reader.close()

    def test_completed_turn_does_not_reconnect_after_stream_closes(self):
        class ClosingServer(_Server):
            def events(self, *, cwd, deadline, stop_event):
                self._event_stream_number += 1
                yield {'type': 'server.connected', 'properties': {}}
                for event in self._event_batches.get(timeout=1):
                    yield event

        with tempfile.TemporaryDirectory() as temp:
            server = ClosingServer()
            reader = _TurnEventReader(
                server, cwd=Path(temp), session_id='ses_test_parity',
                message_id='msg_current', deadline=time.monotonic() + 2)
            reader.start()
            reader.mark_turn_posted()
            reader.expect_final('msg_final')
            server._event_batches.put([
                {'type': 'message.updated', 'properties': {
                    'sessionID': 'ses_test_parity',
                    'info': {'id': 'msg_current', 'role': 'user'}}},
                {'type': 'message.updated', 'properties': {
                    'sessionID': 'ses_test_parity',
                    'info': {'id': 'msg_final', 'role': 'assistant',
                             'time': {'completed': 1}}}},
            ])
            try:
                reader.wait_for_final('msg_final')
                reader._thread.join(timeout=1)
                self.assertFalse(reader._thread.is_alive())
                self.assertEqual(server._event_stream_number, 1)
            finally:
                reader.close()

    def test_completed_event_before_post_reply_clears_false_gap(self):
        class ClosingServer(_Server):
            def events(self, *, cwd, deadline, stop_event):
                self._event_stream_number += 1
                yield {'type': 'server.connected', 'properties': {}}
                for event in self._event_batches.get(timeout=1):
                    yield event

        with tempfile.TemporaryDirectory() as temp:
            server = ClosingServer()
            reader = _TurnEventReader(
                server, cwd=Path(temp), session_id='ses_test_parity',
                message_id='msg_current', deadline=time.monotonic() + 2)
            reader.start()
            reader.mark_turn_posted()
            server._event_batches.put([
                {'type': 'message.updated', 'properties': {
                    'sessionID': 'ses_test_parity',
                    'info': {'id': 'msg_current', 'role': 'user'}}},
                {'type': 'message.updated', 'properties': {
                    'sessionID': 'ses_test_parity',
                    'info': {'id': 'msg_final', 'role': 'assistant',
                             'time': {'completed': 1}}}},
            ])
            try:
                reader._thread.join(timeout=1)
                self.assertTrue(reader.turn_gap)
                reader.expect_final('msg_final')
                self.assertFalse(reader.turn_gap)
                reader.wait_for_final('msg_final')
            finally:
                reader.close()

    def test_pre_ready_header_timeout_retries_without_false_gap(self):
        class RetryingServer(_Server):
            def events(self, *, cwd, deadline, stop_event):
                self._event_stream_number += 1
                if self._event_stream_number == 1:
                    raise OpenCodeEventConnectTimeout('delayed headers')
                yield {'type': 'server.connected', 'properties': {}}
                while not stop_event.is_set():
                    stop_event.wait(0.01)

        with tempfile.TemporaryDirectory() as temp:
            server = RetryingServer()
            reader = _TurnEventReader(
                server, cwd=Path(temp), session_id='ses_test_parity',
                message_id='msg_current', deadline=time.monotonic() + 2)
            try:
                reader.start()
                reader.mark_turn_posted()
                self.assertEqual(server._event_stream_number, 2)
                self.assertEqual(reader.reconnect_count, 0)
            finally:
                reader.close()

    def test_sse_gap_before_completed_turn_fails_closed(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            workspace = root / 'worktree'
            workspace.mkdir()
            server = _Server()
            server.drop_once_after_post = True
            with patch('modport.opencode_agent.OpenCodeServer.start', return_value=server):
                with self.assertRaisesRegex(RuntimeError, 'SSE disconnected'):
                    run_agent(prompt='Execute', cwd=workspace, log=root / 'agent.log',
                              model='gpt-6-luna', variant='max', timeout=5)
            self.assertGreaterEqual(server._event_stream_number, 1)

    def test_event_disconnect_between_ready_and_post_fails_closed(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            server = _Server()
            server.end_stream_after_connect = True
            reader = _TurnEventReader(
                server, cwd=root, session_id='ses_test_parity',
                message_id='msg_current_turn', deadline=time.monotonic() + 2)
            reader.start()
            time.sleep(0.02)
            try:
                with self.assertRaisesRegex(RuntimeError, 'interrupted before'):
                    reader.mark_turn_posted()
            finally:
                reader.close()


if __name__ == '__main__':
    unittest.main()
