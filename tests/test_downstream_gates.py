"""Business diagnostics never authorize upstream assignments by themselves."""
from unittest.mock import patch
import unittest

from modport.contracts import OperationInput, OperationResult
from modport.gate_policy import GATE_POLICY
from modport.workflow import compile_migration_workflow
import test_planning_operations


class DownstreamGateTests(unittest.TestCase):
    def setUp(self):
        self.fixture = test_planning_operations.PlanningPolicyTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.header['definition']['workflow_version'] = 16
        self.fixture.header['definition']['gate_policy'] = dict(GATE_POLICY)

    def settle_consumer(self, command, outputs=None):
        f = self.fixture
        attempt = f.snapshot['tasks'][command.task_id]['attempts'][-1]
        result = OperationResult('completed', command.run_id, command.task_id,
            command.stage_id, command.command_id, outputs or {})
        attempt.update(state='succeeded', result={'value': result.to_dict()})
        return f.operations._gate_handoff_decision(f.snapshot, f.header, f.app)

    def test_every_failed_gate_dispatches_only_a_downstream_consumer(self):
        stages = ('mod_analysis', 'research_review', 'contract_verify', 'contract_review',
                  'migration_tasks', 'parallel_review', 'implementation', 'target_build',
                  'test_execute', 'client_smoke', 'gap_review', 'delivery')
        for stage in stages:
            with self.subTest(stage=stage):
                f = self.fixture
                outcome = OperationResult('failed', 'policy', stage, stage,
                    'failed:' + stage, {'verdict': 'rejected', 'replan_stage': 'migration_inventory'},
                    error_code='planning_artifact_invalid')
                operations = f.operations._repair_failure(f.snapshot, f.header, f.app,
                    stage, outcome, outcome.command_id)
                commands = f.scheduled(operations)
                self.assertEqual(['gate_handoff'], [row.stage_id for row in commands])
                self.assertEqual('downstream_toolcall', commands[0].options['gate_policy'])
                self.assertEqual({}, f.app['rounds'])
                self.assertEqual({}, f.app['format_retries'])
                self.assertFalse(any(row['kind'] in {'cancel', 'finish'} for row in operations))

    def test_consumer_prose_rejection_does_not_dispatch_rework_or_pass_failed_build(self):
        f = self.fixture
        commands = f.scheduled(f.settle('target_build', status='failed', error_code='build_failed'))
        self.assertEqual(['gate_handoff'], [row.stage_id for row in commands])
        operations = self.settle_consumer(commands[0], {'verdict': 'rejected',
            'replan_stage': 'migration_inventory', 'report': 'The author should repair this.'})
        self.assertEqual(['code_review'], [row.stage_id for row in f.scheduled(operations)])
        self.assertEqual('failed', f.app['effective']['target_build']['status'])
        self.assertEqual({}, f.app['rounds'])
        self.assertEqual('forwarded', next(iter(f.app['gate_diagnostics'].values()))['state'])
        self.assertEqual([], self.settle_consumer(commands[0]))

    def test_failed_consumer_does_not_forward_the_diagnostic(self):
        f = self.fixture
        consumer = f.scheduled(f.settle(
            'target_build', status='failed', error_code='build_failed'))[0]
        attempt = f.snapshot['tasks'][consumer.task_id]['attempts'][-1]
        attempt.update(state='failed', result=None)

        operations = f.operations._gate_handoff_decision(f.snapshot, f.header, f.app)

        record = next(iter(f.app['gate_diagnostics'].values()))
        self.assertEqual('consumer_failed', record['state'])
        self.assertEqual('blocked', record['consumer_result']['status'])
        self.assertEqual([], f.app.get('diagnostic_forwarded', []))
        self.assertEqual([], f.scheduled(operations))
        self.assertEqual('execution_failed', f.app['terminal_reason'])
        self.assertEqual(['failed'], [row['state'] for row in operations
                                      if row['kind'] == 'finish'])

    def test_blocked_consumer_does_not_forward_the_diagnostic(self):
        f = self.fixture
        consumer = f.scheduled(f.settle(
            'target_build', status='failed', error_code='build_failed'))[0]
        attempt = f.snapshot['tasks'][consumer.task_id]['attempts'][-1]
        blocked = OperationResult(
            'blocked', consumer.run_id, consumer.task_id, consumer.stage_id,
            consumer.command_id, error_code='handoff_report_missing')
        attempt.update(state='succeeded', result={'value': blocked.to_dict()})

        operations = f.operations._gate_handoff_decision(f.snapshot, f.header, f.app)

        record = next(iter(f.app['gate_diagnostics'].values()))
        self.assertEqual('consumer_failed', record['state'])
        self.assertEqual([], f.app.get('diagnostic_forwarded', []))
        self.assertEqual('handoff_report_missing', f.app['terminal_reason'])
        self.assertEqual([], f.scheduled(operations))

    def test_continued_gap_handoff_restores_saved_join_route(self):
        f = self.fixture
        f.app['continuation_feedback'] = {
            'gate_failure': {'command_id': 'gap:failed'}, 'resume': {
                'location': 'gap', 'active_stage': None, 'early_active': False,
                'gap_join_stage': 'development_integrate', 'gap_join_ids': ['gap-a'],
            }}
        failure = OperationResult(
            'failed', 'policy', 'gap_research', 'gap_research', 'gap:failed',
            error_code='research_failed')
        changes = f.operations._diagnostic_handoff(
            f.snapshot, f.header, f.app, 'gap_research', failure,
            failure.command_id, location='gap')
        consumer = f.scheduled(changes)[0]
        command = next(row['command'] for row in changes if row['kind'] == 'add_task')
        f.snapshot['tasks'][consumer.task_id] = {
            'attempts': [{'state': 'running', 'command': command}]}

        self.assertEqual([], self.settle_consumer(consumer))
        operations, app = f.operations._decision(f.snapshot, f.header)

        self.assertEqual(['development_integrate'],
                         [row.stage_id for row in f.scheduled(operations)])
        self.assertNotEqual('source', app.get('active_stage'))

    def test_continued_support_handoff_without_saved_route_fails(self):
        f = self.fixture
        f.app['continuation_feedback'] = {
            'gate_failure': {'command_id': 'support:failed'}, 'resume': {
                'location': 'support', 'active_stage': None, 'early_active': False,
                'gap_join_stage': None, 'gap_join_ids': None,
            }}
        failure = OperationResult(
            'failed', 'policy', 'gap_plan_review', 'gap_plan_review',
            'support:failed', error_code='review_rejected')
        changes = f.operations._diagnostic_handoff(
            f.snapshot, f.header, f.app, 'gap_plan_review', failure,
            failure.command_id, location='support')
        consumer = f.scheduled(changes)[0]
        command = next(row['command'] for row in changes if row['kind'] == 'add_task')
        f.snapshot['tasks'][consumer.task_id] = {
            'attempts': [{'state': 'running', 'command': command}]}

        operations = self.settle_consumer(consumer)

        self.assertEqual('continuation_route_incomplete', f.app['terminal_reason'])
        self.assertEqual([], f.scheduled(operations))
        self.assertEqual(['failed'], [row['state'] for row in operations
                                      if row['kind'] == 'finish'])

    def test_carried_stage_with_old_task_alias_has_no_unknown_dependency(self):
        f = self.fixture
        f.header['continuation'] = {'carried_stages': ['target_build']}
        f.app['effective']['target_build'] = OperationResult(
            'completed', 'policy', 'agent-rework.old.verify', 'target_build',
            'old-recheck:1').to_dict()

        operations = f.operations._schedule(
            {'run_id': f.snapshot['run_id'], 'tasks': {}}, f.header, f.app,
            'code_review')

        task = next(row for row in operations if row['kind'] == 'add_task')
        self.assertEqual([], task['dependencies'])

    def test_delivery_rejects_retained_failed_development_group(self):
        f = self.fixture
        from modport.workflow import MAIN_STAGES, REVIEW_STAGES
        for stage in MAIN_STAGES:
            if stage in {'contract_draft', 'implementation', 'delivery'}:
                continue
            outputs = {'verdict': 'approved'} if stage in REVIEW_STAGES else {}
            f.app['effective'][stage] = OperationResult(
                'completed', 'policy', stage, stage, stage + ':ok', outputs).to_dict()
        f.app['effective']['implementation'] = OperationResult(
            'completed', 'policy', 'implementation', 'implementation',
            'implementation:ok').to_dict()
        failure = OperationResult(
            'failed', 'policy', 'coder.g1.task', 'coder', 'coder:failed',
            error_code='goal_failed').to_dict()
        f.app['failed_development_group'] = {
            'kind': 'development', 'members': ['coder.g1.task'],
            'results': {'coder.g1.task': failure},
        }

        operations = f.settle('delivery')

        self.assertEqual('acceptance_gates_incomplete', f.app['terminal_reason'])
        self.assertEqual(['failed'], [row['state'] for row in operations
                                      if row['kind'] == 'finish'])

    def test_early_failure_does_not_cancel_peer_or_terminate_run(self):
        f = self.fixture
        f.app.update(early_active=True, early_pending=['contract_verify'])
        original = OperationInput('policy', 'contract_verify', 'contract_verify',
            'verify:1', f.header['run_dir'])
        result = OperationResult('blocked', 'policy', 'contract_verify', 'contract_verify',
            original.command_id, error_code='contract_test_failed')
        f.snapshot['tasks']['contract_verify'] = {'attempts': [{'state': 'succeeded',
            'command': {'execution_id': original.command_id, 'payload': original.to_dict()},
            'result': {'value': result.to_dict()}}]}
        with patch('modport.operations.EARLY_STAGES', ()):
            operations = f.operations._early_decision(f.snapshot, f.header, f.app)
        self.assertEqual(['gate_handoff'], [row.stage_id for row in f.scheduled(operations)])
        self.assertEqual([], f.app['early_pending'])
        self.assertFalse(f.app.get('terminal_reason'))
        self.assertFalse(f.app.get('stop_reason'))
        self.assertFalse(any(row['kind'] == 'cancel' for row in operations))

    def test_missing_resumption_does_not_mutate_idle_state(self):
        from copy import deepcopy
        f = self.fixture
        f.app.pop('gate_resumptions', None)
        before = deepcopy(f.app)
        original = {'state': 'running'}
        for _ in range(25):
            selected, resumed = f.operations._resumed_attempt(
                f.snapshot, f.app, 'target_build', original)
            self.assertIs(original, selected)
            self.assertFalse(resumed)
        self.assertEqual(before, f.app)

    def test_explicit_tool_result_resumes_from_real_sdk_attempt(self):
        f = self.fixture
        consumer = f.scheduled(f.settle('target_build', status='failed', error_code='build_failed'))[0]
        source = OperationInput('policy', 'agent-rework.one', 'target_build',
            'requested-build:1', f.header['run_dir'], payload={'reviewer_rework': {'request_id': 'one'}})
        result = OperationResult('completed', source.run_id, source.task_id, source.stage_id,
                                 source.command_id)
        attempt = {'state': 'succeeded', 'command': {'execution_id': source.command_id,
            'payload': source.to_dict()}, 'result': {'value': result.to_dict()}}
        f.snapshot['tasks'][source.task_id] = {'attempts': [attempt]}
        f.app['review_rework'] = {'requests': {'one': {'sequence': 1, 'state': 'completed',
            'reviewer_execution_id': consumer.command_id, 'target_agent': 'target_build',
            'updates': [{'target_agent': 'target_build', 'stage': 'target_build',
                         'result': result.to_dict()}]}}}
        self.assertEqual([], self.settle_consumer(consumer))
        selected, resumed = f.operations._resumed_attempt(f.snapshot, f.app, 'target_build', {})
        self.assertTrue(resumed)
        self.assertIs(attempt, selected)
        original = f.snapshot['tasks']['target_build']['attempts'][-1]
        self.assertEqual('failed', original['result']['value']['status'])

    def test_unrequested_result_cannot_satisfy_handoff(self):
        f = self.fixture
        consumer = f.scheduled(f.settle('target_build', status='failed', error_code='build_failed'))[0]
        f.app['effective']['target_build'] = OperationResult('completed', 'policy', 'target_build',
            'target_build', 'unrelated').to_dict()
        self.settle_consumer(consumer)
        self.assertEqual({}, f.app.get('gate_resumptions', {}))
        self.assertEqual('forwarded', next(iter(f.app['gate_diagnostics'].values()))['state'])

    def test_delivery_diagnostic_never_becomes_success(self):
        f = self.fixture
        consumer = f.scheduled(f.settle('delivery', status='blocked', error_code='missing_evidence'))[0]
        operations = self.settle_consumer(consumer)
        finishes = [row for row in operations if row['kind'] == 'finish']
        self.assertEqual('failed', finishes[0]['state'])
        self.assertEqual('delivery_incomplete', f.app['terminal_reason'])

    def test_partial_skill_publication_cannot_hide_failed_skill_group(self):
        f = self.fixture
        f.header['request']['workflow_mode'] = 'skill_generation'
        f.app['effective']['skill_lookup'] = OperationResult('completed', 'policy',
            'skill_lookup', 'skill_lookup', 'lookup:1', {'missing_kinds': ['platform']}).to_dict()
        f.app['effective']['platform_diff'] = OperationResult('failed', 'policy',
            'platform_diff', 'platform_diff', 'diff:1', error_code='research_failed').to_dict()
        f.app['failed_development_group'] = {'kind': 'skills', 'kinds': ['platform']}
        operations = f.settle('skill_publish')
        self.assertEqual('skills_incomplete', f.app['terminal_reason'])
        self.assertEqual(['failed'], [row['state'] for row in operations if row['kind'] == 'finish'])
