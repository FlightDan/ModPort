"""Current SDK rework keeps a nested author's edits in its isolated candidate."""
from dataclasses import replace
import io
import json
from pathlib import Path
import subprocess
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from modport.contracts import OperationInput, OperationResult
from modport.development import _artifact, development_workspace, validate_plan
from modport.goal_planning import validate_goal
from modport.kernel_runtime import open_runtime, operation_lock
from modport.memory_admission import MemorySnapshot
from modport.models import Budget, MigrationRequest
from modport.operations import MigrationOperations
from modport.payload_storage import unpack_input
from modport.rework_coder import rework_workspace
from modport.rework_effects import CoderReworkHandler
from modport.rework_mcp import PendingCall, ReworkServer, Session
from modport.rework_orchestration import project_rework_responses
from modport.rework_tools import prepare_session, rework_targets
from modport.workflow import WORKFLOW_VERSION, WorkflowDefinition


def git(workspace, *arguments):
    return subprocess.check_output([
        'git', '-c', 'user.name=Test', '-c', 'user.email=test@example.invalid',
        *arguments], cwd=workspace, stderr=subprocess.STDOUT).decode().strip()


class TestReworkHandler(CoderReworkHandler):
    __execution_kernel_revision__ = 'current-caller-rework-test'


class CallerWorkspaceReworkTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        product = self.root / 'worktree'
        product.mkdir()
        git(product, 'init')
        (product / 'a.txt').write_text('original a\n')
        (product / 'b.txt').write_text('original b\n')
        git(product, 'add', '.')
        git(product, 'commit', '-m', 'source')
        self.base = git(product, 'rev-parse', 'HEAD')
        self.deadline = time.time() + 600
        self.options = {'workflow_version': WORKFLOW_VERSION,
                        'deadline_epoch': self.deadline}
        plan = validate_plan({'schema_version': 1, 'base_commit': self.base,
            'shared_paths': [], 'tasks': [
                {'id': 'A', 'objective': 'Repair A', 'dependencies': [],
                 'owned_paths': ['a.txt'], 'acceptance': ['A is repaired']},
                {'id': 'B', 'objective': 'Preserve B with corrected A',
                 'dependencies': ['A'], 'owned_paths': ['b.txt'],
                 'acceptance': ['B is retained']}]}, workflow_version=WORKFLOW_VERSION)
        source = OperationInput('nested', 'coder.g1.A', 'coder', 'author-A', str(self.root),
            options=self.options)
        task = plan['tasks'][0]
        plan_ref = _artifact(source, 'development-plan.json', json.dumps(plan).encode(),
                             {'development_base': self.base})
        goal = validate_goal({}, task, {}, gates_disabled=True)
        goal_ref = _artifact(source, 'goal.json', json.dumps(goal).encode())
        self.original = replace(source, payload={'development_task': task,
            'planning_context': {}, 'goal_scope': 'migration',
            'development_generation': 1, 'development_workspace_epoch': '1111111111111111',
            'recovered_partial_patch': {'path': 'artifacts/old-recovery.patch'},
            'recovered_rework_request': {'path': 'artifacts/old-recovery-request.json'},
            'coder_revival': {'request_id': 'old-revival'}},
            artifact_refs={'development_plan': plan_ref, 'coder_goal': goal_ref})
        original_result = OperationResult('completed', source.run_id, source.task_id,
            source.stage_id, source.command_id, outputs={'artifact_refs': self.original.artifact_refs})
        caller_payload = {'development_task': plan['tasks'][1],
            'development_generation': 1, 'development_workspace_epoch': '2222222222222222'}
        relative = development_workspace(1, 'B', caller_payload)
        self.workspace = self.root / relative
        self.workspace.parent.mkdir(parents=True)
        git(self.root, 'clone', '--no-hardlinks', str(product), str(self.workspace))
        self.caller = OperationInput('nested', 'coder.g1.B', 'coder', 'author-B', str(self.root),
            payload=caller_payload, options={**self.options, 'workspace': relative},
            upstream_results={source.task_id: original_result.to_dict()})
        self.snapshot = {'run_id': 'nested', 'state': 'running', 'tasks': {
            source.task_id: {'attempts': [{'state': 'succeeded', 'command': {
                'execution_id': source.command_id, 'payload': self.original.to_dict()},
                'result': {'value': original_result.to_dict()}}]},
            self.caller.task_id: {'attempts': [{'state': 'running', 'command': {
                'execution_id': self.caller.command_id, 'payload': self.caller.to_dict()}}]}}}
        targets = rework_targets(self.snapshot, self.caller)
        self.assertEqual([source.task_id], [row['target_agent'] for row in targets])
        self.caller = replace(self.caller, payload={**self.caller.payload,
                                                  'review_rework_targets': targets})
        self.snapshot['tasks'][self.caller.task_id]['attempts'][0]['command']['payload'] = self.caller.to_dict()
        request = MigrationRequest('nested', 'https://example.invalid/source.git',
            '1.20.1', '26.1.2', budget=Budget(max_seconds=600)).to_dict()
        self.header = {'run_dir': str(self.root), 'request': request,
            'definition': WorkflowDefinition(request).to_dict(), 'deadline_epoch': self.deadline,
            'rubric_sha256': 'test-host-rubric', 'registry_revision': 'test-host-registry',
            'initial_refs': {}, 'prior_findings': []}
        self.host = MigrationOperations(memory_probe=lambda: MemorySnapshot(
            64 * 1024**3, 64 * 1024**3, 'test host capacity'))
        self.app = self.host._new_application()
        self.app['effective'][source.task_id] = original_result.to_dict()
        self.app['active_group'] = {'results': {source.task_id: original_result.to_dict()}}

    def start_tool(self, caller=None, workspace=None):
        caller = caller or self.caller
        session_path = prepare_session(caller, workspace or self.workspace, 300)
        server = ReworkServer(Session.load(session_path), input_stream=io.StringIO(),
                              output_stream=io.StringIO(), poll_interval=0.01)
        pending = PendingCall(rpc_id='request')
        self.tool_result = []
        published = threading.Event()
        from modport.rework_mcp import atomic_json

        def publish(path, value):
            atomic_json(path, value)
            if path.parent.name == 'requests':
                published.set()

        def invoke():
            self.tool_result.append(server._request_rework(pending, {
                'target_agent': self.original.task_id, 'instructions': 'Correct A and retain caller B.'}))

        with patch('modport.rework_mcp.atomic_json', side_effect=publish):
            thread = threading.Thread(target=invoke, daemon=True)
            thread.start()
            self.assertTrue(published.wait(5), 'MCP request was not published')
        def close():
            server._stop.set()
            thread.join(5)
        self.addCleanup(close)
        self.thread = thread
        return pending.request_id

    def schedule(self):
        operations = self.host._review_rework_decision(self.snapshot, self.header, self.app)
        addition = next(row for row in operations if row['kind'] == 'add_task')
        child = OperationInput.from_dict(unpack_input(self.root, addition['command']['payload']))
        self.assertEqual('agent_rework', child.stage_id)
        self.assertEqual(self.caller.options['workspace'], rework_workspace(child))
        self.assertEqual(self.caller.to_dict(), child.payload['rework_caller_command'])
        self.assertLessEqual(child.options['deadline_epoch'], self.deadline)
        self.assertEqual(self.root / '.locks/scopes' / self.caller.options['workspace'],
                         operation_lock(self.root, child))
        return child

    def execute(self, child, *, fail=False, caller_drift=False, caller_conflict=False):
        outer = self

        class ModelBoundary:
            def __init__(self, _prompt, **_options):
                pass

            def __call__(self, command):
                from modport.execution_budget import current_deadline_budget
                outer.assertLessEqual(current_deadline_budget(command).effective_deadline,
                                      child.options['deadline_epoch'])
                outer.assertIsNone(command.payload['development_workspace_epoch'])
                for stale in ('recovered_partial_patch', 'recovered_rework_request', 'coder_revival'):
                    outer.assertNotIn(stale, command.payload)
                expected = development_workspace(command.payload['development_generation'],
                                                 'A', command.payload)
                outer.assertEqual(expected, command.options['workspace'])
                clone = outer.root / expected
                outer.assertTrue(clone.is_dir())
                outer.assertEqual('caller B\n', (clone / 'b.txt').read_text())
                if fail:
                    return OperationResult('failed', command.run_id, command.task_id,
                        command.stage_id, command.command_id,
                        detail='raw model transport failure', error_code='model_transport_failed')
                (clone / 'a.txt').write_text('repaired a\n')
                if caller_drift:
                    (outer.workspace / 'b.txt').write_text('later caller B\n')
                    git(outer.workspace, 'add', 'b.txt')
                    git(outer.workspace, 'commit', '-m', 'caller progressed during child')
                if caller_conflict:
                    (outer.workspace / 'a.txt').write_text('caller changed A\n')
                    git(outer.workspace, 'add', 'a.txt')
                    git(outer.workspace, 'commit', '-m', 'caller changed the same line')
                return OperationResult('completed', command.run_id, command.task_id,
                    command.stage_id, command.command_id)

        with patch('modport.handlers.CodexStageHandler', ModelBoundary):
            with open_runtime(self.root, handlers={'modport.agent_rework': TestReworkHandler()},
                              isolation_mode='thread') as runtime:
                sdk_command = runtime.command('modport.agent_rework',
                    execution_id=child.command_id, idempotency_key=child.command_id,
                    correlation_id=child.run_id, timeout_seconds=180, payload=child.to_dict())
                runtime.submit(sdk_command)
                execution = runtime.run_once()
        self.assertEqual('succeeded', execution.state)
        receipt = json.loads((self.root / 'artifacts/executions' / child.command_id / 'receipt.json').read_text())
        self.assertEqual(child.command_id, receipt['execution_id'])
        self.assertEqual(execution.result.value, receipt['response'])
        return OperationResult.from_dict(execution.result.value)

    def deliver(self, child, result):
        self.snapshot['tasks'][child.task_id] = {'attempts': [{'state': 'succeeded',
            'command': {'execution_id': child.command_id, 'payload': child.to_dict()},
            'result': {'value': result.to_dict()}}]}
        followups = self.host._review_rework_decision(self.snapshot, self.header, self.app)
        self.assertEqual([], followups, 'isolated caller rework must return without a root verification')
        project_rework_responses(self.root, self.app)
        self.thread.join(5)
        self.assertFalse(self.thread.is_alive(), 'caller did not consume its tool response')
        self.assertEqual(1, len(self.tool_result))

    def test_sdk_nested_delta_preserves_dirty_caller_and_later_progress(self):
        (self.workspace / 'b.txt').write_text('caller B\n')
        report = self.workspace / '.modport/goal-reports/B.json'
        report.parent.mkdir(parents=True)
        report.write_text('caller report in progress\n')
        generated = self.workspace / 'build/generated.txt'
        generated.parent.mkdir()
        generated.write_text('generated output\n')
        request = self.start_tool()
        child = self.schedule()
        frozen_original = self.original.to_dict()
        result = self.execute(child, caller_drift=True)
        self.assertEqual('completed', result.status, result.detail)
        self.assertEqual('repaired a\n', (self.workspace / 'a.txt').read_text())
        self.assertEqual('later caller B\n', (self.workspace / 'b.txt').read_text())
        self.assertEqual('caller report in progress\n', report.read_text())
        self.assertEqual('generated output\n', generated.read_text())
        self.assertEqual('', git(self.workspace, 'ls-files', '--', '.modport/goal-reports/B.json',
                                 'build/generated.txt'))
        self.assertEqual(self.base, git(self.root / 'worktree', 'rev-parse', 'HEAD'))
        self.assertEqual('original a\n', (self.root / 'worktree/a.txt').read_text())
        self.assertEqual(frozen_original, self.original.to_dict())
        self.deliver(child, result)
        self.assertNotIn('isError', self.tool_result[0])
        self.assertEqual('author-A', self.app['active_group']['results'][self.original.task_id]['command_id'])
        self.assertEqual('completed', self.app['review_rework']['requests']['author-B/' + request]['state'])

    def test_sdk_failure_returns_raw_cause_to_waiting_caller(self):
        (self.workspace / 'b.txt').write_text('caller B\n')
        self.start_tool()
        child = self.schedule()
        result = self.execute(child, fail=True)
        self.assertEqual('model_transport_failed', result.error_code)
        self.assertIn('raw model transport failure', result.detail)
        self.deliver(child, result)
        self.assertTrue(self.tool_result[0]['isError'])
        self.assertIn('raw model transport failure', json.dumps(self.tool_result[0]))
        self.assertEqual('original a\n', (self.workspace / 'a.txt').read_text())
        self.assertEqual(self.base, git(self.root / 'worktree', 'rev-parse', 'HEAD'))

    def test_unbound_peer_or_diagnostic_workspace_is_not_a_destination(self):
        self.start_tool()
        child = self.schedule()
        for destination in ('workspaces/development/g1/A', 'workspaces/supervisor/diagnosis'):
            with self.subTest(destination=destination):
                changed = replace(child, payload={**child.payload, 'reviewer_workspace': destination})
                with self.assertRaisesRegex(ValueError, 'bound caller workspace'):
                    rework_workspace(changed)

    def test_actual_merge_conflict_returns_to_live_author_with_both_edits(self):
        (self.workspace / 'b.txt').write_text('caller B\n')
        self.start_tool()
        child = self.schedule()
        result = self.execute(child, caller_conflict=True)
        self.assertEqual('failed', result.status, result.detail)
        self.assertEqual('coder_rework_conflict', result.error_code)
        self.assertEqual('conflict', result.outputs['integration_status'])
        self.assertEqual(['a.txt'], result.outputs['dependency_conflicts'][0]['paths'])
        conflict = (self.workspace / 'a.txt').read_text()
        self.assertIn('caller changed A', conflict)
        self.assertIn('repaired a', conflict)
        self.assertIn('<<<<<<<', conflict)
        self.assertEqual(self.base, git(self.root / 'worktree', 'rev-parse', 'HEAD'))
        self.deliver(child, result)
        self.assertTrue(self.tool_result[0]['isError'])
        self.assertIn('requesting author', json.dumps(self.tool_result[0]))

    def test_supervisor_rework_keeps_diagnostic_copy_read_only(self):
        workspace = self.root / 'workspaces/supervisor/diagnosis'
        workspace.mkdir(parents=True)
        (workspace / 'notes.txt').write_text('diagnostic observation\n')
        supervisor = replace(self.caller, task_id='supervisor', stage_id='supervisor',
            command_id='supervisor-1', payload={'watchdog_incident': {},
            'review_rework_targets': self.caller.payload['review_rework_targets']},
            options=self.options)
        self.snapshot['tasks'][supervisor.task_id] = {'attempts': [{'state': 'running',
            'command': {'execution_id': supervisor.command_id, 'payload': supervisor.to_dict()}}]}
        self.start_tool(supervisor, workspace)
        operations = self.host._review_rework_decision(self.snapshot, self.header, self.app)
        addition = next(row for row in operations if row['kind'] == 'add_task')
        child = OperationInput.from_dict(unpack_input(self.root, addition['command']['payload']))
        self.assertEqual('worktree', rework_workspace(child))
        self.assertEqual('workspaces/supervisor/diagnosis', child.payload['reviewer_context_workspace'])
        self.assertNotIn('rework_caller_command', child.payload)
        self.assertEqual('diagnostic observation\n', (workspace / 'notes.txt').read_text())


if __name__ == '__main__':
    unittest.main()
