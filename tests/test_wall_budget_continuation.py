"""A wall deadline preserves completed work and restarts only unfinished work."""
import unittest
import tempfile
from pathlib import Path
from modport.continuation import (_budget_exhausted_development_group,
                                  _retire_planned_successor)
from modport.contracts import OperationInput, OperationResult
from modport.operations import MigrationOperations


class WallBudgetTests(unittest.TestCase):
    def test_replacement_rejects_global_dispatch_and_replays_planned_cancellation(self):
        class SDK:
            def __init__(self, state):
                self.state = state
                self.calls = []

            def get_run(self, name):
                self.calls.append(('get_run', name))
                return self.state

            def apply_operations(self, run_id, **kwargs):
                self.calls.append(('apply_operations', run_id, kwargs))
                self.state['state'] = 'cancelled'

            def get_command_receipt(self, run_id, command_id):
                return {'state': 'cancelled'} if self.state['state'] == 'cancelled' else None

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            header = {'run_id': 'old', 'continuation': {'previous_run_id': 'source'}}
            state = {'input': header, 'state': 'running', 'revision': 1, 'waits': {},
                     'tasks': {'author': {'attempts': [{'state': 'pending_dispatch',
                        'result': None}]}}}
            sdk = SDK(state)
            with self.assertRaisesRegex(ValueError, 'no dispatched tasks'):
                _retire_planned_successor(root, sdk, 'old', 'new', 'source', header)
            self.assertFalse(any(row[0] == 'apply_operations' for row in sdk.calls))
            state['tasks']['author']['attempts'][0]['state'] = 'planned'
            _retire_planned_successor(root, sdk, 'old', 'new', 'source', header)
            applied = [row for row in sdk.calls if row[0] == 'apply_operations']
            self.assertEqual(['cancel', 'finish'], [x['kind'] for x in applied[0][2]['operations']])
            self.assertTrue((root / 'artifacts/continuations/old/run.json').exists())
            _retire_planned_successor(root, sdk, 'old', 'new', 'source', header)
            self.assertEqual(1, sum(row[0] == 'apply_operations' for row in sdk.calls))

    def test_public_sdk_replacement_retires_only_never_dispatched_work(self):
        from dispatcher_sdk.execution_kernel import Kernel, ExecutionCommandV2, RetryPolicy
        from dispatcher_sdk.orchestrator import Orchestrator, Operations

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            kernel = Kernel.open_sqlite(root / 'kernel.sqlite3',
                {'echo': lambda payload, context: payload}, isolation_mode='thread')
            try:
                sdk = Orchestrator(root / 'orchestrator.sqlite3', kernel.kernel, runtime=kernel)
                try:
                    header = {'run_id': 'old', 'continuation': {'previous_run_id': 'source'}}
                    sdk.create_run('old', command_id='create', input=header)
                    def command(name):
                        return ExecutionCommandV2(execution_id=name, idempotency_key=name,
                            registry_revision=kernel.registry_revision, correlation_id='old',
                            causation_id=None, handler_id='echo', handler_contract_version=1,
                            retry_policy=RetryPolicy(), timeout_seconds=5, payload={}).to_dict()
                    sdk.apply_operations('old', command_id='add', expected_revision=0,
                        operations=[Operations.add_task('author', command('old:author'))])
                    _retire_planned_successor(root, sdk, 'old', 'new', 'source', header)
                    self.assertEqual('cancelled', sdk.get_run('old')['state'])
                    self.assertEqual('cancelled', sdk.get_run('old')['tasks']['author']['attempts'][-1]['state'])
                    _retire_planned_successor(root, sdk, 'old', 'new', 'source', header)
                    pending = {'run_id': 'pending', 'continuation': {'previous_run_id': 'source'}}
                    sdk.create_run('pending', command_id='create-pending', input=pending)
                    sdk.apply_operations('pending', command_id='dispatch-pending', expected_revision=0,
                        operations=[Operations.add_task('author', command('pending:author')),
                                    Operations.dispatch('author')])
                    with self.assertRaisesRegex(ValueError, 'no dispatched tasks'):
                        _retire_planned_successor(root, sdk, 'pending', 'replacement', 'source', pending)
                    self.assertEqual('pending_dispatch',
                        sdk.get_run('pending')['tasks']['author']['attempts'][-1]['state'])
                finally:
                    sdk.close()
            finally:
                kernel.close()

    def test_local_coder_timeout_with_frozen_patch_does_not_end_live_run(self):
        class Host(MigrationOperations):
            clock = staticmethod(lambda: 100)

            def _flowthrough_outcome(self, attempt):
                return command, result

            def _flowthrough_record(self, app, cmd, outcome, *, canonical=True):
                return outcome.to_dict()

            def _finish(self, app, reason, state='failed'):
                raise AssertionError('local coder timeout incorrectly terminated Run: ' + reason)

            def _schedule(self, snapshot, header, app, stage, **kwargs):
                self.integrated = kwargs['payload']['development_results']
                return [{'kind': 'dispatch', 'stage': stage}]

        command = OperationInput('logical', 'coder.g1.a', 'coder', 'segment:coder.g1.a:1',
                                 '/tmp', payload={'development_task': {'id': 'a'}})
        result = OperationResult('blocked', 'logical', command.task_id, 'coder',
            command.command_id, error_code='budget_exhausted',
            outputs={'artifact_refs': {'coder_patch': {'path': 'frozen', 'sha256': 'x'}}})
        snapshot = {'tasks': {command.task_id: {'attempts': [{
            'state': 'succeeded', 'command': {'execution_id': command.command_id}}]}}}
        group = {'kind': 'development', 'generation': 1,
                 'tasks': [{'id': 'a', 'dependencies': []}], 'base': 'a' * 40,
                 'members': [command.task_id], 'results': {}, 'scheduled': ['a'],
                 'goal_scheduled': ['a'], 'artifact_refs': {}, 'execution_payload': {}}
        app = {'active_group': group, 'processed': [], 'acceptance_status': 'unverified'}
        operations = Host(clock=lambda: 100)._flowthrough_group_decision(snapshot,
            {'deadline_epoch': 200, 'request': {'max_parallel_coders': 1}}, app)
        self.assertEqual('development_integrate', operations[0]['stage'])
        self.assertEqual('blocked', result.status)

    def test_wall_budget_carries_prior_segment_and_requeues_interrupted_task(self):
        done = OperationResult('completed', 'first', 'coder.g1.a', 'coder', 'first:a',
            outputs={'development_task_id': 'a', 'artifact_refs': {'coder_patch': {'path': 'patch'}}})
        cmd = OperationInput('second', 'coder.g1.b', 'coder', 'second:b', '/tmp',
            payload={'development_task': {'id': 'b'}}, options={'workspace': 'workspaces/old-b'})
        failed = OperationResult('blocked', 'second', cmd.task_id, 'coder', cmd.command_id,
                                error_code='budget_exhausted')
        impl = OperationResult('completed', 'first', 'implementation', 'implementation', 'first:impl',
            outputs={'development_tasks': [{'id': 'a', 'dependencies': []}, {'id': 'b', 'dependencies': ['a']}],
                     'development_base': 'a'*40, 'artifact_refs': {}})
        app = {'terminal_reason': 'budget_exhausted', 'development_generation': 1,
               'effective': {'implementation': impl.to_dict(), done.task_id: done.to_dict()}}
        state = {'run_id': 'second', 'revision': 20, 'tasks': {cmd.task_id: {'attempts': [{
            'state': 'succeeded', 'command': {'payload': cmd.to_dict()},
            'result': {'value': failed.to_dict()}}]}}}
        group = _budget_exhausted_development_group(state, app)
        self.assertEqual(['a'], group['scheduled'])
        self.assertNotIn('coder.g1.b', group['results'])
        self.assertEqual(done.to_dict(), group['results']['coder.g1.a'])
        self.assertIn('first:a', group['execution_payload']['carried_development_results'])
        self.assertEqual('workspaces/old-b', group['execution_payload']['interrupted_development_work'][0]['workspace'])

        # A later failed explicit rework supersedes even a successful coder
        # attempt in this same segment. Its original patch is carried from
        # that SDK attempt separately, while the author is scheduled again.
        author = OperationInput('second', done.task_id, 'coder', 'second:a', '/tmp',
                                payload={'development_task': {'id': 'a'}})
        first = OperationResult('completed', 'second', done.task_id, 'coder', author.command_id,
                                outputs={'artifact_refs': {'coder_patch': {'path': 'first-patch'}}})
        app['effective'][done.task_id] = OperationResult(
            'failed', 'second', done.task_id, 'coder', 'second:rework',
            error_code='rework_execution_failed').to_dict()
        state['tasks'][done.task_id] = {'attempts': [{
            'state': 'succeeded', 'command': {'payload': author.to_dict()},
            'result': {'value': first.to_dict()}}]}
        goal = OperationResult('completed', 'second', 'goal.g1.a', 'goal_prepare', 'second:goal')
        app['effective'][goal.task_id] = goal.to_dict()
        next_group = _budget_exhausted_development_group(state, app)
        self.assertIsNotNone(next_group)
        self.assertNotIn('a', next_group['scheduled'])
        self.assertNotIn(done.task_id, next_group['results'])

        # A result cannot overwrite another planned task by changing only its
        # reported output identity while retaining the original SDK task key.
        state['tasks'][cmd.task_id]['attempts'][0]['command']['payload']['payload']['development_task']['id'] = 'a'
        rejected = _budget_exhausted_development_group(state, app)
        self.assertNotIn(cmd.task_id, rejected['results'])
        self.assertEqual('attempt_identity_mismatch',
            rejected['execution_payload']['continuation_identity_diagnostics'][0]['reason'])
