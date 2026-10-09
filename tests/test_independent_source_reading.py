"""Consume the actual source-reading freeze at independent author preparation."""
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from modport.contracts import OperationInput, OperationResult
from modport.evidence import atomic_json
from modport.independent_tests import TestDesignHandler, _contract
from modport.regression import partition_scopes
from modport.target_contract import TargetContractFreezeHandler
from modport.workflow import WORKFLOW_VERSION


class IndependentSourceReadingTests(unittest.TestCase):
    def setUp(self):
        folder = TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.rubric = {'rubric_id': 'target-behavior', 'rubric_version': 1}
        requirements = {'schema_version': 1, 'behaviors': [{
            'behavior_id': 'damage', 'description': 'Damage updates health',
            'source_anchors': [{'path': 'src/Health.java', 'symbol': 'Health.damage'}],
            'assertions': [{'assertion_id': 'damage.health',
                            'expected': 'Health decreases by the configured damage'}]}]}
        candidate = {'schema_version': 1, 'behaviors': [{
            'id': 'damage', 'side': 'server', 'preconditions': ['live target player'],
            'action': ['apply damage'], 'test_mapping': ['target.damage'],
            'assertion_contracts': [{'assertion_id': 'damage.health',
                'text': 'Health decreases by the configured damage',
                'test_ids': ['target.damage']}]}],
            'baseline_gradle_tasks': ['runGameTestServer'],
            'baseline_evidence_files': ['.modport/evidence/target.damage.json'],
            'test_evidence': {'target.damage': {
                'path': '.modport/evidence/target.damage.json',
                'evidence_kind': 'runtime', 'executor': 'gametest',
                'runtime_operations': ['apply damage'],
                'test_source_files': ['.modport/harness/HealthGameTest.java'],
                'result_identity': {'kind': 'junit_xml', 'gradle_task': 'runGameTestServer',
                                    'classname': 'HealthGameTest', 'name': 'damage'}}}}
        atomic_json(self.root / 'artifacts/requirements.json', requirements)
        atomic_json(self.root / 'artifacts/rubric.json', self.rubric)
        atomic_json(self.root / 'worktree/.modport/functional-contract.json', candidate)
        harness = self.root / 'worktree/.modport/harness/HealthGameTest.java'
        harness.parent.mkdir(parents=True)
        harness.write_text('class HealthGameTest {}\n')
        self.refs = {'behavior_requirements': {'path': 'artifacts/requirements.json'},
                     'acceptance_rubric': {'path': 'artifacts/rubric.json'}}
        freeze = TargetContractFreezeHandler()(self.command('target_contract_freeze'))
        self.assertEqual('completed', freeze.status, freeze.detail)
        self.refs.update(freeze.outputs['artifact_refs'])
        self.lock = json.loads((self.root / self.refs['functional_contract_lock']['path']).read_text())

    def command(self, stage, *, payload=None):
        return OperationInput('run', stage, stage, stage + '-current', str(self.root),
            artifact_refs=self.refs, payload=payload or {}, options={
                'workflow_version': WORKFLOW_VERSION, 'workspace': 'workspaces/tests/current',
                'business_gates_disabled': True})

    def test_freeze_contract_partition_and_design_reach_author_dispatch(self):
        assertion = self.lock['contract']['behaviors'][0]['assertion_contracts'][0]
        self.assertEqual([{'path': 'src/Health.java', 'symbol': 'Health.damage'}],
                         assertion['source_anchors'])
        self.assertNotIn('source_anchor', assertion)
        scopes = partition_scopes(self.root, self.refs, [], 3, separate_static=True)
        self.assertEqual(['damage'], scopes[0]['runtime_behavior_ids'])
        self.assertEqual([], scopes[0]['static_behavior_ids'])
        dispatched = []

        def author(handler, command):
            workspace = self.root / command.options['workspace']
            self.assertTrue(workspace.is_dir())
            self.assertEqual((self.root / 'worktree/.modport/functional-contract.json').read_text(),
                             (workspace / '.modport/functional-contract.json').read_text())
            self.assertTrue((workspace / '.modport/harness/HealthGameTest.java').is_file())
            self.assertIn('"runtime_behavior_ids":["damage"]', handler.prompt)
            dispatched.append(command)
            # Stop at the author boundary: this regression executes no model,
            # project harness, or content identity verification.
            return OperationResult('blocked', command.run_id, command.task_id,
                command.stage_id, command.command_id, detail='author dispatch observed')

        command = self.command('test_design', payload={'regression_scope': scopes[0]})
        with patch('modport.handlers._acceptance_rubric_for', return_value=self.rubric), \
                patch('modport.handlers.CodexStageHandler.__call__', author):
            result = TestDesignHandler()(command)
        self.assertEqual([command], dispatched)
        self.assertEqual('blocked', result.status)
        self.assertEqual('author dispatch observed', result.detail)
        self.assertNotIn('assertion source_anchor must be an object',
                         result.outputs.get('business_diagnostics', []))

    def test_current_contract_bindings_copy_only_supplied_metadata(self):
        with patch('modport.handlers._acceptance_rubric_for', return_value=self.rubric):
            ids, bindings, static_ids = _contract(self.command('test_design'), self.root)
        self.assertEqual({'damage'}, ids)
        self.assertEqual(set(), static_ids)
        self.assertEqual({**self.rubric, 'contract_schema_version': 1}, bindings)
        self.assertNotIn('contract_id', bindings)


if __name__ == '__main__':
    unittest.main()
