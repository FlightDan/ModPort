"""Current progress supervisors publish fixes for later dispatch/integration."""
from copy import deepcopy
from dataclasses import replace
import json
from pathlib import Path
import subprocess
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from dispatcher_sdk.execution_kernel import BudgetEnvelope, ClockCheckpoint, DeadlineConstraint

from modport.contracts import OperationInput
from modport.development import DevelopmentIntegrateHandler
from modport.execution_budget import execution_budget
from modport.handlers import SupervisorHandler
from modport.models import Budget, MigrationRequest
from modport.operations import MigrationOperations
from modport.workflow import compile_migration_workflow, WORKFLOW_VERSION


def git(directory, *arguments):
    return subprocess.check_output(
        ['git', '-c', 'user.name=Test', '-c', 'user.email=test@example.invalid', *arguments],
        cwd=directory, stderr=subprocess.DEVNULL, text=True).strip()


class SDKBudgetContext:
    def __init__(self, command):
        self.command = SimpleNamespace(execution_id=command.command_id,
                                       timeout_seconds=1200, payload=command.to_dict())
        self.envelope = BudgetEnvelope((DeadlineConstraint(
            'supervisor-model', 'execution', time.time() + 1200, 0),), self.sample())

    @staticmethod
    def sample():
        return ClockCheckpoint(time.time(), time.monotonic(), 'progress-diagnostic-test', 'boot')

    @property
    def budget(self):
        self.envelope = self.envelope.recheckpoint(sample=self.sample())
        return self.envelope.view(sample=self.envelope.checkpoint)


class ProgressDiagnosticRepairTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.worktree = self.root / 'worktree'
        (self.worktree / 'src').mkdir(parents=True)
        self.original = 'class Mod { void call() { wrong(); } }\n'
        (self.worktree / 'src' / 'Mod.java').write_text(self.original)
        git(self.worktree, 'init')
        git(self.worktree, 'add', '.')
        git(self.worktree, 'commit', '-m', 'Original task baseline')
        self.base = git(self.worktree, 'rev-parse', 'HEAD')
        self.author_relative = 'workspaces/development/g1/api'
        self.author = self.root / self.author_relative
        (self.author / 'src').mkdir(parents=True)
        (self.author / 'src' / 'Mod.java').write_text(self.original)
        self.task = {'id': 'api', 'objective': 'Preserve API behavior on the target version.',
                     'owned_paths': ['src'], 'dependencies': [], 'acceptance': ['Keep behavior'],
                     'complexity': 'simple'}
        self.plan_path = self.root / 'artifacts' / 'development-plan.json'
        self.plan_path.parent.mkdir()
        self.plan_path.write_text(json.dumps({'schema_version': 1, 'base_commit': self.base,
                                             'shared_paths': [], 'tasks': [self.task]}))
        # Existing host plan provenance is supplied directly; this regression
        # adds no hash computation or verification to diagnosis or integration.
        self.plan_ref = {'path': 'artifacts/development-plan.json', 'media_type': 'application/json',
                         'metadata': {'execution_id': 'planner', 'development_base': self.base}}
        request = MigrationRequest('diagnostic-probe', 'https://example.invalid/mod.git',
                                   '1.20.1', '1.21.1',
                                   budget=Budget(max_seconds=3600, max_agent_assignments=12,
                                                 max_rework_rounds=0))
        definition = compile_migration_workflow(request).to_dict()
        self.assertEqual(WORKFLOW_VERSION, definition['workflow_version'])
        self.assertEqual(40, WORKFLOW_VERSION)
        self.now = [time.time()]
        self.owner = MigrationOperations(clock=lambda: self.now[0])
        self.header = {'run_id': 'probe', 'run_dir': str(self.root), 'request': request.to_dict(),
                       'definition': definition, 'started_at': self.now[0],
                       'deadline_epoch': self.now[0] + 3600,
                       'registry_revision': 'host-registry', 'rubric_sha256': 'host-rubric',
                       'initial_refs': {'development_plan': self.plan_ref}, 'prior_findings': []}
        self.app = self.owner._new_application()
        self.snapshot = {'run_id': 'probe', 'state': 'running', 'tasks': {}, 'waits': {}}
        actions = self.owner._schedule(self.snapshot, self.header, self.app, 'coder',
                    task_id='coder.g1.api', dependencies=[], activate=False,
                    payload={'development_task': self.task, 'goal_scope': 'migration',
                             'development_base': self.base, 'development_generation': 1},
                    extra_options={'workspace': self.author_relative})
        self.coder = self.command(actions)
        self.install(self.coder, 'running')

    def command(self, actions):
        commands = [OperationInput.from_dict(action['command']['payload'])
                    for action in actions if action['kind'] in {'add_task', 'new_attempt'}]
        self.assertEqual(1, len(commands), actions)
        return commands[0]

    def install(self, command, state, result=None):
        attempt = {'state': state,
                   'command': {'execution_id': command.command_id, 'payload': command.to_dict()}}
        if result is not None:
            attempt['result'] = {'value': result.to_dict()}
        self.snapshot['tasks'][command.task_id] = {'attempts': [attempt]}

    def idle_supervisor(self):
        # Four deterministic observations represent a baseline plus three
        # idle windows. No polling, background workers or waiting are needed.
        self.assertEqual([], self.owner._progress_supervision_decision(
            self.snapshot, self.header, self.app))
        actions = []
        for _ in range(3):
            self.now[0] += 600
            actions = self.owner._progress_supervision_decision(self.snapshot, self.header, self.app)
        supervisor = self.command(actions)
        self.assertEqual(self.coder.command_id,
                         supervisor.payload['progress_supervision']['target_execution_id'])
        self.assertEqual(self.author_relative,
                         supervisor.payload['diagnostic_repair_targets'][0]['source_workspace'])
        self.assertEqual(self.header['deadline_epoch'], supervisor.options['deadline_epoch'])
        self.assertFalse(any(action['kind'] == 'cancel' for action in actions))
        return supervisor

    def execute_supervisor(self, supervisor, decision='continue'):
        request = supervisor.payload['progress_supervision']
        response = {key: request[key] for key in ('review_id', 'target_task_id', 'target_execution_id')}
        response.update(decision=decision, reason='Wrong target API call confirmed in src/Mod.java.')
        calls = []

        def run_model(**arguments):
            calls.append(arguments)
            self.assertFalse(arguments['read_only'])
            self.assertEqual(supervisor.command_id, arguments['command_id'])
            self.assertLessEqual(arguments['timeout'], 1200)
            self.assertGreater(arguments['timeout'], 0)
            self.assertNotEqual(self.author, arguments['cwd'])
            task_document = json.loads((self.root / 'artifacts' / 'executions'
                / supervisor.command_id / 'task-instructions.execute.json').read_text())
            self.assertIn('confirmed, small code errors', task_document['task'])
            self.assertIn('source/task-N/', task_document['task'])
            edited = arguments['cwd'] / 'source' / 'task-0' / 'src' / 'Mod.java'
            self.assertEqual(self.original, edited.read_text())
            edited.write_text(self.original.replace('wrong()', 'correct()'))
            completed = subprocess.CompletedProcess(['opencode'], 0, json.dumps({
                'type': 'item.completed', 'item': {'type': 'agent_message', 'text': json.dumps(response)}}))
            completed.dialogue_metadata = {}
            return completed

        compressor = SimpleNamespace(compress=lambda text, **kwargs: SimpleNamespace(
            text=text, metadata={'compressed': False, 'context_window': 272000,
                'input_token_budget': 200000, 'output_token_reserve': 10000,
                'tool_token_reserve': 10000}))
        context = SDKBudgetContext(supervisor)
        # Exercise the actual stage and its model transport boundary. Host
        # rule/rubric authentication is supplied from the established context.
        with patch('modport.handlers._acceptance_rubric_for', return_value={}), \
             patch('modport.handlers._read_artifact_ref', return_value=(
                 'Shared execution rules.', {'path': 'artifacts/rules.md',
                                             'sha256': 'host-supplied-provenance'})), \
             patch('modport.handlers._reuse_cached_compressed_prompt', return_value=None), \
             patch('modport.handlers.PromptCompressor.from_environment', return_value=compressor), \
             patch('modport.opencode_agent.run_agent', side_effect=run_model), \
             execution_budget(context):
            result = SupervisorHandler()(supervisor)
        self.assertEqual('completed', result.status, result.detail)
        self.assertEqual(response, result.outputs['progress_supervisor_decision'])
        self.assertEqual(1, len(calls))
        self.assertEqual(1, len(result.outputs['diagnostic_repairs']))
        self.assertEqual(self.original, (self.author / 'src' / 'Mod.java').read_text())
        self.assertEqual(self.original, (self.worktree / 'src' / 'Mod.java').read_text())
        return result

    def test_live_supervisor_fix_is_registered_for_matching_future_dispatch_only(self):
        original_command = deepcopy(self.snapshot['tasks'][self.coder.task_id]['attempts'][0]['command'])
        supervisor = self.idle_supervisor()
        result = self.execute_supervisor(supervisor)
        self.install(supervisor, 'succeeded', result)
        actions = self.owner._progress_supervision_decision(self.snapshot, self.header, self.app)
        self.assertFalse(any(action['kind'] == 'cancel' for action in actions))
        self.assertEqual(result.outputs['diagnostic_repairs'], self.app['diagnostic_repairs'])
        successor = self.command(self.owner._schedule(self.snapshot, self.header, self.app, 'coder',
            task_id='coder.g2.api', dependencies=[], activate=False,
            payload={'development_task': self.task, 'goal_scope': 'migration'},
            artifact_overrides={'development_plan': self.plan_ref}))
        self.assertEqual(result.outputs['diagnostic_repairs'], successor.payload['diagnostic_repair_refs'])
        self.assertEqual(original_command,
                         self.snapshot['tasks'][self.coder.task_id]['attempts'][0]['command'])
        self.assertEqual(self.header['deadline_epoch'], successor.options['deadline_epoch'])
        self.assertEqual(self.original, (self.author / 'src' / 'Mod.java').read_text())
        other = self.command(self.owner._schedule(self.snapshot, self.header, self.app, 'coder',
            task_id='coder.g2.other', dependencies=[], activate=False,
            payload={'development_task': {**self.task, 'id': 'other'}, 'goal_scope': 'migration'}))
        self.assertNotIn('diagnostic_repair_refs', other.payload)

    def test_late_completed_supervisor_retains_fix_without_cancelling_settled_target(self):
        supervisor = self.idle_supervisor()
        result = self.execute_supervisor(supervisor, decision='terminate')
        self.snapshot['tasks'][self.coder.task_id]['attempts'][0]['state'] = 'succeeded'
        self.install(supervisor, 'succeeded', result)
        actions = self.owner._progress_supervision_decision(self.snapshot, self.header, self.app)
        self.assertFalse(any(action['kind'] == 'cancel' for action in actions))
        self.assertEqual(result.outputs['diagnostic_repairs'], self.app['diagnostic_repairs'])

    def test_repair_after_frozen_integration_is_dispatched_to_cleanup(self):
        from modport.cleanup import CodeCleanupHandler
        from modport.handlers import _result
        supervisor = self.idle_supervisor()
        integration = self.command(self.owner._schedule(
            self.snapshot, self.header, self.app, 'development_integrate', dependencies=[],
            artifact_overrides={'development_plan': self.plan_ref},
            payload={'development_results': [], 'development_base': self.base,
                     'development_generation': 1, 'goal_scope': 'migration'}))
        frozen = deepcopy(integration.to_dict())
        self.assertNotIn('diagnostic_repair_refs', integration.payload)
        result = self.execute_supervisor(supervisor)
        self.snapshot['tasks'][self.coder.task_id]['attempts'][0]['state'] = 'succeeded'
        with patch('modport.development._verified', return_value=self.plan_path):
            integrated = DevelopmentIntegrateHandler()(integration)
        self.assertEqual('completed', integrated.status, integrated.detail)
        self.owner._flowthrough_record(self.app, integration, integrated)
        self.install(supervisor, 'succeeded', result)
        self.owner._progress_supervision_decision(self.snapshot, self.header, self.app)
        cleanup = self.command(self.owner._schedule(
            self.snapshot, self.header, self.app, 'code_cleanup', dependencies=[]))
        self.assertEqual(result.outputs['diagnostic_repairs'], cleanup.payload['diagnostic_repair_refs'])
        self.assertEqual(frozen, integration.to_dict())

        def model(handler, command):
            private = self.root / command.options['workspace'] / 'src/Mod.java'
            self.assertIn('correct()', private.read_text())
            self.assertEqual(self.original, (self.worktree / 'src/Mod.java').read_text())
            return _result(command, 'completed')

        with patch('modport.handlers.CodexStageHandler.__call__', model):
            cleaned = CodeCleanupHandler()(cleanup)
        self.assertEqual('completed', cleaned.status, cleaned.detail)
        self.owner._flowthrough_record(self.app, cleanup, cleaned)
        self.assertIn('correct()', (self.worktree / 'src/Mod.java').read_text())
        ref = result.outputs['diagnostic_repairs'][0]
        self.assertEqual('integrated', self.app['diagnostic_repair_dispositions'][ref['path']]['status'])
        review = self.app['progress_supervision']['reviews'][supervisor.payload['progress_supervision']['review_id']]
        self.assertEqual('superseded', review['status'])
        self.owner._progress_supervision_decision(self.snapshot, self.header, self.app)
        self.assertEqual(1, len(self.app['diagnostic_repairs']))
        self.assertEqual([], self.app['cancel_sent'])

    def test_late_fix_is_committed_at_final_integration_without_fresh_coder(self):
        supervisor = self.idle_supervisor()
        result = self.execute_supervisor(supervisor)
        self.snapshot['tasks'][self.coder.task_id]['attempts'][0]['state'] = 'succeeded'
        self.install(supervisor, 'succeeded', result)
        self.owner._progress_supervision_decision(self.snapshot, self.header, self.app)
        integration = self.command(self.owner._schedule(self.snapshot, self.header, self.app,
            'development_integrate', dependencies=[],
            payload={'development_results': [], 'development_base': self.base,
                     'development_generation': 1, 'goal_scope': 'migration'}))
        self.assertEqual(result.outputs['diagnostic_repairs'], integration.payload['diagnostic_repair_refs'])
        # The host already supplied this frozen plan. Exercise its actual
        # consumer and Git integration without a second digest verification.
        with patch('modport.development._verified', return_value=self.plan_path):
            integrated = DevelopmentIntegrateHandler()(integration)
        self.assertEqual('completed', integrated.status, integrated.detail)
        self.assertEqual('applied', integrated.outputs['diagnostic_repair_receipts'][0]['status'])
        self.assertIn('correct()', (self.worktree / 'src' / 'Mod.java').read_text())
        self.assertNotEqual(self.base, git(self.worktree, 'rev-parse', 'HEAD'))
        self.assertEqual('', git(self.worktree, 'status', '--porcelain'))
        self.assertEqual(self.original, (self.author / 'src' / 'Mod.java').read_text())

    def test_interrupted_snapshot_does_not_block_read_only_supervision(self):
        from modport.handlers import _result
        supervisor = self.idle_supervisor()
        partial = self.root / 'workspaces/diagnostic-repairs' / supervisor.command_id / 'partial.txt'
        partial.parent.mkdir(parents=True)
        partial.write_text('Interrupted snapshot evidence')
        request = supervisor.payload['progress_supervision']
        decision = {key: request[key] for key in ('review_id', 'target_task_id', 'target_execution_id')}
        decision.update(decision='continue', reason='Keep observing the active coder.')

        def model(handler, command):
            self.assertTrue(handler.read_only)
            self.assertEqual(supervisor.options['workspace'], command.options['workspace'])
            self.assertNotIn('diagnostic_repair_manifest', command.payload)
            self.assertIn('Continue the investigation read-only', handler.prompt)
            report = self.root / 'logs/supervisor-read-only.txt'
            report.parent.mkdir(exist_ok=True)
            report.write_text(json.dumps(decision))
            return _result(command, 'completed', outputs={'last_message': str(report)})

        with patch('modport.handlers.CodexStageHandler.__call__', model):
            result = SupervisorHandler()(supervisor)
        self.assertEqual('completed', result.status, result.detail)
        self.assertEqual(decision, result.outputs['progress_supervisor_decision'])
        self.assertIn('no host manifest', result.outputs['diagnostic_repair_preparation_diagnostic'])
        self.assertEqual('Interrupted snapshot evidence', partial.read_text())
        self.assertEqual(self.original, (self.author / 'src/Mod.java').read_text())


if __name__ == '__main__':
    unittest.main()
