"""Failure supervision through public SDK dispatch and the real report protocol.

Authors and model output are controlled fixtures. No project code is executed;
these checks establish routing, report custody and budget authority, not migration
or runtime acceptance.
"""
import json
from pathlib import Path
import tempfile
import time
import unittest

from dispatcher_sdk.orchestrator import Orchestrator

from modport.application_state_storage import hydrate_run_snapshot
from modport.contracts import OperationResult
from modport.evidence import atomic_json
from modport.execution_budget import current_deadline_budget, current_sdk_context
from modport.kernel_runtime import open_runtime
from modport.memory_admission import MemorySnapshot
from modport.models import Budget, MigrationRequest
from modport.operations import MigrationOperations
from modport.report_schemas import report_contract
from modport.rework_orchestration import project_rework_responses
from modport.rework_tools import session_directory
from modport.watchdog_supervisor import invoke_watchdog_supervisor, watchdog_supervisor_schema
from modport.workflow import WORKFLOW_VERSION, compile_migration_workflow


class ControlledAuthor:
    __execution_kernel_revision__ = 'failure-supervision-sdk-author-v1'

    def __init__(self):
        self.commands = []

    def __call__(self, command):
        self.commands.append(command)
        root = Path(command.run_dir)
        failed = command.stage_id in {'test_design', 'test_execute', 'research_cleanup', 'agent_rework'} and command.attempt == 1
        report = root / 'logs' / (command.command_id + '.txt')
        report.parent.mkdir(parents=True, exist_ok=True)
        report.write_text('raw missing adapter: fixture_action' if failed else 'fresh fixture author result')
        if command.stage_id == 'test_design':
            workspace = root / command.options['workspace']
            workspace.mkdir(parents=True, exist_ok=True)
            (workspace / 'retained-author.txt').write_text(command.command_id)
        return OperationResult('failed' if failed else 'completed', command.run_id,
            command.task_id, command.stage_id, command.command_id,
            outputs={'last_message': report.relative_to(root).as_posix(),
                     'fixture_result': report.read_text(), 'workspace': command.options.get('workspace')},
            error_code=('cleanup_report_failed' if command.stage_id == 'research_cleanup'
                        else 'independent_test_failed') if failed else None,
            detail=report.read_text())


class ControlledSupervisor:
    __execution_kernel_revision__ = 'failure-supervision-sdk-supervisor-v1'

    def __init__(self):
        self.mode = 'repair_resume'
        self.commands = []
        self.prompts = []
        self.budgets = []

    def __call__(self, command):
        self.commands.append(command)
        self.budgets.append((current_sdk_context(), current_deadline_budget(command)))

        def author(prompt):
            self.prompts.append(prompt)

            def write(prepared):
                request = prepared.payload['watchdog_incident']
                document = {'incident_id': request['incident_id'], 'action': self.mode,
                    'reason': 'The retained raw failure identifies fixture_action as missing.',
                    'instruction': 'Restore fixture_action and return a fresh author result.',
                    'wait_for': [], 'stop_category': None}
                if self.mode == 'stop':
                    document.update(instruction=None, stop_category='budget_exhausted')
                elif self.mode == 'continue':
                    document.update(instruction=None,
                        reason='The navigation diagnostic is advisory; retain its raw failure and continue the migration.')
                output = Path(prepared.run_dir) / 'logs' / (prepared.command_id + '.report.txt')
                output.parent.mkdir(parents=True, exist_ok=True)
                output.write_text('invalid report' if self.mode == 'invalid' else json.dumps(document))
                return OperationResult('completed', prepared.run_id, prepared.task_id,
                    prepared.stage_id, prepared.command_id,
                    outputs={'last_message': output.relative_to(prepared.run_dir).as_posix()})

            return write

        return invoke_watchdog_supervisor(command, author)


class FailureSupervisionSDKTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        (self.root / 'worktree').mkdir()
        self.now = [time.time()]
        self.author = ControlledAuthor()
        self.supervisor = ControlledSupervisor()
        handlers = {f'modport.{stage}': self.author for stage in
                    ('test_design', 'test_review', 'test_execute', 'gap_review', 'acceptance_preflight',
                     'research_cleanup', 'agent_rework', 'code_review')}
        handlers['modport.supervisor'] = self.supervisor
        self.owner = MigrationOperations(handlers=handlers, isolation_mode='thread', clock=lambda: self.now[0])
        self.runtime = self.enterContext(open_runtime(self.root, handlers=handlers,
            isolation_mode='thread', memory_policy=self.owner.memory_policy))
        self.sdk = Orchestrator(self.root / 'orchestrator.sqlite3', self.runtime.kernel,
                                runtime=self.runtime)
        self.addCleanup(self.sdk.close)
        request = MigrationRequest('fixture', 'https://example.invalid/fixture.git', '1.20.1', '1.21.1',
            max_parallel_coders=1,
            budget=Budget(max_seconds=100_000, max_agent_assignments=20, max_rework_rounds=0))
        definition = compile_migration_workflow(request).to_dict()
        self.assertEqual(44, WORKFLOW_VERSION)
        self.assertEqual(WORKFLOW_VERSION, definition['workflow_version'])
        self.assertEqual('supervisor_first', definition['failure_supervision_policy']['mode'])
        self.header = {'format_version': 2, 'run_id': 'failure-probe', 'run_dir': str(self.root),
            'request': request.to_dict(), 'definition': definition,
            'registry_revision': self.runtime.registry_revision, 'prior_findings': [],
            'initial_refs': {}, 'rubric_sha256': 'host-provided',
            'started_at': self.now[0], 'deadline_epoch': self.now[0] + 100_000,
            'watchdog_policy': {'enabled': False}}
        atomic_json(self.root / 'run.json', self.header)
        self.sdk.create_run(self.header['run_id'], command_id='create', input=self.header, definition=definition)
        self.sequence = 0

    def read(self):
        self.sdk.sync()
        return hydrate_run_snapshot(self.root, self.sdk.get_run(self.header['run_id']))

    def apply(self, snapshot, actions, app):
        self.sequence += 1
        self.sdk.apply_operations(self.header['run_id'], command_id='decision-' + str(self.sequence),
            expected_revision=snapshot['revision'], expected_generation=snapshot['generation'],
            operations=actions, application_state=app)

    def execute_one(self):
        self.sdk.flush()
        self.assertIsNotNone(self.runtime.run_once())
        self.sdk.sync()
        self.owner._drain_result_delivery(self.sdk)
        return self.read()

    def seed(self, stage='test_execute', *, peer=False, scoped=False):
        snapshot = self.read()
        app = self.owner._new_application()
        task_id = 'test_execute.g1.scope-001' if scoped else stage
        scope = {'scope_id': 'scope-001', 'behavior_ids': ['behavior-1'], 'gap_obligations': []}
        actions = self.owner._schedule(snapshot, self.header, app, stage, task_id=task_id,
            dependencies=[], activate=not scoped,
            payload={'regression_scope': scope, 'regression_generation': 1} if scoped else None,
            extra_options={'workspace': 'workspaces/tests/original'} if stage == 'test_design' else None)
        if peer:
            peer_id = 'test_review.g1.scope-002' if scoped else 'test_review.peer'
            peer_actions = self.owner._schedule(snapshot, self.header, app, 'test_review',
                task_id=peer_id, dependencies=[], activate=False)
            actions += [row for row in peer_actions if row['kind'] != 'dispatch']
        if scoped:
            second_scope = {'scope_id': 'scope-002', 'behavior_ids': ['behavior-2'], 'gap_obligations': []}
            app['active_group'] = {'kind': 'regression', 'generation': 1, 'scopes': [scope, second_scope],
                'members': [task_id, peer_id], 'results': {},
                'artifact_refs': {}, 'review_task_id': 'code_review'}
        self.apply(snapshot, actions, app)
        snapshot = self.execute_one()
        self.assertEqual('failed', snapshot['tasks'][task_id]['attempts'][-1]['result']['value']['status'])
        return snapshot, task_id

    def diagnose(self, snapshot):
        actions, app = self.owner._decision(snapshot, self.header, sdk=self.sdk)
        self.assertFalse(any(row['kind'] in {'finish', 'cancel'} for row in actions))
        episode = app['watchdog']['episodes'][app['watchdog']['active']]
        supervisor_id = episode['supervisor_task_id']
        self.assertTrue(any(row['kind'] == 'dispatch' and row['task_id'] == supervisor_id for row in actions))
        self.apply(snapshot, actions, app)
        snapshot = self.execute_one()
        self.assertEqual('succeeded', snapshot['tasks'][supervisor_id]['attempts'][-1]['state'])
        return snapshot, episode

    def test_failed_design_supervisor_report_repairs_fresh_attempt_and_reaches_consumer(self):
        snapshot, task_id = self.seed('test_design')
        original = snapshot['tasks'][task_id]['attempts'][-1]['command']['payload']
        snapshot, episode = self.diagnose(snapshot)
        command = self.supervisor.commands[-1]
        self.assertEqual(original['command_id'], command.payload['watchdog_incident']['target_execution_id'])
        self.assertEqual(watchdog_supervisor_schema(), report_contract(command)['schema'])
        self.assertIn('conclusively unrecoverable', self.supervisor.prompts[-1])
        self.assertIn('Do not run project code', self.supervisor.prompts[-1])
        context, budget = self.supervisor.budgets[-1]
        self.assertIsNotNone(context)
        self.assertIsNotNone(budget)
        self.assertEqual(self.header['deadline_epoch'], budget.run_deadline)
        actions, app = self.owner._decision(snapshot, self.header, sdk=self.sdk)
        self.assertFalse(any(row['kind'] in {'finish', 'cancel'} for row in actions))
        self.apply(snapshot, actions, app)
        snapshot = self.execute_one()
        retry = snapshot['tasks'][task_id]['attempts'][-1]
        self.assertEqual(2, len(snapshot['tasks'][task_id]['attempts']))
        self.assertNotEqual(original['options']['workspace'], retry['command']['payload']['options']['workspace'])
        self.assertEqual(original['command_id'], (self.root / original['options']['workspace'] / 'retained-author.txt').read_text())
        self.assertEqual('Restore fixture_action and return a fresh author result.',
                         retry['command']['payload']['payload']['watchdog_recovery']['instruction'])
        self.assertEqual(self.header['deadline_epoch'], retry['command']['payload']['options']['deadline_epoch'])
        self.assertEqual(3, snapshot['application_state']['agent_assignments'])
        actions, app = self.owner._decision(snapshot, self.header, sdk=self.sdk)
        result = app['effective'][task_id]
        self.assertEqual('fresh fixture author result', result['outputs']['fixture_result'])
        self.assertEqual(retry['command']['execution_id'], result['command_id'])
        self.assertIn(result['command_id'], app['processed'])
        self.assertEqual(4, app['agent_assignments'])  # Fresh independent review is dispatched by the consumer.

    def test_scoped_failed_executor_keeps_group_and_does_not_cancel_peer(self):
        snapshot, task_id = self.seed(peer=True, scoped=True)
        snapshot, _ = self.diagnose(snapshot)
        actions, app = self.owner._decision(snapshot, self.header, sdk=self.sdk)
        self.assertFalse(any(row['kind'] == 'cancel' for row in actions))
        self.assertIsNone(app['active_stage'])
        self.assertEqual('regression', app['active_group']['kind'])
        self.apply(snapshot, actions, app)
        snapshot = self.execute_one()
        self.assertEqual('planned', snapshot['tasks']['test_review.g1.scope-002']['attempts'][-1]['state'])
        actions, app = self.owner._decision(snapshot, self.header, sdk=self.sdk)
        self.assertEqual('fresh fixture author result', app['active_group']['results'][task_id]['outputs']['fixture_result'])
        self.assertFalse(any(row['kind'] == 'cancel' for row in actions))

    def test_required_acceptance_failure_after_successful_gap_review_dispatches_supervisor(self):
        snapshot = self.read()
        app = self.owner._new_application()
        actions = self.owner._schedule(snapshot, self.header, app, 'gap_review', dependencies=[])
        self.apply(snapshot, actions, app)
        snapshot = self.execute_one()
        self.assertEqual('completed', snapshot['tasks']['gap_review']['attempts'][-1]['result']['value']['status'])
        snapshot, episode = self.diagnose(snapshot)
        self.assertEqual('run_failure', episode['request']['kind'])
        self.assertEqual('required_target_acceptance_incomplete', episode['request']['reason'])
        self.assertNotIn(snapshot['state'], {'failed', 'succeeded', 'cancelled'})
        self.assertEqual('failed', snapshot['application_state']['final_cleanup']['assessment']['status'])
        self.assertNotEqual('complete', snapshot['application_state']['final_cleanup'].get('phase'))

    def test_malformed_supervisor_report_retains_raw_evidence_and_cannot_stop(self):
        self.supervisor.mode = 'invalid'
        snapshot, _ = self.seed()
        snapshot, episode = self.diagnose(snapshot)
        result = snapshot['tasks'][episode['supervisor_task_id']]['attempts'][-1]['result']['value']
        self.assertIsNone(result['outputs']['watchdog_decision'])
        self.assertEqual('invalid report', (self.root / result['outputs']['watchdog_supervisor_raw_report']).read_text())
        actions, app = self.owner._decision(snapshot, self.header, sdk=self.sdk)
        self.assertFalse(any(row['kind'] in {'finish', 'cancel'} for row in actions))
        self.assertIsNone(app['stop_reason'])
        self.assertIsNotNone(app['watchdog']['active'])

    def test_false_budget_stop_is_rejected_with_original_budget_still_available(self):
        self.supervisor.mode = 'stop'
        snapshot, _ = self.seed()
        snapshot, _ = self.diagnose(snapshot)
        actions, app = self.owner._decision(snapshot, self.header, sdk=self.sdk)
        self.assertFalse(any(row['kind'] in {'finish', 'cancel'} for row in actions))
        self.assertIsNone(app['stop_reason'])
        self.assertNotIn('stop_confirmed', app['watchdog'])
        episode = app['watchdog']['episodes'][app['watchdog']['active']]
        self.assertIn('budget remains available', episode['diagnostic'])

    def test_explicit_user_cancel_preserves_cancellation_authority(self):
        snapshot, _ = self.seed(peer=True)
        actions, app = self.owner._decision(snapshot, self.header, sdk=self.sdk,
                                             stop_reason='user_cancelled')
        self.assertTrue(app['user_cancelled'])
        self.assertTrue(any(row['kind'] == 'cancel' and row['task_id'] == 'test_review.peer' for row in actions))
        self.assertFalse(any(row['kind'] == 'add_task' and row['task_id'].startswith('watchdog.') for row in actions))
        self.assertFalse(self.supervisor.commands)
        self.apply(snapshot, actions, app)
        snapshot = self.read()
        self.assertEqual('cancelled', snapshot['tasks']['test_review.peer']['attempts'][-1]['state'])
        actions, app = self.owner._decision(snapshot, self.header, sdk=self.sdk)
        self.assertTrue(any(row['kind'] == 'finish' and row['state'] == 'cancelled' for row in actions))
        self.apply(snapshot, actions, app)
        self.assertEqual('cancelled', self.read()['state'])

    def test_real_deadline_exhaustion_preserves_failed_finish(self):
        snapshot = self.read()
        self.now[0] = self.header['deadline_epoch'] + 1
        self.apply(snapshot, [], self.owner._new_application())
        snapshot = self.read()
        actions, app = self.owner._decision(snapshot, self.header, sdk=self.sdk)
        self.assertTrue(any(row['kind'] == 'finish' and row['state'] == 'failed' for row in actions))
        self.assertFalse(any(row['kind'] == 'add_task' for row in actions))
        self.assertFalse(self.supervisor.commands)
        self.assertEqual('wall_clock_budget_exhausted', app['terminal_reason'])
        self.apply(snapshot, actions, app)
        self.assertEqual('failed', self.read()['state'])

    def test_real_assignment_exhaustion_cannot_dispatch_another_supervisor(self):
        snapshot, _ = self.seed('test_design')
        app = snapshot['application_state']
        app['agent_assignments'] = self.header['request']['budget']['max_agent_assignments']
        self.apply(snapshot, [], app)
        snapshot = self.read()
        actions, app = self.owner._decision(snapshot, self.header, sdk=self.sdk)
        self.assertTrue(any(row['kind'] == 'finish' and row['state'] == 'failed' for row in actions))
        self.assertFalse(any(row['kind'] == 'add_task' for row in actions))
        self.assertFalse(self.supervisor.commands)
        self.assertEqual('agent_assignment_budget_exhausted', app['terminal_reason'])
        self.apply(snapshot, actions, app)
        self.assertEqual('failed', self.read()['state'])

    def test_advisory_cleanup_failure_supervisor_continue_retains_failure_and_allows_downstream(self):
        self.supervisor.mode = 'continue'
        snapshot, task_id = self.seed('research_cleanup')
        failed_attempt = snapshot['tasks'][task_id]['attempts'][-1]
        original = failed_attempt['result']['value']
        snapshot, episode = self.diagnose(snapshot)
        self.assertEqual('task_failure', episode['request']['kind'])
        actions, app = self.owner._decision(snapshot, self.header, sdk=self.sdk)
        self.assertEqual(original, app['effective'][task_id])
        self.assertIn(original['command_id'], app['processed'])
        self.assertIsNone(app['watchdog']['active'])
        self.assertEqual('continued', app['watchdog']['episodes'][episode['request']['incident_id']]['status'])
        self.assertFalse(any(row['kind'] in {'finish', 'cancel', 'new_attempt'} for row in actions))
        self.assertTrue(any(row['kind'] == 'add_task' and row['task_id'] != task_id for row in actions))
        self.assertEqual('failed', self.owner._final_cleanup_acceptance(self.header, app)['status'])
        self.assertNotEqual('passed', app.get('acceptance_status'))
        self.apply(snapshot, actions, app)
        snapshot = self.read()
        self.assertEqual(1, len(snapshot['tasks'][task_id]['attempts']))
        self.assertEqual(original, snapshot['tasks'][task_id]['attempts'][-1]['result']['value'])
        self.assertNotIn(snapshot['state'], {'failed', 'succeeded', 'cancelled'})

    def test_heavy_retry_waits_planned_for_memory_then_dispatches_same_attempt_once(self):
        available = MemorySnapshot(64 * 1024**3, 64 * 1024**3, 'fixture capacity')
        memory = [available]
        self.owner.memory_probe = lambda: memory[0]
        snapshot, task_id = self.seed('test_execute')
        snapshot, episode = self.diagnose(snapshot)
        spent = snapshot['application_state']['agent_assignments']
        memory[0] = MemorySnapshot(None, None, 'fixture observation unavailable')
        actions, app = self.owner._decision(snapshot, self.header, sdk=self.sdk)
        addition = next(row for row in actions if row['kind'] == 'new_attempt')
        execution_id = addition['command']['execution_id']
        self.assertFalse(any(row['kind'] == 'dispatch' for row in actions))
        self.assertEqual(spent, app['agent_assignments'])  # Deterministic execution does not add an agent assignment.
        pending = app['watchdog']['episodes'][app['watchdog']['active']]['resume_pending']
        self.assertEqual(execution_id, pending['execution_id'])
        self.assertEqual('memory_metrics_unavailable', app['memory_waits']['failure_resume']['reason'])
        self.apply(snapshot, actions, app)
        snapshot = self.read()
        self.assertEqual('planned', snapshot['tasks'][task_id]['attempts'][-1]['state'])
        self.assertEqual(2, len(snapshot['tasks'][task_id]['attempts']))
        self.sdk.flush()
        self.assertIsNone(self.runtime.run_once())
        actions, app = self.owner._decision(snapshot, self.header, sdk=self.sdk)
        self.assertFalse(actions)
        self.assertEqual(spent, app['agent_assignments'])
        self.apply(snapshot, actions, app)
        snapshot = self.read()
        memory[0] = available
        actions, app = self.owner._decision(snapshot, self.header, sdk=self.sdk)
        self.assertEqual([{'kind': 'dispatch', 'task_id': task_id}], actions)
        self.assertEqual(spent, app['agent_assignments'])
        self.assertIsNone(app['watchdog']['active'])
        self.apply(snapshot, actions, app)
        snapshot = self.execute_one()
        retry = snapshot['tasks'][task_id]['attempts'][-1]
        self.assertEqual(execution_id, retry['command']['execution_id'])
        self.assertEqual(self.header['deadline_epoch'], retry['command']['payload']['options']['deadline_epoch'])
        self.assertEqual(2, len(snapshot['tasks'][task_id]['attempts']))
        actions, app = self.owner._decision(snapshot, self.header, sdk=self.sdk)
        self.assertEqual(execution_id, app['effective'][task_id]['command_id'])
        self.assertEqual('fresh fixture author result', app['effective'][task_id]['outputs']['fixture_result'])
        self.assertEqual(spent, app['agent_assignments'])

    def test_queued_rework_caller_settles_without_marker_cancels_only_child_and_returns_response(self):
        # The fixture injects a host-bound tool ledger; model/MCP request creation
        # is outside this check. Both caller and child attempts are actual SDK
        # tasks, and the ordinary consumer publishes the settled tool response.
        available = MemorySnapshot(64 * 1024**3, 64 * 1024**3, 'fixture capacity')
        memory = [available]
        self.owner.memory_probe = lambda: memory[0]
        snapshot = self.read()
        app = self.owner._new_application()
        caller_id, child_id, peer_id = 'review.caller', 'agent-rework.repair', 'review.peer'
        caller_actions = self.owner._schedule(snapshot, self.header, app, 'code_review',
            task_id=caller_id, dependencies=[])
        caller_execution = next(row['command']['execution_id'] for row in caller_actions if row['kind'] == 'add_task')
        actions = [row for row in caller_actions if row['kind'] != 'dispatch']
        actions += self.owner._schedule(snapshot, self.header, app, 'agent_rework',
            task_id=child_id, dependencies=[], activate=False,
            payload={'reviewer_workspace': 'worktree', 'reviewer_execution_id': caller_execution,
                     'reviewer_rework': {'request_id': 'repair', 'reviewer_execution_id': caller_execution}})
        peer_actions = self.owner._schedule(snapshot, self.header, app, 'test_review',
            task_id=peer_id, dependencies=[], activate=False)
        actions += [row for row in peer_actions if row['kind'] != 'dispatch']
        app['review_rework'] = {'sequence': 1, 'latest_targets': {}, 'requests': {'repair': {
            'state': 'running', 'task_id': child_id, 'reviewer_execution_id': caller_execution,
            'reviewer_stage': 'code_review', 'target_stage': 'coder', 'target_agent': 'coder.fixture',
            'request_id': 'repair', 'sequence': 1, 'updates': [], 'downstream_toolcall': True,
            'queue_deadline_epoch': self.header['deadline_epoch']}}}
        self.apply(snapshot, actions, app)
        snapshot = self.execute_one()
        original = snapshot['tasks'][child_id]['attempts'][0]['result']['value']
        self.assertEqual('failed', original['status'])
        snapshot, _ = self.diagnose(snapshot)
        memory[0] = MemorySnapshot(None, None, 'fixture observation unavailable')
        actions, app = self.owner._decision(snapshot, self.header, sdk=self.sdk)
        addition = next(row for row in actions if row['kind'] == 'new_attempt')
        queued_execution = addition['command']['execution_id']
        self.assertFalse(any(row['kind'] == 'dispatch' for row in actions))
        self.assertEqual(caller_id, app['active_stage'])
        self.assertEqual('running', app['review_rework']['requests']['repair']['state'])
        self.apply(snapshot, actions, app)
        snapshot = self.read()
        self.assertEqual('planned', snapshot['tasks'][child_id]['attempts'][-1]['state'])
        spent = snapshot['application_state']['agent_assignments']
        # Settle the caller through the public SDK without creating closed.json
        # or a request cancellation marker. The queued child must lose custody.
        self.apply(snapshot, [{'kind': 'dispatch', 'task_id': caller_id}], snapshot['application_state'])
        snapshot = self.execute_one()
        self.assertEqual('succeeded', snapshot['tasks'][caller_id]['attempts'][-1]['state'])
        directory = session_directory(self.root, caller_execution)
        self.assertFalse((directory / 'closed.json').exists())
        self.assertFalse((directory / 'requests/repair.cancel.json').exists())
        actions, app = self.owner._decision(snapshot, self.header, sdk=self.sdk)
        self.assertEqual([child_id], [row['task_id'] for row in actions if row['kind'] == 'cancel'])
        self.assertIn(queued_execution, app['cancel_sent'])
        self.assertEqual(spent, app['agent_assignments'])
        self.apply(snapshot, actions, app)
        snapshot = self.read()
        self.assertEqual('cancelled', snapshot['tasks'][child_id]['attempts'][-1]['state'])
        self.assertEqual('planned', snapshot['tasks'][peer_id]['attempts'][-1]['state'])
        self.assertEqual(original, snapshot['tasks'][child_id]['attempts'][0]['result']['value'])
        actions, app = self.owner._decision(snapshot, self.header, sdk=self.sdk)
        record = app['review_rework']['requests']['repair']
        self.assertEqual('failed', record['state'])
        self.assertTrue(record['recovery_caller_closed'])
        self.assertEqual(queued_execution, record['updates'][-1]['result']['command_id'])
        self.assertFalse(any(row['kind'] == 'cancel' and row['task_id'] == peer_id for row in actions))
        self.apply(snapshot, actions, app)
        project_rework_responses(self.root, self.read()['application_state'])
        response = json.loads((directory / 'responses/repair.json').read_text())
        self.assertEqual('failed', response['status'])
        self.assertEqual(queued_execution, response['updates'][-1]['result']['command_id'])
        self.assertEqual('rework_execution_cancelled', response['updates'][-1]['result']['error_code'])


if __name__ == '__main__':
    unittest.main()
