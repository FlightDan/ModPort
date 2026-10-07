"""Persistent goal lifecycle tests with a deterministic OpenCode HTTP peer."""
import copy
import fcntl
from hashlib import sha256
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
from tempfile import TemporaryDirectory
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit

from modport.goal_runtime import _transport_config_identity, run_goal
from modport.opencode_runtime import OpenCodeCleanupError, OpenCodeResponseError, OpenCodeServer
from modport.process_diagnostics import CgroupMemoryEvents


class FakeOpenCode:
    instances = []
    sessions_by_root = {}
    behaviors = []
    connected = True
    transient_mcp_timeouts = 0
    mcp_calls = 0

    @classmethod
    def start(cls, **kwargs):
        server = cls(**kwargs)
        cls.instances.append(server)
        return server

    def __init__(self, *, cwd, config, xdg_root, deadline, lock_fd=None, **_kwargs):
        self.cwd = Path(cwd)
        self.config = config
        self.xdg_root = Path(xdg_root)
        self.deadline = deadline
        self.process = SimpleNamespace(pid=99999999)
        self.process_birth = None
        self.version = '1.18.32'
        self.executable = '/fixture/opencode'
        self.executable_sha256 = 'a' * 64
        self.stderr_path = str(self.xdg_root / 'server.stderr.log')
        self.close_options = None
        self.close_count = 0
        self.abort_calls = []
        self.send_calls = []
        self.key = str(self.xdg_root)
        self.db = self.sessions_by_root.setdefault(self.key, {'session': None})

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()

    def ownership_record(self):
        return {'pid': self.process.pid, 'birth': self.process_birth,
                'cwd': str(self.cwd), 'executable': self.executable}

    def events(self, *, cwd, deadline, stop_event):
        yield {'type': 'server.connected', 'properties': {}}
        while not stop_event.is_set() and time.monotonic() < deadline:
            stop_event.wait(.01)

    def mcp_status(self, *, cwd, deadline=None):
        type(self).mcp_calls += 1
        if type(self).transient_mcp_timeouts:
            type(self).transient_mcp_timeouts -= 1
            raise TimeoutError('fixture MCP HTTP probe timed out')
        if not self.connected:
            return {'modport_sandbox': {'status': 'failed', 'error': 'fixture'}}
        return {name: {'status': 'connected'}
                for name in self.config.to_dict().get('mcp', {})}

    def require_model(self, *, cwd, model, variant=None, deadline=None):
        if getattr(type(self), 'model_connected', True) is False:
            raise RuntimeError('OpenCode provider is not connected')
        provider, _, model_id = model.partition('/')
        return {'providerID': provider, 'modelID': model_id, 'variant': variant}

    def tool_ids(self, *, cwd, deadline=None):
        return ['read', 'edit', 'write', 'bash', 'task', 'patch', 'apply_patch']

    def create_session(self, *, cwd, title, model=None, variant=None, deadline=None, **_kwargs):
        self.db['session'] = {
            'id': 'ses-persistent-goal', 'title': title, 'directory': str(Path(cwd)),
            'messages': [],
        }
        return {key: value for key, value in self.db['session'].items() if key != 'messages'}

    def sessions(self, *, cwd, deadline=None):
        session = self.db.get('session')
        return ([{key: value for key, value in session.items() if key != 'messages'}]
                if session else [])

    def get_session(self, session_id, *, cwd=None, deadline=None):
        session = self.db.get('session')
        if not session or session['id'] != session_id:
            raise RuntimeError('session missing')
        return {key: value for key, value in session.items() if key != 'messages'}

    def messages(self, session_id, *, cwd=None, deadline=None):
        session = self.db.get('session')
        if not session or session['id'] != session_id:
            raise RuntimeError('session missing')
        return copy.deepcopy(session['messages'])

    def abort_session(self, session_id, *, cwd=None, deadline=None):
        self.abort_calls.append(session_id)
        return True

    def send_message(self, session_id, text, *, cwd=None, model=None, variant=None,
                     tools=None, output_format=None, deadline=None, message_id=None,
                     system=None, **_kwargs):
        self.send_calls.append({
            'session_id': session_id, 'text': text, 'tools': dict(tools or {}),
            'output_format': copy.deepcopy(output_format), 'message_id': message_id,
            'model': model, 'variant': variant, 'system': system,
        })
        behavior = (type(self).behaviors.pop(0) if type(self).behaviors else {})
        if isinstance(behavior, BaseException):
            raise behavior
        session = self.db['session']
        user = {'info': {'id': message_id, 'role': 'user'},
                'parts': [{'type': 'text', 'text': text}]}
        if behavior.get('store_user', True):
            session['messages'].append(user)
        response = None
        custom_responses = behavior.get('responses')
        if custom_responses:
            custom_responses = copy.deepcopy(custom_responses)
            for row in custom_responses:
                info = row.get('info') if isinstance(row, dict) else None
                if isinstance(info, dict) and info.get('parentID') == 'PENDING_USER_ID':
                    info['parentID'] = message_id
            session['messages'].extend(custom_responses)
            response = custom_responses[-1]
        elif behavior.get('store_assistant', True):
            response = behavior.get('response')
            if response is None:
                response = {
                    'info': {
                        'id': behavior.get('id', f"msg-assistant-{len(self.send_calls)}"),
                        'parentID': message_id, 'role': 'assistant',
                        'providerID': 'openai', 'modelID': 'gpt-6-luna',
                        'variant': variant, 'time': {'created': 100, 'completed': 1100},
                        'tokens': {'input': 5, 'output': 3, 'reasoning': 1,
                                   'cache': {'read': 2, 'write': 1}},
                    },
                    'parts': [{'type': 'text', 'text': behavior.get('text', 'work complete')}] +
                             behavior.get('parts', []),
                }
            session['messages'].append(response)
        if behavior.get('raise_after'):
            raise behavior['raise_after']
        if not behavior.get('store_assistant', True):
            raise behavior.get('raise_after', TimeoutError('response lost'))
        return copy.deepcopy(response or session['messages'][-1])

    def close(self, *, deadline_exceeded=False, cleanup_reason='host_cleanup'):
        self.close_count += 1
        self.close_options = {'deadline_exceeded': deadline_exceeded,
                              'cleanup_reason': cleanup_reason}
        return {
            'classification': 'host_requested_termination', 'returncode': -9,
            'signal_number': 9, 'attribution': 'not_applicable',
            'target_pid': self.process.pid, 'target_birth': None,
            'cgroup_before': None, 'cgroup_after': None, 'collection_errors': [],
            'host_requested_termination': True,
        }


class GoalRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.worktree = self.root / 'worktree' / 'candidate'
        self.worktree.mkdir(parents=True)
        self.command = SimpleNamespace(
            command_id='command', stage_id='coder',
            options={'model': 'gpt-6-luna', 'reasoning_effort': 'max'}, payload={},
        )
        FakeOpenCode.instances = []
        FakeOpenCode.sessions_by_root = {}
        FakeOpenCode.behaviors = []
        FakeOpenCode.connected = True
        FakeOpenCode.transient_mcp_timeouts = 0
        FakeOpenCode.mcp_calls = 0
        FakeOpenCode.model_connected = True
        self.patch = patch('modport.goal_runtime.OpenCodeServer', FakeOpenCode)
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def run_goal(self, validate=lambda: {'accepted': True, 'failures': [], 'evidence': {}} ,
                 **kwargs):
        timeout = kwargs.pop('timeout', 30)
        return run_goal(
            command=self.command, root=self.root, worktree=self.worktree,
            prompt='Complete contextual assignment', objective='Bounded objective',
            validate=validate, timeout=timeout, **kwargs)

    def test_accepted_candidate_persists_goal_session_usage_and_safe_mcp(self):
        result = self.run_goal()
        self.assertEqual(0, result.returncode, result.metadata)
        self.assertTrue(result.metadata['host_accepted'])
        self.assertEqual('accepted', result.metadata['status'])
        self.assertTrue(result.metadata['thread_id'].startswith('ses'))
        self.assertEqual('complete', result.metadata['native_goal_status'])
        self.assertEqual(12, result.metadata['tokens_used'])
        self.assertEqual(1, len(result.metadata['turns']))
        self.assertEqual('connected', result.metadata['mcp_status']['modport_sandbox'])
        server = FakeOpenCode.instances[0]
        effective = server.config.to_dict()
        self.assertFalse(effective['tools']['bash'])
        self.assertFalse(effective['tools']['shell'])
        self.assertEqual('deny', effective['permission']['bash'])
        self.assertEqual('deny', effective['permission']['external_directory'])
        self.assertEqual('allow', effective['permission']['modport_sandbox_read_run_artifact'])
        self.assertIn('modport_sandbox', effective['mcp'])
        self.assertIn('modport.opencode_shell_mcp', ' '.join(
            effective['mcp']['modport_sandbox']['command']))
        self.assertEqual(1, server.close_count)
        state = json.loads((self.root / result.metadata['state_path']).read_text())
        self.assertEqual(result.metadata['thread_id'], state['thread_id'])
        self.assertEqual(0o600, (self.root / result.metadata['state_path']).stat().st_mode & 0o777)
        audit = [json.loads(row) for row in
                 (self.root / result.metadata['events_path']).read_text().splitlines()]
        self.assertTrue(any(row['event']['method'] == 'modport/goal/turn_completed'
                            for row in audit))

    def test_current_goal_exposes_native_helpers_with_frozen_coder_selection(self):
        from modport.workflow import WORKFLOW_VERSION
        from modport.opencode_subagents import TASK_PERMISSION
        self.command.options.update(workflow_version=WORKFLOW_VERSION, goal_read_only=True,
            model_policy={'default': {'model': 'gpt-6-luna', 'reasoning_effort': 'max'},
                          'roles': {'coder': {'model': 'gpt-6.1-sol', 'reasoning_effort': 'high'}},
                          'stages': {}})
        result = self.run_goal()
        self.assertEqual(0, result.returncode, result.metadata)
        server = FakeOpenCode.instances[0]
        effective = server.config.to_dict()
        self.assertEqual(TASK_PERMISSION, effective['permission']['task'])
        self.assertNotEqual(False, effective['tools'].get('task'))
        self.assertNotEqual(False, server.send_calls[0]['tools'].get('task'))
        self.assertNotIn('model', effective['agent']['general'])
        self.assertNotIn('variant', effective['agent']['general'])
        self.assertEqual('openai/gpt-6.1-sol', server.send_calls[0]['model'])
        self.assertEqual('high', server.send_calls[0]['variant'])
        self.assertEqual('deny', effective['agent']['general']['permission']['edit'])

    def test_host_network_deny_applies_before_goal_turn_and_preserves_artifact_reads(self):
        self.command.options['workflow_version'] = 25
        with patch.dict(os.environ, {'MODPORT_DENY_NETWORK_TOOLS': '1'}):
            result = self.run_goal()
        self.assertEqual(0, result.returncode, result.metadata)
        server = FakeOpenCode.instances[0]
        effective = server.config.to_dict()
        for name in ('modport_sandbox_run_project_command', 'webfetch', 'websearch'):
            self.assertFalse(effective['tools'][name])
            self.assertEqual('deny', effective['permission'][name])
            self.assertFalse(server.send_calls[0]['tools'][name])
        self.assertEqual('allow', effective['permission']['modport_sandbox_read_run_artifact'])
        self.assertNotEqual(False, server.send_calls[0]['tools'].get('edit'))
        descriptor = (self.root / 'artifacts' / 'executions' / 'command'
                      / 'opencode-shell' / 'session.json')
        self.assertIs(json.loads(descriptor.read_text())['allow_project_command'], False)
        self.command.options['native_goal_resume'] = True
        reopened = self.run_goal()
        self.assertEqual(1, reopened.returncode)
        self.assertEqual('native_goal_resume_identity_mismatch', reopened.metadata['error'])

    def test_v25_legacy_default_allow_goal_resumes_without_network_policy_field(self):
        self.command.options['workflow_version'] = 25
        FakeOpenCode.behaviors = [KeyboardInterrupt(), {'text': 'resumed'}]
        with patch.dict(os.environ, {'MODPORT_DENY_NETWORK_TOOLS': '0'}):
            first = self.run_goal()
        self.assertEqual('paused', first.metadata['native_goal_status'])
        state_path = self.root / first.metadata['state_path']
        state = json.loads(state_path.read_text(encoding='utf-8'))
        self.assertIs(state.pop('network_tools_disabled'), False)
        state_path.write_text(json.dumps(state), encoding='utf-8')
        self.command.options['native_goal_resume'] = True
        with patch.dict(os.environ, {'MODPORT_DENY_NETWORK_TOOLS': '0'}):
            resumed = self.run_goal()
        self.assertEqual(0, resumed.returncode, resumed.metadata)
        self.assertEqual(first.metadata['thread_id'], resumed.metadata['thread_id'])

    def test_transient_mcp_http_timeout_does_not_timeout_goal_before_deadline(self):
        FakeOpenCode.transient_mcp_timeouts = 1
        result = self.run_goal()
        self.assertEqual(0, result.returncode, result.metadata)
        self.assertEqual('accepted', result.metadata['status'])
        self.assertEqual('connected', result.metadata['mcp_status']['modport_sandbox'])
        self.assertGreaterEqual(FakeOpenCode.mcp_calls, 2)
        self.assertFalse(FakeOpenCode.instances[0].close_options['deadline_exceeded'])

    def test_persistent_mcp_http_timeouts_end_at_actual_goal_deadline(self):
        FakeOpenCode.transient_mcp_timeouts = 100
        result = self.run_goal(timeout=0.05)
        self.assertNotEqual(0, result.returncode)
        self.assertEqual('timed_out', result.metadata['status'])
        self.assertEqual('configured OpenCode MCP connection deadline exhausted',
                         result.metadata['error'])
        self.assertTrue(FakeOpenCode.instances[0].close_options['deadline_exceeded'])

    def test_long_utf8_objective_keeps_full_prompt_and_bounded_goal_identity(self):
        self.command.options['workflow_version'] = 20
        prompt = 'Keep this assigned context.'
        objective = '完整目标约束。' * 900
        result = run_goal(
            command=self.command, root=self.root, worktree=self.worktree,
            prompt=prompt, objective=objective,
            validate=lambda: {'accepted': True, 'failures': [], 'evidence': {}},
            timeout=30)

        self.assertEqual(0, result.returncode, result.metadata)
        sent_prompt = FakeOpenCode.instances[0].send_calls[0]['text']
        state_path = self.root / result.metadata['state_path']
        state = json.loads(state_path.read_text(encoding='utf-8'))
        objective_digest = sha256(objective.encode('utf-8')).hexdigest()
        self.assertEqual(objective, result.metadata['objective'])
        self.assertLessEqual(len(result.metadata['native_objective'].encode('utf-8')), 4000)
        self.assertIn(objective_digest, result.metadata['native_objective'])
        self.assertIn(prompt, sent_prompt)
        self.assertIn(objective, sent_prompt)
        self.assertEqual(sha256(sent_prompt.encode('utf-8')).hexdigest(),
                         result.metadata['execution_prompt_sha256'])
        self.assertEqual(objective, state['objective'])
        self.assertEqual(result.metadata['native_objective'], state['native_objective'])

    def test_exact_objective_limit_preserves_unmodified_goal_and_prompt(self):
        self.command.options['workflow_version'] = 25
        prompt = 'Keep this assigned context.'
        objective = 'a' * 4000
        result = run_goal(
            command=self.command, root=self.root, worktree=self.worktree,
            prompt=prompt, objective=objective,
            validate=lambda: {'accepted': True, 'failures': [], 'evidence': {}},
            timeout=30)

        self.assertEqual(0, result.returncode, result.metadata)
        self.assertEqual(objective, result.metadata['objective'])
        self.assertNotIn('native_objective', result.metadata)
        self.assertEqual(prompt, FakeOpenCode.instances[0].send_calls[0]['text'])

    def test_long_objective_without_saved_identity_cannot_resume(self):
        self.command.options['workflow_version'] = 20
        prompt = 'Keep this assigned context.'
        objective = '完整目标约束。' * 900
        first = run_goal(
            command=self.command, root=self.root, worktree=self.worktree,
            prompt=prompt, objective=objective,
            validate=lambda: {'accepted': True, 'failures': [], 'evidence': {}},
            timeout=30)
        self.assertEqual(0, first.returncode, first.metadata)

        state_path = self.root / first.metadata['state_path']
        state = json.loads(state_path.read_text(encoding='utf-8'))
        state.pop('native_objective')
        state_path.write_text(json.dumps(state), encoding='utf-8')
        self.command.options['native_goal_resume'] = True
        resumed = run_goal(
            command=self.command, root=self.root, worktree=self.worktree,
            prompt=prompt, objective=objective,
            validate=lambda: {'accepted': True, 'failures': [], 'evidence': {}},
            timeout=30)

        self.assertEqual('native_goal_resume_identity_mismatch', resumed.metadata['error'])
        self.assertEqual(1, len(FakeOpenCode.instances))

    def test_turn_ledger_aggregates_intermediate_tool_messages_from_transcript(self):
        self.command.options['workflow_version'] = 20
        intermediate = {
            'info': {
                'id': 'msg-intermediate-tool', 'parentID': 'PENDING_USER_ID',
                'role': 'assistant', 'providerID': 'openai', 'modelID': 'gpt-6-luna',
                'variant': 'max', 'finish': 'tool-calls',
                'time': {'created': 1000, 'completed': 1200},
                'tokens': {'input': 4, 'output': 2, 'reasoning': 1,
                           'cache': {'read': 3, 'write': 1}},
            },
            'parts': [
                {'type': 'tool', 'callID': 'call-1',
                 'tool': 'modport_sandbox_run_project_command',
                 'state': {'status': 'completed', 'input': {'argv': ['git', 'status']},
                           'output': 'exit 0'}},
            ],
        }
        final = {
            'info': {
                'id': 'msg-final-text', 'parentID': 'PENDING_USER_ID',
                'role': 'assistant', 'providerID': 'openai', 'modelID': 'gpt-6-luna',
                'variant': 'max', 'finish': 'stop',
                'time': {'created': 1300, 'completed': 1500},
                'tokens': {'input': 5, 'output': 3, 'reasoning': 1,
                           'cache': {'read': 2, 'write': 1}},
            },
            'parts': [{'type': 'text', 'text': 'The safe command completed.'}],
        }
        behavior = {'responses': [intermediate, final]}
        original_send = FakeOpenCode.send_message

        def bind_parent_id(server, session_id, text, **kwargs):
            message_id = kwargs['message_id']
            behavior['responses'][0]['info']['parentID'] = message_id
            behavior['responses'][1]['info']['parentID'] = message_id
            FakeOpenCode.behaviors = [behavior]
            return original_send(server, session_id, text, **kwargs)

        with patch.object(FakeOpenCode, 'send_message', bind_parent_id):
            result = self.run_goal()

        self.assertEqual(0, result.returncode, result.metadata)
        turn = result.metadata['turns'][0]
        self.assertEqual('msg-final-text', turn['id'])
        self.assertEqual(23, result.metadata['tokens_used'])
        self.assertEqual('modport_sandbox_run_project_command',
                         turn['tool_calls'][0]['tool'])
        self.assertEqual('completed', turn['tool_calls'][0]['status'])
        progress = result.metadata['progress_observations']
        self.assertEqual(1, progress['tool_completions'])
        self.assertEqual('The safe command completed.',
                         result.metadata['public_messages'][0])

    def test_rejected_turn_continues_same_session_with_exact_validation_feedback(self):
        self.command.options['workflow_version'] = 15
        self.command.payload = {'request': {'budget': {'max_rework_rounds': 2}}}
        FakeOpenCode.behaviors = [{'text': 'first candidate'}, {'text': 'repaired candidate'}]
        verdicts = iter([
            {'accepted': False, 'failures': ['runtime evidence missing'],
             'evidence': {'digest': 'first'}},
            {'accepted': True, 'failures': [], 'evidence': {'digest': 'second'}},
        ])
        result = self.run_goal(lambda: next(verdicts))
        self.assertEqual(0, result.returncode, result.metadata)
        self.assertEqual(2, len(result.metadata['turns']))
        self.assertEqual(1, result.metadata['rejected_completions'])
        calls = FakeOpenCode.instances[0].send_calls
        self.assertEqual(calls[0]['session_id'], calls[1]['session_id'])
        self.assertIn('runtime evidence missing', calls[1]['text'])
        self.assertIn('Complete contextual assignment', calls[1]['text'])

    def test_v15_rework_budget_stops_after_configured_rejections(self):
        self.command.options['workflow_version'] = 15
        self.command.payload = {'request': {'budget': {'max_rework_rounds': 1}}}
        FakeOpenCode.behaviors = [{'text': 'one'}, {'text': 'two'}]
        result = self.run_goal(lambda: {'accepted': False, 'failures': ['same check']})
        self.assertEqual(1, result.returncode)
        self.assertEqual('native_goal_rework_rounds_exhausted', result.metadata['stop_reason'])
        self.assertEqual(2, result.metadata['rejected_completions'])
        self.assertEqual(2, len(FakeOpenCode.instances[0].send_calls))

    def test_v17_validation_is_diagnostic_and_candidate_capture_stops_server_first(self):
        self.command.options.update(workflow_version=17, host_collect_candidate=True)
        FakeOpenCode.behaviors = [{'text': 'candidate'}]

        def validate():
            self.assertEqual(1, FakeOpenCode.instances[0].close_count)
            return {'accepted': False, 'failures': ['diagnostic only'], 'evidence': {}}

        result = self.run_goal(validate)
        self.assertEqual(0, result.returncode)
        self.assertFalse(result.metadata['host_accepted'])
        self.assertEqual('completed_with_diagnostics', result.metadata['status'])
        self.assertTrue(result.metadata['producer_stopped'])

    def test_candidate_capture_rejects_unconfirmed_server_cleanup(self):
        self.command.options.update(workflow_version=17, host_collect_candidate=True)
        FakeOpenCode.behaviors = [{'text': 'candidate'}]
        with patch.object(FakeOpenCode, 'close', return_value={
                'classification': 'unknown', 'returncode': None,
                'cleanup_confirmed': False, 'target_pid': 99999999}):
            result = self.run_goal(lambda: self.fail('validation raced an active producer'))
        self.assertEqual(1, result.returncode)
        self.assertEqual('failed', result.metadata['status'])
        self.assertFalse(result.metadata['producer_stopped'])
        self.assertFalse(result.metadata['host_accepted'])
        self.assertEqual('native_goal_producer_cleanup_unconfirmed',
                         result.metadata['stop_reason'])

    def test_final_cleanup_cannot_leave_an_accepted_goal_with_live_producer(self):
        with patch.object(FakeOpenCode, 'close', return_value={
                'classification': 'unknown', 'returncode': None,
                'cleanup_confirmed': False, 'target_pid': 99999999}):
            result = self.run_goal()
        self.assertEqual(1, result.returncode)
        self.assertEqual('failed', result.metadata['status'])
        self.assertEqual('failed', result.metadata['native_goal_status'])
        self.assertEqual('failed', result.metadata['native_goal']['status'])
        self.assertFalse(result.metadata['host_accepted'])
        self.assertFalse(result.metadata['producer_stopped'])
        self.assertTrue(result.metadata['cleanup_unconfirmed'])

    def test_failed_startup_persists_unconfirmed_producer_without_private_detail(self):
        with patch.object(FakeOpenCode, 'start', side_effect=OpenCodeCleanupError({
                'cleanup_confirmed': False, 'target_pid': 900003,
                'detail': 'private provider message'})):
            result = self.run_goal()
        self.assertEqual(1, result.returncode)
        self.assertEqual('failed', result.metadata['status'])
        self.assertFalse(result.metadata['producer_stopped'])
        self.assertTrue(result.metadata['cleanup_unconfirmed'])
        self.assertEqual(900003, result.metadata['process_diagnostics'][-1]['target_pid'])
        self.assertNotIn('private provider message', json.dumps(result.metadata))

    def test_host_candidate_validation_can_finish_after_model_deadline(self):
        self.command.options.update(
            workflow_version=17,
            host_collect_candidate=True,
            host_settlement_deadline_epoch=1050,
        )
        FakeOpenCode.behaviors = [{'text': 'candidate'}]
        clock = SimpleNamespace(now=1000.0)
        original_send = FakeOpenCode.send_message

        def finish_after_model_deadline(server, *args, **kwargs):
            response = original_send(server, *args, **kwargs)
            clock.now = 1031.0
            return response

        def validate():
            self.assertGreater(clock.now, 1030.0)
            self.assertEqual(1, FakeOpenCode.instances[0].close_count)
            return {'accepted': False, 'failures': ['diagnostic'], 'evidence': {}}

        with patch('modport.goal_runtime.time.time', side_effect=lambda: clock.now), \
                patch('modport.goal_runtime.time.monotonic', side_effect=lambda: clock.now), \
                patch.object(FakeOpenCode, 'send_message', finish_after_model_deadline):
            result = self.run_goal(validate)

        self.assertEqual(0, result.returncode, result.metadata)
        self.assertEqual('completed_with_diagnostics', result.metadata['status'])
        self.assertEqual(1030.0, result.metadata['deadline_epoch'])
        self.assertEqual(1050.0, result.metadata['host_deadline_epoch'])

    def test_downstream_toolcall_rejection_is_returned_without_automatic_retry(self):
        self.command.options['gate_policy'] = 'downstream_toolcall'
        result = self.run_goal(lambda: {'accepted': False, 'failures': ['check failed']})
        self.assertEqual(1, result.returncode)
        self.assertTrue(result.metadata['downstream_rework_required'])
        self.assertEqual('native_goal_validation_rejected', result.metadata['stop_reason'])
        self.assertEqual(1, len(FakeOpenCode.instances[0].send_calls))

    def test_explicit_blocked_marker_is_not_host_acceptance(self):
        FakeOpenCode.behaviors = [{'text': 'Cannot proceed.\nMODPORT_GOAL_STATUS: blocked'}]
        result = self.run_goal(lambda: self.fail('blocked goal cannot be accepted'))
        self.assertEqual(1, result.returncode)
        self.assertFalse(result.metadata['host_accepted'])
        self.assertEqual('blocked', result.metadata['status'])
        self.assertEqual('blocked', result.metadata['native_goal_status'])

    def test_cancel_aborts_and_explicit_resume_reuses_same_request_identity(self):
        FakeOpenCode.behaviors = [KeyboardInterrupt(), {'text': 'resumed'}]
        first = self.run_goal()
        self.assertEqual('cancelled', first.metadata['status'])
        self.assertEqual('paused', first.metadata['native_goal_status'])
        self.assertEqual(1, len(FakeOpenCode.instances[0].abort_calls))
        self.command.options['native_goal_resume'] = True
        resumed = self.run_goal()
        self.assertEqual(0, resumed.returncode, resumed.metadata)
        self.assertEqual(first.metadata['thread_id'], resumed.metadata['thread_id'])
        self.assertEqual(2, len(resumed.metadata['attempts']))
        calls = [call for server in FakeOpenCode.instances for call in server.send_calls]
        self.assertEqual(calls[0]['message_id'], calls[1]['message_id'])

    def test_lost_http_response_reconciles_completed_transcript_without_duplicate_send(self):
        FakeOpenCode.behaviors = [{'text': 'committed before disconnect',
                                   'raise_after': TimeoutError('response lost')}]
        first = self.run_goal()
        self.assertEqual('timed_out', first.metadata['status'])
        self.assertEqual('budgetLimited', first.metadata['native_goal_status'])
        self.command.options['native_goal_resume'] = True
        resumed = self.run_goal()
        self.assertEqual(0, resumed.returncode, resumed.metadata)
        all_calls = [call for server in FakeOpenCode.instances for call in server.send_calls]
        self.assertEqual(1, len(all_calls))
        self.assertEqual('committed before disconnect', resumed.metadata['public_messages'][0])

    def test_truncated_tcp_response_reconciles_completed_turn_without_duplicate_send(self):
        side_effect = self.root / 'remote-effect.txt'
        peer = {'posts': [], 'gets': [], 'messages': []}
        worktree = str(self.worktree)

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def do_POST(self):
                self.assert_path('/session/ses-persistent-goal/message')
                body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                user_id = body['messageID']
                peer['posts'].append(user_id)
                side_effect.write_text(side_effect.read_text() + 'effect\n'
                                       if side_effect.exists() else 'effect\n')
                peer['messages'] = [
                    {'info': {'id': user_id, 'role': 'user'}, 'parts': body['parts']},
                    {'info': {'id': 'msg-completed-after-eof', 'parentID': user_id,
                              'role': 'assistant', 'providerID': 'openai',
                              'modelID': 'gpt-6-luna', 'variant': 'max',
                              'time': {'created': 100, 'completed': 1100},
                              'tokens': {'input': 5, 'output': 3, 'reasoning': 1,
                                         'cache': {'read': 2, 'write': 1}}},
                     'parts': [{'type': 'text', 'text': 'committed before TCP EOF'}]},
                ]
                response = json.dumps(peer['messages'][-1]).encode()
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(response) + 20))
                self.end_headers()
                self.wfile.write(response[:8])
                self.wfile.flush()
                self.connection.shutdown(socket.SHUT_WR)

            def do_GET(self):
                path = urlsplit(self.path).path
                peer['gets'].append(path)
                if path == '/session/ses-persistent-goal':
                    value = {'id': 'ses-persistent-goal', 'directory': worktree}
                elif path == '/session/ses-persistent-goal/message':
                    value = peer['messages']
                else:
                    self.send_error(404)
                    return
                body = json.dumps(value).encode()
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def assert_path(self, expected):
                if urlsplit(self.path).path != expected:
                    self.send_error(404)
                    raise ValueError('unexpected local peer path')

        try:
            server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        except PermissionError as exc:
            self.skipTest(f'loopback socket denied by sandbox: {exc}')
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(lambda: (server.shutdown(), thread.join(2), server.server_close()))

        class HTTPBodyEOFOpenCode(FakeOpenCode):
            base_url_for_test = f'http://127.0.0.1:{server.server_port}'
            _deadline_timeout = OpenCodeServer._deadline_timeout
            _auth_header = OpenCodeServer._auth_header
            _directory = OpenCodeServer._directory
            _request = OpenCodeServer._request
            send_message = OpenCodeServer.send_message
            messages = OpenCodeServer.messages
            get_session = OpenCodeServer.get_session

            def __init__(self, **kwargs):
                super().__init__(**kwargs)
                self.base_url = self.base_url_for_test
                self.env = {'OPENCODE_SERVER_USERNAME': 'modport',
                            'OPENCODE_SERVER_PASSWORD': ''}
                self._session_directories = {}

        with patch('modport.goal_runtime.OpenCodeServer', HTTPBodyEOFOpenCode):
            first = self.run_goal()
            self.assertEqual(1, first.returncode)
            self.assertEqual('failed', first.metadata['status'])
            self.assertIn('response body ended before completion',
                          first.metadata['error'])
            self.assertEqual(1, len(peer['posts']))
            self.assertIn('pending_turn', first.metadata)
            self.command.options['native_goal_resume'] = True
            resumed = self.run_goal()
        self.assertEqual(0, resumed.returncode, resumed.metadata)
        self.assertEqual(first.metadata['thread_id'], resumed.metadata['thread_id'])
        self.assertEqual(first.metadata['deadline_epoch'], resumed.metadata['deadline_epoch'])
        self.assertEqual(1, len(peer['posts']))
        self.assertEqual(['effect'], side_effect.read_text().splitlines())
        self.assertIn('/session/ses-persistent-goal', peer['gets'])
        self.assertIn('/session/ses-persistent-goal/message', peer['gets'])
        self.assertEqual('committed before TCP EOF', resumed.metadata['public_messages'][0])

    def test_incomplete_request_is_aborted_then_recovered_with_new_message_id(self):
        FakeOpenCode.behaviors = [{
            'responses': [{
                'info': {'id': 'msg-tool-step', 'parentID': 'PENDING_USER_ID',
                         'role': 'assistant', 'finish': 'tool-calls',
                         'time': {'created': 100, 'completed': 200}},
                'parts': [{'type': 'tool', 'callID': 'call-effect',
                           'tool': 'modport_sandbox_run_project_command',
                           'state': {'status': 'completed', 'input': {'argv': ['touch', 'x']},
                                     'output': 'exit 0'}}],
            }],
            'raise_after': TimeoutError('response lost'),
        },
                                  {'text': 'continued after reconciliation'}]
        first = self.run_goal()
        self.assertEqual('timed_out', first.metadata['status'])
        self.command.options['native_goal_resume'] = True
        resumed = self.run_goal()
        self.assertEqual(0, resumed.returncode, resumed.metadata)
        servers = FakeOpenCode.instances
        self.assertEqual(1, len(servers[1].abort_calls))
        calls = [call for server in servers for call in server.send_calls]
        self.assertNotEqual(calls[0]['message_id'], calls[1]['message_id'])
        self.assertIn('Do not repeat a tool call', calls[1]['text'])
        self.assertEqual(1, len(resumed.metadata['interrupted_turns']))

    def test_provider_disconnect_fails_before_session_or_model_message(self):
        FakeOpenCode.model_connected = False
        result = self.run_goal()
        self.assertEqual(1, result.returncode)
        self.assertIn('not connected', result.metadata['error'])
        self.assertIsNone(result.metadata['thread_id'])
        self.assertEqual([], FakeOpenCode.instances[0].send_calls)

    def test_provider_error_redacts_nonpattern_api_key_from_persisted_metadata(self):
        secret = 'opaque-openai-key-value-12345'
        provider_error = OpenCodeResponseError(
            {'name': f'AuthenticationError {secret}',
             'data': {'message': f'provider rejected credential {secret}'}},
            {'info': {'error': {'name': f'AuthenticationError {secret}'}}},
        )
        FakeOpenCode.behaviors = [provider_error]

        with patch.dict(os.environ, {'OPENAI_API_KEY': secret}):
            result = self.run_goal()

        self.assertEqual(1, result.returncode)
        self.assertEqual('failed', result.metadata['status'])
        self.assertEqual('AuthenticationError [REDACTED]',
                         result.metadata['pending_response_error']['name'])
        self.assertIn('[REDACTED]', result.metadata['pending_response_error']['message'])
        self.assertIn('OpenCode response AuthenticationError [REDACTED]:',
                      result.metadata['error'])
        self.assertNotIn(secret, json.dumps(result.metadata))
        state = (self.root / result.metadata['state_path']).read_text(encoding='utf-8')
        self.assertNotIn(secret, state)

    def test_recovered_provider_error_redacts_nonpattern_api_key(self):
        secret = 'opaque-openai-key-value-12345'
        FakeOpenCode.behaviors = [
            {'store_assistant': False, 'raise_after': TimeoutError('response lost')},
        ]
        with patch.dict(os.environ, {'OPENAI_API_KEY': secret}):
            first = self.run_goal()
            pending_id = first.metadata['pending_turn']['message_id']
            FakeOpenCode.instances[0].db['session']['messages'].append({
                'info': {
                    'id': 'msg-provider-error', 'parentID': pending_id,
                    'role': 'assistant',
                    'error': {'name': f'AuthenticationError {secret}',
                              'data': {'message': f'provider rejected credential {secret}'}},
                },
                'parts': [],
            })
            self.command.options['native_goal_resume'] = True
            FakeOpenCode.behaviors = [{'text': 'recovered'}]
            resumed = self.run_goal()

        self.assertEqual(0, resumed.returncode, resumed.metadata)
        self.assertEqual('AuthenticationError [REDACTED]',
                         resumed.metadata['response_errors'][0]['name'])
        self.assertIn('[REDACTED]', resumed.metadata['response_errors'][0]['message'])
        self.assertNotIn(secret, json.dumps(resumed.metadata))
        state = (self.root / resumed.metadata['state_path']).read_text(encoding='utf-8')
        self.assertNotIn(secret, state)

    def test_advisory_validation_and_cleanup_diagnostics_redact_env_secret(self):
        secret = 'opaque-openai-key-value-12345'
        self.command.options.update(workflow_version=17, host_collect_candidate=True)
        def failed_close(server, **_kwargs):
            raise RuntimeError(f'cleanup exposed {secret}')

        def failed_validation():
            raise ValueError(f'validation exposed {secret}')

        with patch.dict(os.environ, {'OPENAI_API_KEY': secret}), \
                patch.object(FakeOpenCode, 'close', failed_close):
            result = self.run_goal(failed_validation)

        self.assertEqual(1, result.returncode, result.metadata)
        self.assertEqual([], result.metadata['validation_records'])
        self.assertFalse(result.metadata['producer_stopped'])
        self.assertEqual('native_goal_producer_cleanup_unconfirmed',
                         result.metadata['stop_reason'])
        self.assertIn('[REDACTED]',
                      result.metadata['process_diagnostics'][0]['detail'])
        self.assertNotIn(secret, json.dumps(result.metadata))
        self.assertNotIn(secret, (self.root / result.metadata['state_path']).read_text())
        self.assertNotIn(secret, (self.root / result.metadata['events_path']).read_text())

    def test_configured_mcp_failure_is_fail_closed_before_model_turn(self):
        FakeOpenCode.connected = False
        result = self.run_goal()
        self.assertEqual(1, result.returncode)
        self.assertIn('failed to connect', result.metadata['error'])
        self.assertEqual([], FakeOpenCode.instances[0].send_calls)

    def test_plan_and_execute_share_session_and_execution_schema_is_forwarded(self):
        schema = {'type': 'json_schema', 'schema': {'type': 'object'}}
        FakeOpenCode.behaviors = [{'text': 'Plan facts from the workspace'},
                                  {'text': 'execution complete'}]
        plan_path = self.root / 'artifacts' / 'goal-plan.json'
        rework = {'modport_rework': {'type': 'local', 'command': ['/usr/bin/python3', 'mcp.py'],
                                     'enabled': True, 'timeout': 20000}}
        result = self.run_goal(
            planning_prompt='Plan the work', plan_path=plan_path,
            transport_args=rework, output_format=schema,
        )
        self.assertEqual(0, result.returncode, result.metadata)
        self.assertEqual('Plan facts from the workspace\n', plan_path.read_text())
        calls = FakeOpenCode.instances[0].send_calls
        self.assertEqual(2, len(calls))
        self.assertEqual(calls[0]['session_id'], calls[1]['session_id'])
        self.assertEqual('Plan the work', calls[0]['text'])
        self.assertEqual(schema, calls[1]['output_format'])
        self.assertFalse(calls[0]['tools']['modport_sandbox_run_project_command'])
        self.assertFalse(calls[0]['tools']['modport_rework_request_rework'])
        self.assertFalse(calls[0]['tools']['modport_rework_list_rework_targets'])
        self.assertTrue(calls[1]['tools']['modport_sandbox_run_project_command'])
        self.assertTrue(calls[1]['tools']['modport_rework_request_rework'])

    def test_v25_context_budget_blocks_execute_and_rechecks_on_resume(self):
        self.command.options['workflow_version'] = 25
        plan_path = self.root / 'artifacts' / 'goal-plan.md'
        budget = {'context_window': 400, 'input_token_budget': 280,
                  'output_token_reserve': 80, 'tool_token_reserve': 40}
        first = self.run_goal(planning_prompt='Plan the work', plan_path=plan_path,
                              session_context_budget=budget)
        self.assertEqual(1, first.returncode)
        self.assertIn('context preflight exceeded', first.metadata['error'])
        self.assertEqual('work complete\n', plan_path.read_text())
        self.assertEqual(1, len(FakeOpenCode.instances[0].send_calls))
        self.assertEqual('final_assistant_usage',
                         first.metadata['planning_turns'][0]['context_usage']['source'])
        self.command.options['native_goal_resume'] = True
        resumed = self.run_goal(planning_prompt='Plan the work', plan_path=plan_path,
                                session_context_budget=budget)
        self.assertEqual(1, resumed.returncode)
        self.assertIn('context preflight exceeded', resumed.metadata['error'])
        self.assertEqual(0, len(FakeOpenCode.instances[-1].send_calls))

    def test_v25_context_budget_allows_execute(self):
        self.command.options['workflow_version'] = 25
        budget = {'context_window': 10000, 'input_token_budget': 7000,
                  'output_token_reserve': 2000, 'tool_token_reserve': 1000}
        result = self.run_goal(planning_prompt='Plan the work',
                               plan_path=self.root / 'artifacts' / 'goal-plan.md',
                               session_context_budget=budget)
        self.assertEqual(0, result.returncode, result.metadata)
        self.assertEqual(2, len(FakeOpenCode.instances[0].send_calls))
        self.assertIn('context_preflight', result.metadata)

    def test_v25_context_budget_uses_final_step_not_summed_turn_usage(self):
        self.command.options['workflow_version'] = 25
        FakeOpenCode.behaviors = [{'responses': [
            {'info': {'id': 'msg-plan-tool', 'parentID': 'PENDING_USER_ID',
                      'role': 'assistant', 'time': {'created': 100, 'completed': 200},
                      'tokens': {'input': 200, 'output': 20}},
             'parts': [{'type': 'tool', 'tool': 'read', 'callID': 'call-plan',
                        'state': {'status': 'completed', 'output': 'source fact'}}]},
            {'info': {'id': 'msg-plan-final', 'parentID': 'PENDING_USER_ID',
                      'role': 'assistant', 'time': {'created': 201, 'completed': 300},
                      'tokens': {'input': 220, 'output': 10}},
             'parts': [{'type': 'text', 'text': 'Use source fact'}]},
        ]}]
        budget = {'context_window': 820, 'input_token_budget': 570,
                  'output_token_reserve': 170, 'tool_token_reserve': 80}
        result = self.run_goal(planning_prompt='Plan the work',
                               plan_path=self.root / 'artifacts' / 'goal-plan.md',
                               session_context_budget=budget)
        self.assertEqual(0, result.returncode, result.metadata)
        self.assertEqual(2, len(FakeOpenCode.instances[0].send_calls))
        self.assertEqual(450, result.metadata['planning_turns'][0]['tokens']['input']
                         + result.metadata['planning_turns'][0]['tokens']['output'])
        self.assertEqual(230, result.metadata['planning_turns'][0]['context_usage']['tokens'])

    def test_v25_partial_execution_is_counted_before_recovery_send(self):
        self.command.options['workflow_version'] = 25
        partial = {'responses': [{
            'info': {'id': 'msg-partial-execute', 'parentID': 'PENDING_USER_ID',
                     'role': 'assistant', 'finish': 'tool-calls',
                     'time': {'created': 100, 'completed': 200}},
            'parts': [{'type': 'tool', 'tool': 'read', 'callID': 'call-large',
                       'state': {'status': 'completed', 'output': 'x' * 2000}}],
        }], 'raise_after': TimeoutError('response lost')}
        FakeOpenCode.behaviors = [{'text': 'Plan the work'}, partial]
        budget = {'context_window': 1800, 'input_token_budget': 1200,
                  'output_token_reserve': 400, 'tool_token_reserve': 200}
        plan = self.root / 'artifacts' / 'goal-plan.md'
        first = self.run_goal(planning_prompt='Plan the work', plan_path=plan,
                              session_context_budget=budget)
        self.assertEqual('timed_out', first.metadata['status'])
        self.assertEqual(2, len(FakeOpenCode.instances[0].send_calls))
        self.command.options['native_goal_resume'] = True
        resumed = self.run_goal(planning_prompt='Plan the work', plan_path=plan,
                                session_context_budget=budget)
        self.assertEqual(1, resumed.returncode)
        self.assertIn('context preflight exceeded', resumed.metadata['error'])
        self.assertEqual(0, len(FakeOpenCode.instances[-1].send_calls))
        self.assertGreater(resumed.metadata['interrupted_context_usage']['tokens'], 2000)

    def test_v25_partial_plan_is_counted_before_plan_retry(self):
        self.command.options['workflow_version'] = 25
        FakeOpenCode.behaviors = [{'responses': [{
            'info': {'id': 'msg-partial-plan', 'parentID': 'PENDING_USER_ID',
                     'role': 'assistant', 'finish': 'tool-calls',
                     'time': {'created': 100, 'completed': 200}},
            'parts': [{'type': 'tool', 'tool': 'read', 'callID': 'call-large',
                       'state': {'status': 'completed', 'output': 'x' * 2000}}],
        }], 'raise_after': TimeoutError('response lost')}]
        budget = {'context_window': 1800, 'input_token_budget': 1200,
                  'output_token_reserve': 400, 'tool_token_reserve': 200}
        plan = self.root / 'artifacts' / 'goal-plan.md'
        first = self.run_goal(planning_prompt='Plan the work', plan_path=plan,
                              session_context_budget=budget)
        self.assertEqual('timed_out', first.metadata['status'])
        self.command.options['native_goal_resume'] = True
        resumed = self.run_goal(planning_prompt='Plan the work', plan_path=plan,
                                session_context_budget=budget)
        self.assertEqual(1, resumed.returncode)
        self.assertIn('context preflight exceeded', resumed.metadata['error'])
        self.assertEqual(0, len(FakeOpenCode.instances[-1].send_calls))

    def test_resume_requires_explicit_recovery_and_same_goal_identity(self):
        FakeOpenCode.behaviors = [KeyboardInterrupt()]
        first = self.run_goal()
        replay = self.run_goal()
        self.assertEqual('native_goal_replay_requires_explicit_recovery', replay.metadata['error'])
        self.assertEqual(1, len(FakeOpenCode.instances))
        self.command.options['native_goal_resume'] = True
        changed = run_goal(command=self.command, root=self.root, worktree=self.worktree,
                           prompt='changed assignment', objective='Bounded objective',
                           validate=lambda: {'accepted': True}, timeout=30)
        self.assertEqual('native_goal_resume_identity_mismatch', changed.metadata['error'])
        self.assertEqual(first.metadata['thread_id'], changed.metadata['thread_id'])

    def test_resume_rejects_live_previous_process_and_cannot_extend_deadline(self):
        FakeOpenCode.behaviors = [KeyboardInterrupt()]
        first = self.run_goal()
        self.command.options['native_goal_resume'] = True
        with patch('modport.goal_runtime._previous_process_alive', return_value=True):
            live = self.run_goal()
        self.assertEqual('native_goal_previous_process_alive', live.metadata['error'])
        state_path = self.root / first.metadata['state_path']
        state = json.loads(state_path.read_text())
        state['deadline_epoch'] = 1
        state_path.write_text(json.dumps(state))
        expired = self.run_goal(lambda: self.fail('expired resume cannot validate'))
        self.assertEqual('timed_out', expired.metadata['status'])
        self.assertEqual(1, len(FakeOpenCode.instances))

    def test_resume_mcp_identity_allows_timeout_reduction_only(self):
        FakeOpenCode.behaviors = [KeyboardInterrupt(), {'text': 'resumed'}]
        first_mcp = {'modport_rework': {'type': 'local', 'command': ['/usr/bin/python3', 'mcp.py'],
                                        'enabled': True, 'timeout': 20000}}
        first = self.run_goal(transport_args=first_mcp)
        self.assertIn('transport_config_sha256', first.metadata)
        self.command.options['native_goal_resume'] = True
        extended = {'modport_rework': {**first_mcp['modport_rework'], 'timeout': 21000}}
        rejected = self.run_goal(transport_args=extended)
        self.assertEqual('native_goal_resume_identity_mismatch', rejected.metadata['error'])
        reduced = {'modport_rework': {**first_mcp['modport_rework'], 'timeout': 10000}}
        resumed = self.run_goal(transport_args=reduced)
        self.assertEqual(0, resumed.returncode, resumed.metadata)

    def test_transport_config_hash_ignores_only_timeout(self):
        first = {'host': {'type': 'local', 'command': ['tool'], 'timeout': 20000}}
        reduced = {'host': {'type': 'local', 'command': ['tool'], 'timeout': 10000}}
        changed = {'host': {'type': 'local', 'command': ['other'], 'timeout': 10000}}
        self.assertEqual(_transport_config_identity(first)[0],
                         _transport_config_identity(reduced)[0])
        self.assertNotEqual(_transport_config_identity(first)[0],
                            _transport_config_identity(changed)[0])
        with self.assertRaisesRegex(ValueError, 'timeout'):
            _transport_config_identity({'host': {'type': 'local', 'timeout': True}})
        with self.assertRaisesRegex(TypeError, 'mapping'):
            _transport_config_identity(('-c', 'legacy'))

    def test_openai_proxy_endpoint_is_explicit_and_part_of_resume_identity(self):
        FakeOpenCode.behaviors = [KeyboardInterrupt()]
        with patch.dict(os.environ, {'OPENAI_BASE_URL': 'https://proxy-one.example/v1'}):
            first = self.run_goal()
        effective = FakeOpenCode.instances[0].config.to_dict()
        self.assertEqual(
            'https://proxy-one.example/v1',
            effective['provider']['openai']['options']['baseURL'],
        )
        self.assertNotIn('apiKey', effective['provider']['openai'])

        self.command.options['native_goal_resume'] = True
        with patch.dict(os.environ, {'OPENAI_BASE_URL': 'https://proxy-two.example/v1'}):
            changed = self.run_goal()
        self.assertEqual('native_goal_resume_identity_mismatch', changed.metadata['error'])
        self.assertEqual(1, len(FakeOpenCode.instances))
        self.assertEqual(first.metadata['thread_id'], changed.metadata['thread_id'])

    def test_concurrent_goal_lock_prevents_duplicate_server_launch(self):
        from hashlib import sha256
        directory = self.root / 'artifacts' / 'native-goals' / sha256(b'command').hexdigest()[:24]
        directory.mkdir(parents=True)
        with (directory / 'session.lock').open('a') as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            result = self.run_goal()
        self.assertEqual('native_goal_session_already_owned', result.metadata['error'])
        self.assertEqual([], FakeOpenCode.instances)


