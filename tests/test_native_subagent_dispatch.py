"""Opt-in, bounded native dispatch probe against the real OpenCode server.

Set MODPORT_OPENCODE_BIN to OpenCode 1.18.32. Provider responses are scripted
locally; this checks transport, native task dispatch and permissions, not model
quality. Evidence is retained under /tmp on success and failure.
"""
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import sqlite3
import tempfile
import threading
import time
import traceback
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from modport.desktop_model_settings import PROVIDER_CONFIG_ENV
from modport.opencode_agent import run_agent
from modport.opencode_runtime import OpenCodeServer
from modport.telemetry import public_last_message
from modport.token_budget import read_token_budget


def _is_native_child_request(body):
    return any('NATIVE_CHILD_PROBE' in json.dumps(message.get('content', ''))
               for message in body['messages'] if message.get('role') == 'user')


class _ScriptedProvider:
    def __init__(self, root, agent, *, workspace=None, on_child_result=None):
        self.root = root
        self.agent = agent
        self.workspace = workspace or root / 'workspace'
        self.requests = []
        self.unexpected = []
        self.parent_step = 0
        self.child_step = 0
        provider = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def do_GET(self):
                provider.unexpected.append(self.path)
                self.send_error(404)

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                provider.requests.append({'path': self.path, 'body': body})
                (root / 'provider-requests.json').write_text(json.dumps(provider.requests, indent=2))
                if self.path != '/v1/chat/completions':
                    provider.unexpected.append(self.path)
                    self.send_error(404)
                    return
                is_parent = not _is_native_child_request(body)
                if is_parent:
                    provider.parent_step += 1
                    if provider.parent_step == 1:
                        calls = [('task', {'description': 'Read-only child probe',
                                          'subagent_type': provider.agent,
                                          'prompt': 'NATIVE_CHILD_PROBE: inspect permission restrictions.'})]
                        content = None
                    else:
                        calls = []
                        returned = [message.get('content', '') for message in body['messages']
                                    if message.get('role') == 'tool']
                        content = 'PARENT_RECEIVED_CHILD' if any(
                            'CHILD_DENIALS_OBSERVED' in str(value) for value in returned
                        ) else 'PARENT_MISSING_CHILD_RESULT'
                else:
                    provider.child_step += 1
                    if provider.child_step == 1:
                        calls = [
                            ('read', {'filePath': str(provider.workspace / 'protected.txt')}),
                            ('edit', {'filePath': str(provider.workspace / 'protected.txt'),
                                      'oldString': 'unchanged', 'newString': 'modified'}),
                            ('bash', {'command': 'touch shell-executed',
                                      'description': 'Probe denied native shell'}),
                            ('webfetch', {'url': provider.url + '/forbidden-network',
                                         'format': 'text'}),
                        ]
                        content = None
                    else:
                        if on_child_result is not None:
                            on_child_result()
                        calls = []
                        returned = [message.get('content', '') for message in body['messages']
                                    if message.get('role') == 'tool']
                        content = ('CHILD_DENIALS_OBSERVED' if any(
                            'unchanged' in str(value) for value in returned
                        ) else 'CHILD_MISSING_READ_RESULT')
                delta = {'role': 'assistant'}
                if calls:
                    delta['tool_calls'] = [
                        {'index': index, 'id': f'call_{len(provider.requests)}_{index}',
                         'type': 'function', 'function': {
                             'name': name, 'arguments': json.dumps(arguments)}}
                        for index, (name, arguments) in enumerate(calls)
                    ]
                else:
                    delta['content'] = content
                chunks = [
                    {'id': f'completion-{len(provider.requests)}',
                     'object': 'chat.completion.chunk', 'created': int(time.time()),
                     'model': body['model'], 'choices': [
                         {'index': 0, 'delta': delta, 'finish_reason': None}]},
                    {'id': f'completion-{len(provider.requests)}',
                     'object': 'chat.completion.chunk', 'created': int(time.time()),
                     'model': body['model'], 'choices': [
                         {'index': 0, 'delta': {},
                          'finish_reason': 'tool_calls' if calls else 'stop'}],
                     'usage': {'prompt_tokens': 11, 'completion_tokens': 7, 'total_tokens': 18}},
                ]
                data = ''.join('data: ' + json.dumps(chunk) + '\n\n' for chunk in chunks)
                data += 'data: [DONE]\n\n'
                encoded = data.encode()
                self.send_response(200)
                self.send_header('Content-Type', 'text/event-stream')
                self.send_header('Content-Length', str(len(encoded)))
                self.end_headers()
                self.wfile.write(encoded)
                self.wfile.flush()

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.server.daemon_threads = True
        self.url = f'http://127.0.0.1:{self.server.server_port}'
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *_args):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)


