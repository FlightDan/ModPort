"""Scoped regression routing without a model, Gradle, or SDK worker."""
from pathlib import Path
import tempfile
import unittest

from modport.characterization import BehaviorEntry, CharacterizationContract
from modport.contracts import OperationInput, OperationResult
from modport.evidence import atomic_json, file_digest
from modport.regression import partition_scopes, regression_decision


class Policy:
    def __init__(self, budget=None):
        self.dispatched = []
        self.budget = budget
        self.assignments = 0

    def _schedule(self, snapshot, header, app, stage, **kwargs):
        if stage == 'test_design':
            if self.budget is not None and self.assignments >= self.budget:
                app['stop_reason'] = 'agent_assignment_budget_exhausted'
                return []
            self.assignments += 1
        identifier = kwargs.get('task_id', stage)
        self.dispatched.append((stage, kwargs))
        return [{'kind': 'dispatch', 'task_id': identifier}]

    def _capture_repair_result(self, *args):
        pass

    def _repair_failure(self, snapshot, header, app, stage, failure, execution_id):
        return [{'kind': 'repair', 'stage': stage, 'failure': failure.command_id}]


class RegressionOperationsTests(unittest.TestCase):
    def setUp(self):
        self.scopes = [{'scope_id': 'scope-001', 'behavior_ids': ['a'], 'gap_obligations': []},
                       {'scope_id': 'scope-002', 'behavior_ids': ['b'], 'gap_obligations': []}]
        self.header = {'request': {'max_parallel_coders': 2}}
        self.group = {'kind': 'regression', 'generation': 1, 'scopes': self.scopes,
                      'members': [], 'results': {}, 'artifact_refs': {'contract': {'path': 'lock'}},
                      'review_task_id': 'code_review'}
        self.app = {'active_group': self.group, 'processed': [], 'effective': {}, 'history': [],
                    'stop_reason': None}
        self.snapshot = {'tasks': {}}
        self.policy = Policy()

    def decide(self):
        return regression_decision(self.policy, self.snapshot, self.header, self.app)

    def complete(self, stage, scope, *, status='completed'):
        task_id = f'{stage}.g1.{scope["scope_id"]}'
        execution_id = task_id + ':1'
        command = OperationInput('run', task_id, stage, execution_id, '/tmp/regression-fixture',
                                 payload={'regression_scope': scope})
        result = OperationResult(status, 'run', task_id, stage, execution_id,
            error_code='test_failure' if status != 'completed' else None,
            outputs={'regression_scope': scope, 'workspace': 'workspaces/tests/' + scope['scope_id'],
                     'artifact_refs': {'independent_test_snapshot': {'path': scope['scope_id']}}})
        self.snapshot['tasks'][task_id] = {'attempts': [{'state': 'succeeded',
            'command': {'execution_id': execution_id, 'payload': command.to_dict()},
            'result': {'value': result.to_dict()}}]}

    def test_parallel_designs_pipeline_and_authenticated_join_wait_for_all_scopes(self):
        first = self.decide()
        self.assertEqual(2, len(first))
        self.assertTrue(all(row['task_id'].startswith('test_design.') for row in first))
        self.assertEqual([], self.decide())  # Dispatches not yet published still occupy slots.
        self.complete('test_design', self.scopes[0])
        ready = self.decide()
        self.assertEqual(['test_execute.g1.scope-001'], [row['task_id'] for row in ready])
        stage, request = self.policy.dispatched[-1]
        self.assertEqual(['test_design.g1.scope-001'], request['dependencies'])
        self.assertEqual('scope-001', request['artifact_overrides']['independent_test_snapshot']['path'])
        self.complete('test_execute', self.scopes[0])
        self.assertEqual([], self.decide())
        self.complete('test_design', self.scopes[1])
        self.decide()
        self.complete('test_execute', self.scopes[1])
        joined = self.decide()
        self.assertEqual([{'kind': 'dispatch', 'task_id': 'test_execute.g1.join'}], joined)
        self.assertIsNone(self.app['active_group'])
        stage, request = self.policy.dispatched[-1]
        self.assertEqual(2, len(request['payload']['regression_results']))
        self.assertEqual(2, len(request['payload']['regression_designs']))
        self.assertEqual(['test_execute.g1.scope-001', 'test_execute.g1.scope-002'], request['dependencies'])
        self.assertEqual('test_design.g1.scope-001:1', self.app['effective']['test_design']['command_id'])

    def test_business_failure_waits_for_other_scope_then_routes_test_failure(self):
        self.decide()
        self.complete('test_design', self.scopes[0], status='failed')
        self.assertEqual([], self.decide())
        self.assertEqual(2, len(self.policy.dispatched))
        self.complete('test_design', self.scopes[1])
        result = self.decide()
        self.assertEqual('test_design', result[0]['stage'])
        self.assertEqual('repair', result[0]['kind'])
        self.assertEqual(2, len(self.app['history']))
        self.assertIsNone(self.app['active_group'])

    def test_budget_exhaustion_does_not_record_undispatched_scope(self):
        self.policy = Policy(budget=1)
        self.assertEqual(1, len(self.decide()))
        self.assertEqual('agent_assignment_budget_exhausted', self.app['stop_reason'])
        self.assertEqual(['test_design.g1.scope-001'], self.group['members'])

    def test_partition_is_complete_disjoint_and_preserves_unmapped_obligations(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            contract = CharacterizationContract(contract_id='test', entries=tuple(
                BehaviorEntry(entry_id=name, behavior_source='source') for name in ('c', 'a', 'b')))
            atomic_json(root / 'contract.json', {'contract': contract.to_dict()})
            refs = {'functional_contract_lock': {'path': 'contract.json', 'sha256': file_digest(root / 'contract.json')}}
            obligation = {'id': 'save', 'due_stage': 'test_execute', 'closure_criteria': ['old save loads']}
            scopes = partition_scopes(root, refs, [obligation], 2)
            self.assertEqual(2, len(scopes))
            self.assertEqual(['a', 'b', 'c'], sorted(item for scope in scopes for item in scope['behavior_ids']))
            self.assertEqual([obligation], [item for scope in scopes for item in scope['gap_obligations']])
            self.assertEqual(scopes, partition_scopes(root, refs, [obligation], 2))

if __name__ == '__main__':
    unittest.main()