class OpenCodeCleanupDiagnosticTests(unittest.TestCase):
    def server_with_sleeping_process(self):
        directory = TemporaryDirectory(prefix="f01-opencode-cleanup-")
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        process = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(60)"],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, start_new_session=True)
        server = OpenCodeServer(
            process=process, base_url="http://127.0.0.1:1", env={},
            xdg_root=root, version="test-process", executable=Path(sys.executable),
            executable_sha256="0" * 64)
        self.addCleanup(server.close)
        return server

    def test_deadline_cleanup_survives_cgroup_collection_error(self):
        server = self.server_with_sleeping_process()
        server.memory_before = CgroupMemoryEvents(
            "/f01/test", {"oom_kill": 0}, 1.0)

        with patch("modport.opencode_runtime.read_memory_events",
                   side_effect=PermissionError("synthetic cgroup read failure")):
            diagnostic = server.close(
                deadline_exceeded=True, cleanup_reason="deadline_exceeded")

        self.assertEqual(-signal.SIGKILL, server.process.returncode)
        self.assertEqual("deadline_exceeded", diagnostic["classification"])
        self.assertTrue(diagnostic["host_requested_termination"])
        self.assertEqual("deadline_exceeded", diagnostic["cleanup_reason"])
        self.assertEqual(
            [{"phase": "cgroup_after", "error": "PermissionError"}],
            diagnostic["collection_errors"])
        self.assertIs(server.close(), diagnostic)

    def test_routine_cleanup_records_host_reason_and_raw_signal(self):
        server = self.server_with_sleeping_process()
        before = CgroupMemoryEvents("/f01/test", {"oom_kill": 0}, 1.0)
        after = CgroupMemoryEvents("/f01/test", {"oom_kill": 0}, 2.0)
        server.memory_before = before

        with patch("modport.opencode_runtime.read_memory_events", return_value=after):
            diagnostic = server.close(cleanup_reason="goal_accepted")

        self.assertEqual(-signal.SIGKILL, diagnostic["returncode"])
        self.assertEqual("signal_exit", diagnostic["classification"])
        self.assertTrue(diagnostic["host_requested_termination"])
        self.assertEqual(signal.SIGKILL, diagnostic["host_requested_signal"])
        self.assertEqual("goal_accepted", diagnostic["cleanup_reason"])


if __name__ == '__main__':
    unittest.main()