@unittest.skipUnless(os.environ.get('MODPORT_OPENCODE_BIN'),
                     'Set MODPORT_OPENCODE_BIN for the real native OpenCode probe')
class NativeSubagentDispatchTests(unittest.TestCase):
    def test_native_goal_registers_child_during_live_turn_and_accounts_for_usage(self):
        from modport.goal_runtime import run_goal
        from modport.workflow import WORKFLOW_VERSION

        binary = Path(os.environ['MODPORT_OPENCODE_BIN']).resolve()
        self.assertTrue(binary.is_file(), str(binary))
        root = Path(tempfile.mkdtemp(prefix='modport-native-goal-subagent-'))
        self.addCleanup(print, f'Native goal OpenCode probe evidence: {root}')
        workspace = root / 'worktree' / 'candidate'
        workspace.mkdir(parents=True)
        (workspace / 'protected.txt').write_text('unchanged\n')
        deadline = time.monotonic() + 30
        policy = {
            'default': {'model': 'probe/parent-model', 'reasoning_effort': 'low'},
            'roles': {
                'coder': {'model': 'probe/parent-model', 'reasoning_effort': 'low'},
            },
            'stages': {},
        }
        command = SimpleNamespace(command_id='current-native-subagent-goal', stage_id='coder',
            options={'workflow_version': WORKFLOW_VERSION, 'model_policy': policy,
                     'goal_read_only': True}, payload={})
        (root / 'goal-input.json').write_text(json.dumps(vars(command), indent=2))
        observations = {}
        servers = []
        reports = []
        validation_calls = []
        real_create = OpenCodeServer.create_session
        real_close = OpenCodeServer.close

        def record_created_server(server, *args, **kwargs):
            session = real_create(server, *args, **kwargs)
            servers.append(server)
            observations['parent_session_id'] = session['id']
            observations['configured_agents'] = json.loads(
                server.env['OPENCODE_CONFIG_CONTENT'])['agent']
            return session

        def observe_live_child():
            # The provider has received the child's second request, but has not
            # returned its final response. Registration here requires live SSE;
            # final transcript reconciliation has not run yet.
            bound = min(deadline, time.monotonic() + 2)
            child_usage = []
            while time.monotonic() < bound:
                # This is ModPort's application-owned ledger, read without
                # mutation. The parent's task response is still pending, so
                # aggregate usage alone cannot identify the child's charge.
                with sqlite3.connect((root / 'token-budget.sqlite3').as_uri() + '?mode=ro',
                                     uri=True, timeout=0.25) as connection:
                    connection.row_factory = sqlite3.Row
                    child_usage = [dict(row) for row in connection.execute(
                        'SELECT session_id,tokens,complete FROM usage WHERE session_id != ?',
                        (observations['parent_session_id'],))]
                if (len(servers[0]._session_directories) >= 2 and any(
                        row['complete'] and row['tokens'] == 18 for row in child_usage)):
                    break
                time.sleep(0.01)
            observations['live_session_directories'] = {
                session: str(directory) for session, directory in
                servers[0]._session_directories.items()}
            observations['live_token_summary'] = read_token_budget(root)
            observations['live_child_usage'] = child_usage
            (root / 'live-registration.json').write_text(json.dumps(observations, indent=2))

        def observe_then_close(server, **kwargs):
            try:
                observations['registered_before_close'] = {
                    session: str(directory) for session, directory in
                    server._session_directories.items()}
                sessions = server.sessions(cwd=workspace,
                    deadline=min(deadline, time.monotonic() + 3))
                observations['sessions'] = sessions
                observations['messages'] = {
                    session['id']: server.messages(session['id'], cwd=workspace,
                        deadline=min(deadline, time.monotonic() + 3)) for session in sessions}
                (root / 'observations.json').write_text(json.dumps(observations, indent=2))
            except Exception:
                observations['error'] = traceback.format_exc()
                (root / 'observation-error.txt').write_text(observations['error'])
            finally:
                diagnostic = real_close(server, **kwargs)
            return diagnostic

        def validate():
            validation_calls.append(True)
            return {'accepted': True, 'failures': [], 'evidence': {}}

        with _ScriptedProvider(root, 'general', workspace=workspace,
                               on_child_result=observe_live_child) as provider:
            configured = [{'id': 'probe', 'name': 'Local native goal dispatch probe',
                'api_type': 'openai-compatible', 'base_url': provider.url + '/v1',
                'models': [{'id': model, 'context_window': 32000,
                            'max_output_tokens': 1000, 'reasoning_efforts': ['low']}
                           for model in ['parent-model']]}]
            environment = {
                'PATH': os.environ.get('PATH', ''),
                'XDG_DATA_HOME': str(root / 'provider-login'),
                'MODPORT_OPENCODE_BIN': str(binary), 'MODPORT_DENY_NETWORK_TOOLS': '1',
                PROVIDER_CONFIG_ENV: json.dumps(configured),
            }
            with patch.dict(os.environ, environment, clear=True), patch.object(
                    OpenCodeServer, 'create_session', record_created_server), patch.object(
                    OpenCodeServer, 'close', observe_then_close):
                result = run_goal(command=command, root=root, worktree=workspace,
                    prompt='Dispatch the native child and return its result.',
                    objective='Probe the current native coder goal child dispatch.',
                    validate=validate, on_report=reports.append,
                    timeout=min(24, deadline - time.monotonic()))
            (root / 'goal-result.json').write_text(json.dumps(result.metadata, indent=2))
            self.assertEqual(0, result.returncode, result.metadata)
            self.assertTrue(result.metadata['host_accepted'], result.metadata)
            self.assertEqual([True], validation_calls)
            self.assertTrue(any('PARENT_RECEIVED_CHILD' in report for report in reports), reports)
            self.assertNotIn('error', observations, observations)
            self.assertEqual([], provider.unexpected)
            children = [session for session in observations['sessions'] if session.get('parentID')]
            self.assertEqual(1, len(children))
            child = children[0]
            self.assertEqual(result.metadata['thread_id'], child['parentID'])
            self.assertEqual(str(workspace), observations['live_session_directories'].get(child['id']))
            self.assertEqual(str(workspace), observations['registered_before_close'].get(child['id']))
            self.assertTrue(any(row['session_id'] == child['id']
                                and row['complete'] and row['tokens'] == 18
                                for row in observations['live_child_usage']), observations)
            for profile in observations['configured_agents'].values():
                self.assertNotIn('model', profile)
                self.assertNotIn('variant', profile)
            child_requests = [item['body'] for item in provider.requests
                              if _is_native_child_request(item['body'])]
            self.assertEqual(2, len(child_requests))
            self.assertTrue(all(request['model'] == 'parent-model' for request in child_requests))
            self.assertTrue(all(request['reasoning_effort'] == 'low' for request in child_requests))
            child_messages = observations['messages'][child['id']]
            self.assertIn('CHILD_DENIALS_OBSERVED', json.dumps(child_messages))
            self.assertEqual('unchanged\n', (workspace / 'protected.txt').read_text())
            self.assertFalse((workspace / 'shell-executed').exists())
            tokens = read_token_budget(root)
            (root / 'token-summary.json').write_text(json.dumps(tokens, indent=2))
            self.assertTrue(tokens['usage_complete'], tokens)
            self.assertEqual(18 * len(provider.requests), tokens['used_tokens'])
            self.assertGreater(tokens['used_tokens'], 18 * provider.parent_step)

    def test_native_children_select_frozen_models_and_preserve_restrictions(self):
        binary = Path(os.environ['MODPORT_OPENCODE_BIN']).resolve()
        self.assertTrue(binary.is_file(), str(binary))
        evidence_root = Path(tempfile.mkdtemp(prefix='modport-native-subagent-'))
        self.addCleanup(print, f'Native OpenCode probe evidence: {evidence_root}')
        total_deadline = time.monotonic() + 45
        for agent, override in [('general', False), ('explore', True)]:
            with self.subTest(agent=agent, explicit_override=override):
                root = evidence_root / agent
                workspace = root / 'workspace'
                workspace.mkdir(parents=True)
                (workspace / 'protected.txt').write_text('unchanged\n')
                policy = {'default': {'model': 'probe/parent-model', 'reasoning_effort': 'low'},
                          'roles': {'coder': {'model': 'probe/coder-model', 'reasoning_effort': 'high'}},
                          'stages': {}}
                if override:
                    policy['roles']['subagent'] = {
                        'model': 'probe/override-model', 'reasoning_effort': 'max'}
                expected_model = 'override-model' if override else 'coder-model'
                expected_effort = 'max' if override else 'high'
                observations = {}
                real_close = OpenCodeServer.close

                def observe_then_close(server, **kwargs):
                    try:
                        if kwargs.get('cleanup_reason') == 'context_exit':
                            deadline = min(total_deadline, time.monotonic() + 3)
                            sessions = server._request('GET', '/session',
                                query={'directory': str(workspace)}, deadline=deadline)
                            observations['sessions'] = sessions
                            observations['messages'] = {session['id']: server.messages(
                                session['id'], cwd=workspace, deadline=deadline)
                                for session in sessions}
                            observations['agents'] = server._request('GET', '/agent',
                                query={'directory': str(workspace)}, deadline=deadline)
                            (root / 'observations.json').write_text(json.dumps(observations, indent=2))
                    except Exception:
                        observations['error'] = traceback.format_exc()
                        (root / 'observation-error.txt').write_text(observations['error'])
                    finally:
                        diagnostic = real_close(server, **kwargs)
                    return diagnostic

                with _ScriptedProvider(root, agent) as provider:
                    configured = [{'id': 'probe', 'name': 'Local native dispatch probe',
                                   'api_type': 'openai-compatible', 'base_url': provider.url + '/v1',
                                   'models': [{'id': model, 'context_window': 32000,
                                               'max_output_tokens': 1000,
                                               'reasoning_efforts': ['low', 'high', 'max']}
                                              for model in ['parent-model', 'coder-model', 'override-model']]}]
                    environment = {
                        'PATH': os.environ.get('PATH', ''),
                        'XDG_DATA_HOME': str(root / 'provider-login'),
                        'MODPORT_OPENCODE_BIN': str(binary),
                        'MODPORT_DENY_NETWORK_TOOLS': '1',
                        PROVIDER_CONFIG_ENV: json.dumps(configured),
                    }
                    with patch.dict(os.environ, environment, clear=True), patch.object(
                            OpenCodeServer, 'close', observe_then_close):
                        result = run_agent(prompt='Dispatch the native child and return its result.',
                            cwd=workspace, log=root / 'agent.log', model='probe/parent-model',
                            variant='low', timeout=min(20, total_deadline - time.monotonic()),
                            read_only=True, model_policy=policy, token_budget_root=root / 'accounting')
                    self.assertEqual(0, result.returncode)
                    self.assertNotIn('error', observations, observations)
                    self.assertIn('PARENT_RECEIVED_CHILD', public_last_message(result.stdout))
                    self.assertEqual([], provider.unexpected)
                    child_requests = [item['body'] for item in provider.requests
                                      if item['body']['model'] != 'parent-model']
                    self.assertEqual(2, len(child_requests))
                    for request in child_requests:
                        self.assertEqual(expected_model, request['model'])
                        self.assertEqual(expected_effort, request['reasoning_effort'])
                        tools = {tool['function']['name'] for tool in request.get('tools', [])}
                        self.assertIn('read', tools)
                        self.assertTrue({'edit', 'write', 'bash', 'webfetch', 'websearch'}.isdisjoint(tools))
                    tool_results = [message for message in child_requests[-1]['messages']
                                    if message.get('role') == 'tool']
                    self.assertEqual(4, len(tool_results))
                    reads = [message for message in tool_results if 'unchanged' in str(message['content'])]
                    self.assertEqual(1, len(reads))
                    errors = [message for message in tool_results if message not in reads]
                    self.assertEqual(3, len(errors))
                    for error in errors:
                        self.assertRegex(str(error['content']).lower(), 'denied|not available|invalid|unknown')
                children = [session for session in observations['sessions'] if session.get('parentID')]
                self.assertEqual(1, len(children))
                child = children[0]
                parent = next(session for session in observations['sessions']
                              if session['id'] == child['parentID'])
                child_messages = observations['messages'][child['id']]
                child_assistants = [item['info'] for item in child_messages
                                    if item['info']['role'] == 'assistant']
                self.assertTrue(child_assistants)
                self.assertTrue(all(info['modelID'] == expected_model for info in child_assistants))
                self.assertTrue(all(info['variant'] == expected_effort for info in child_assistants))
                parent_text = json.dumps(observations['messages'][parent['id']])
                self.assertIn('CHILD_DENIALS_OBSERVED', parent_text)
                self.assertEqual('unchanged\n', (workspace / 'protected.txt').read_text())
                self.assertFalse((workspace / 'shell-executed').exists())
                tokens = read_token_budget(root / 'accounting')
                (root / 'token-summary.json').write_text(json.dumps(tokens, indent=2))
                self.assertTrue(tokens['usage_complete'], tokens)
                self.assertEqual(18 * len(provider.requests), tokens['used_tokens'])
                self.assertGreater(tokens['used_tokens'], 18 * provider.parent_step)


if __name__ == '__main__':
    unittest.main()
