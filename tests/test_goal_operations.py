"""Goal branches are budgeted and joined before their own coder assignments."""
from copy import deepcopy
import tempfile
import unittest
from unittest.mock import patch

from modport.contracts import OperationInput, OperationResult
from modport.models import MigrationRequest
from modport.memory_admission import MemorySnapshot
from modport.operations import MigrationOperations
from modport.workflow import PLANNING_STAGES, REPAIR_PLANNING_STAGES


class GoalOperationsTests(unittest.TestCase):
    def setUp(self):
        self.run_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.run_directory.cleanup)

    def setup_group(self, scope='migration', limit=None, preparation=False):
        policy = MigrationOperations(memory_probe=lambda: MemorySnapshot(64 * 1024**3, 64 * 1024**3, "fixture"))
        app = policy._new_application()
        request = MigrationRequest('unit', 'https://example.invalid/mod.git', '1.20.1', '26.1.2').to_dict()
        request['budget']['max_agent_assignments'] = limit
        request['max_parallel_coders'] = 2
        header = {'request': request, 'deadline_epoch': None, 'run_dir': self.run_directory.name,
                  'initial_refs': {}, 'prior_findings': [], 'registry_revision': 'a' * 64,
                  'rubric_sha256': 'b' * 64}
        stages = PLANNING_STAGES if scope == 'migration' else tuple(
            stage for stage in REPAIR_PLANNING_STAGES if stage.startswith(scope + '_'))
        refs = {stage: {'path': 'artifacts/' + stage, 'sha256': str(index) * 64}
                for index, stage in enumerate(stages)}
        header['initial_refs'] = refs
        tasks = [{'id': name, 'objective': name, 'owned_paths': [name], 'acceptance': ['works'],
                  'dependencies': [], 'model': 'gpt-5.6-luna', 'reasoning_effort': 'max'}
                 for name in ['a', 'b']]
        entry = 'implementation' if scope == 'migration' else scope + '_revise'
        if preparation:
            entry = 'development_prepare'
        policy._start_development_group(header, app, entry, {
            'development_tasks': tasks, 'development_base': 'a' * 40, 'goal_scope': scope,
            **({'development_kind': 'preparation'} if preparation else {}),
            'development_source_workspace': 'workspaces/source',
            'artifact_refs': {'development_plan': {'path': 'artifacts/plan', 'sha256': 'a' * 64}}})
        snapshot = {'run_id': 'unit', 'state': 'running', 'tasks': {}, 'waits': {}, 'application_state': app}
        return policy, header, app, snapshot, refs

    def settle(self, snapshot, changes, alias):
        for item in changes:
            if item['kind'] != 'add_task':
                continue
            command = OperationInput.from_dict(item['command']['payload'])
            ref = {'path': 'artifacts/' + command.task_id, 'sha256': 'f' * 64}
            result = OperationResult('completed', command.run_id, command.task_id, command.stage_id,
                                     command.command_id, {'artifact_refs': {alias: ref}})
            snapshot['tasks'][command.task_id] = {'attempts': [{'state': 'succeeded',
                'command': item['command'], 'result': {'value': result.to_dict()}}]}

    def test_fifth_round_parallel_then_each_coder_gets_own_goal_and_all_context(self):
        policy, header, app, snapshot, refs = self.setup_group()
        goals = policy._group_decision(snapshot, header, app)
        self.assertEqual(['goal.g1.a', 'goal.g1.b'], [x['task_id'] for x in goals if x['kind'] == 'dispatch'])
        for item in [x for x in goals if x['kind'] == 'add_task']:
            self.assertEqual(refs, item['command']['payload']['payload']['planning_context'])
        self.assertEqual(2, app['agent_assignments'])
        self.assertEqual([], policy._group_decision(snapshot, header, app))
        self.settle(snapshot, goals, 'coder_goal')
        coders = policy._group_decision(snapshot, header, app)
        self.assertEqual(4, app['agent_assignments'])
        for item in [x for x in coders if x['kind'] == 'add_task']:
            payload = item['command']['payload']
            name = payload['payload']['development_task']['id']
            self.assertEqual('artifacts/goal.g1.' + name, payload['artifact_refs']['coder_goal']['path'])
            self.assertEqual(['goal.g1.' + name], item['dependencies'])
            self.assertEqual(refs, payload['payload']['planning_context'])

    def test_goal_budget_exhaustion_never_dispatches_a_coder(self):
        policy, header, app, snapshot, _ = self.setup_group(limit=1)
        changes = policy._group_decision(snapshot, header, app)
        self.assertEqual(['goal.g1.a'], [x['task_id'] for x in changes if x['kind'] == 'dispatch'])
        self.assertEqual(1, app['agent_assignments'])
        self.assertEqual('agent_assignment_budget_exhausted', app['stop_reason'])

    def test_early_contract_repair_joins_into_repair_integration(self):
        policy, header, app, snapshot, _ = self.setup_group('contract')
        app['early_active'] = True
        goals = policy._group_decision(snapshot, header, app)
        self.settle(snapshot, goals, 'coder_goal')
        coders = policy._group_decision(snapshot, header, app)
        self.settle(snapshot, coders, 'coder_patch')
        changes = policy._group_decision(snapshot, header, app)
        self.assertIsNone(app['active_group'])
        self.assertIsNone(app['active_stage'])
        self.assertEqual(['contract_repair_integrate'], app['early_pending'])
        command = next(x['command']['payload'] for x in changes if x['kind'] == 'add_task')
        self.assertEqual('contract_repair_integrate', command['stage_id'])
        self.assertEqual('workspaces/source', command['payload']['development_source_workspace'])
        self.assertEqual(2, len(command['payload']['development_results']))

    def test_contract_goal_ignores_unrelated_migration_knowledge(self):
        policy, header, app, snapshot, _ = self.setup_group('contract')
        with patch.object(policy, '_knowledge_gaps',
                return_value=[{'gap_id': 'target-only', 'affected_tasks': []}]):
            goals = policy._group_decision(snapshot, header, app)
            self.settle(snapshot, goals, 'coder_goal')
            coders = policy._group_decision(snapshot, header, app)
        self.assertEqual(['coder.g1.a', 'coder.g1.b'], [x['task_id'] for x in coders if x['kind'] == 'dispatch'])
        for item in [x for x in coders if x['kind'] == 'add_task']:
            self.assertEqual([], item['command']['payload']['payload']['unresolved_knowledge_gaps'])

    def test_preparation_goals_join_then_return_to_independent_review(self):
        policy, header, app, snapshot, _ = self.setup_group(preparation=True)
        goals = policy._group_decision(snapshot, header, app)
        self.settle(snapshot, goals, 'coder_goal')
        coders = policy._group_decision(snapshot, header, app)
        for item in [x for x in coders if x['kind'] == 'add_task']:
            self.assertEqual('preparation', item['command']['payload']['payload']['development_kind'])
        self.settle(snapshot, coders, 'coder_patch')
        changes = policy._group_decision(snapshot, header, app)
        self.assertEqual('development_prepare_integrate', app['active_stage'])
        self.settle(snapshot, changes, 'development_prepare')
        changes, updated = policy._advance_decision(snapshot, header)
        self.assertEqual('parallel_review', updated['active_stage'])
        self.assertEqual('development_prepare_integrate', updated['effective']['development_prepare']['stage_id'])
        review = next(x['command']['payload'] for x in changes if x['kind'] == 'add_task')
        self.assertIn('development_prepare', review['artifact_refs'])

    def test_deferred_criteria_are_retained_until_final_verification(self):
        policy, header, app, snapshot, _ = self.setup_group()
        row = {'id': 'migration_inventory:later', 'closure_criteria': ['old save preserved'],
               'resolution_stage': 'test_execute'}
        result = OperationResult('completed', 'unit', 'parallel_review', 'parallel_review', 'review:1',
            {'parallel_decision': 'parallel', 'deferred_obligations': [row],
             'artifact_refs': {'parallel_review': {'path': 'artifacts/review', 'sha256': 'b' * 64}}})
        policy._ingest_deferred_obligations(app, result)
        self.assertEqual('pending', app['project_verification_gaps'][row['id']]['project_status'])
        changed = deepcopy(result.to_dict())
        changed['outputs']['deferred_obligations'][0]['closure_criteria'] = ['additional runtime test']
        policy._ingest_deferred_obligations(app, OperationResult.from_dict(changed))
        self.assertEqual(['old save preserved', 'additional runtime test'],
                         app['project_verification_gaps'][row['id']]['closure_criteria'])
