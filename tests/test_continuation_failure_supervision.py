"""Current continuation drops predecessor control authority, retaining diagnosis."""
from copy import deepcopy
from contextlib import ExitStack
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from dispatcher_sdk.execution_kernel import Kernel
from dispatcher_sdk.orchestrator import Orchestrator

from modport.continuation import prepare_application
from modport.contracts import OperationInput, OperationResult
from modport.kernel_runtime import sdk_handlers
from modport.memory_admission import MemorySnapshot
from modport.models import Budget, MigrationRequest
from modport.operations import MigrationOperations
from modport.workflow import WORKFLOW_VERSION, WorkflowDefinition, compile_migration_workflow


class ContinuationFailureSupervisionTests(unittest.TestCase):
    def predecessor(self, root):
        segment = 'carried-workflow44-segment'
        command = OperationInput('desktop-continuation-fixture',
            'gap_review', 'gap_review', segment + ':gap_review:1', str(root))
        result = OperationResult('completed', command.run_id, command.task_id,
            command.stage_id, command.command_id)
        app = MigrationOperations._new_application()
        app.update(agent_assignments=47, acceptance_status='unverified',
            terminal_reason='required_target_acceptance_incomplete')
        app['effective']['gap_review'] = result.to_dict()
        app['history'].append({'execution_id': command.command_id})
        request = MigrationRequest('fixture-mod', 'https://example.invalid/fixture-mod', '1.20.1', '26.1',
            budget=Budget(max_seconds=172800, max_agent_assignments=140))
        header = {'run_id': segment, 'logical_run_id': command.run_id,
            'run_dir': str(root),
            'definition': WorkflowDefinition(request.to_dict(), version=44).to_dict(),
            'watchdog_policy': {'enabled': False, 'inactivity_seconds': 600},
            'deadline_epoch': 2000000000.0,
            'request': request.to_dict()}
        return {'run_id': segment, 'state': 'failed', 'input': header,
            'application_state': app, 'waits': {}, 'tasks': {
                command.task_id: {'attempts': [{'state': 'succeeded',
                    'command': {'payload': command.to_dict(),
                                'execution_id': command.command_id},
                    'result': {'value': result.to_dict()}}]}}}

    def test_carried_segment_cannot_wait_for_its_missing_old_supervisor(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            state = self.predecessor(root)
            old_segment = 'retired-supervisor-segment'
            episode = {'status': 'pending', 'supervisor_task_id': 'watchdog.g0.1',
                'diagnostic': 'watchdog decision must contain exactly the six protocol fields',
                'request': {'kind': 'terminal', 'target_task_id': 'target_contract_freeze',
                    'target_execution_id': old_segment + ':target_contract_freeze:1',
                    'evidence_refs': [{'path': 'artifacts/watchdog/watchdog.g0.1/target-attempt.json'}]}}
            state['application_state']['watchdog'] = {'authorized': True,
                'active': 'old-contract-failure', 'seen': ['old-contract-failure'],
                'episodes': {'old-contract-failure': episode},
                'resume_controls': {'active_stage': 'target_contract_freeze'},
                'stop_confirmed': {'category': 'unrecoverable', 'reason': 'old diagnosis'}}
            before = deepcopy(state)
            successor = prepare_application(root, state, start_stage='code_cleanup',
                target_workflow_version=WORKFLOW_VERSION)
            self.assertEqual(before, state)
            self.assertEqual({'seen': [], 'episodes': {}}, successor['watchdog'])
            history = successor['continuation_feedback']['previous_watchdog']
            self.assertEqual(state['run_id'], history['previous_segment_id'])
            self.assertEqual(before['application_state']['watchdog'], history['state'])
            self.assertNotIn('watchdog.g0.1', state['tasks'])
            self.assertEqual(47, successor['agent_assignments'])
            self.assertEqual('unverified', successor['acceptance_status'])
            self.assertEqual('code_cleanup', successor['flowthrough_resume']['next_stage'])
            self.assertFalse(state['input']['watchdog_policy']['enabled'])
            self.assertEqual(2000000000.0, state['input']['deadline_epoch'])
            self.assertEqual([], list(root.iterdir()))

    def test_successor_with_no_prior_episode_has_no_invented_diagnosis(self):
        with TemporaryDirectory() as temporary:
            state = self.predecessor(Path(temporary))
            state['application_state'].pop('watchdog', None)
            successor = prepare_application(Path(temporary), state,
                start_stage='code_cleanup', target_workflow_version=WORKFLOW_VERSION)
            self.assertEqual({'seen': [], 'episodes': {}}, successor['watchdog'])
            self.assertNotIn('previous_watchdog', successor['continuation_feedback'])

    def test_explicit_cleanup_restarts_real_consumer_before_old_gap_finalization(self):
        with ExitStack() as stack:
            temporary = stack.enter_context(TemporaryDirectory())
            root = Path(temporary)
            state = self.predecessor(root)
            app = state['application_state']
            for stage in ('behavior_freeze', 'development_integrate', 'code_cleanup',
                          'target_contract_freeze', 'target_build', 'code_review',
                          'test_design', 'test_review', 'test_execute', 'acceptance_preflight',
                          'acceptance_build', 'client_smoke', 'final_cleanup', 'delivery'):
                outputs = {'verification_basis': 'source_reading'} if stage == 'behavior_freeze' else {}
                app['effective'][stage] = OperationResult('completed', state['input']['logical_run_id'],
                    stage, stage, state['run_id'] + ':' + stage + ':1', outputs=outputs).to_dict()
            app['effective']['acceptance_build']['status'] = 'failed'
            app['effective']['acceptance_build']['error_code'] = 'target_evidence_invalid'
            app['effective']['target_contract_freeze']['outputs']['artifact_refs'] = {
                'functional_contract_lock': {'path': 'artifacts/stale-target-contract-lock.json'}}
            app['effective']['acceptance_build']['outputs']['artifact_refs'] = {
                'verification_evidence': {'path': 'artifacts/stale-target-runtime-evidence.json'}}
            app['effective']['acceptance_build.g2'] = deepcopy(app['effective']['acceptance_build'])
            app.update(final_cleanup={'phase': 'complete', 'assessment': {'status': 'passed'}},
                flowthrough_finish_pending=True, execution_status='finished',
                required_behavior_status='failed',
                required_behavior_assessments={'target': {'status': 'failed'}})
            before = deepcopy(state)
            successor = prepare_application(root, state, start_stage='code_cleanup',
                target_workflow_version=WORKFLOW_VERSION)
            self.assertEqual(before, state)
            self.assertTrue(successor['flowthrough_resume']['explicit_target_restart'])
            self.assertEqual('gap_review', successor['flowthrough_resume']['stage'])
            archive = successor['continuation_feedback']['migration_target_restart']
            removed = set(before['application_state']['effective']) - {'behavior_freeze', 'development_integrate'}
            self.assertEqual(removed, set(archive['previous_results']))
            for name in removed:
                self.assertEqual(before['application_state']['effective'][name], archive['previous_results'][name])
            for name in ('final_cleanup', 'flowthrough_finish_pending', 'execution_status',
                         'required_behavior_status', 'required_behavior_assessments'):
                self.assertNotIn(name, successor)
                self.assertEqual(before['application_state'][name], archive['previous_settlement'][name])
            self.assertEqual({'behavior_freeze', 'development_integrate'}, set(successor['effective']))
            runtime = stack.enter_context(Kernel.open_sqlite(root / 'kernel.sqlite3',
                sdk_handlers({'modport.code_cleanup': lambda operation: None}), isolation_mode='thread'))
            sdk = stack.enter_context(Orchestrator(root / 'orchestrator.sqlite3', runtime.kernel, runtime=runtime))
            header = {**state['input'], 'run_id': 'current-continuation-witness',
                'definition': compile_migration_workflow(MigrationRequest.from_mapping(state['input']['request'])).to_dict(),
                'registry_revision': runtime.registry_revision, 'initial_refs': {}, 'prior_findings': [],
                'rubric_sha256': 'host-supplied-provenance'}
            fresh = sdk.create_run(header['run_id'], command_id='create-successor',
                input=header, definition=header['definition'])
            owner = MigrationOperations(clock=lambda: 2000000000.0 - 3600,
                memory_probe=lambda: MemorySnapshot(64 * 1024 ** 3, 64 * 1024 ** 3, 'fixture'))
            actions = owner._advance_without_business_gates(fresh, header, successor)
            self.assertFalse(any(row['kind'] == 'finish' for row in actions))
            scheduled = next(row for row in actions if row['kind'] == 'add_task')
            command = OperationInput.from_dict(scheduled['command']['payload'])
            self.assertEqual('code_cleanup', command.stage_id)
            self.assertTrue(set(command.upstream_results).isdisjoint(removed))
            self.assertNotIn('functional_contract_lock', command.artifact_refs)
            self.assertNotIn('verification_evidence', command.artifact_refs)
            self.assertEqual(48, command.options['agent_assignment'])
            self.assertEqual(header['deadline_epoch'], command.options['deadline_epoch'])
            sdk.apply_operations(header['run_id'], command_id='schedule-explicit-restart',
                expected_revision=fresh['revision'], operations=actions, application_state=successor)
            current = sdk.get_run(header['run_id'])
            self.assertEqual(['code_cleanup'], list(current['tasks']))
            self.assertEqual('running', current['state'])
            self.assertEqual(48, successor['agent_assignments'])
            self.assertEqual('unverified', successor['acceptance_status'])
            self.assertFalse(current['input']['watchdog_policy']['enabled'])


if __name__ == '__main__':
    unittest.main()
