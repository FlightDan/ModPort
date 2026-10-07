"""Source reading freezes requirements without source runtime obligations."""
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from modport.behavior_requirements import (
    BehaviorFreezeHandler, requirements_from_legacy_contract,
    source_reading_policy, validate_requirements,
)
from modport.contracts import OperationInput
from modport.prompts import STAGE_PROMPTS, build_prompt
from modport.report_dialogue import materialize_report, prepare_dialogue
from modport.planning import _obligations


class BehaviorRequirementsTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        (self.root / 'artifacts').mkdir()
        (self.root / 'artifacts/source.json').write_text(json.dumps({'source_commit': 'host-source'}))

    def requirements(self):
        return {'schema_version': 1, 'behaviors': [{
            'behavior_id': 'health', 'description': 'Health persists after reconnect',
            'source_anchors': [{'path': 'src/Health.java', 'symbol': 'save'}],
            'assertions': [{'assertion_id': 'health.persist', 'expected': 'Restored health equals saved health'}],
        }]}

    def command(self, stage='behavior_freeze', **kwargs):
        return OperationInput('run', stage, stage, 'execution-' + stage, str(self.root),
            options={'workflow_version': 34, 'workflow_mode': 'artifact_verification',
                     'validation_policy': {'scope': 'artifact_verification',
                                           'required_behavior_completion': True}}, **kwargs)

    def test_carried_contract_conversion_discards_test_results_and_freezes_without_source_execution(self):
        legacy = {'source_contract': {'schema_version': 1, 'behaviors': [{
            'id': 'health', 'source_evidence': 'Health.save preserves player state',
            'assertion_contracts': [{'assertion_id': 'health.persist',
                'text': 'Restored health equals saved health',
                'source_anchor': {'path': 'src/Health.java', 'start_line': 10, 'end_line': 14,
                                  'file_sha256': 'historical-metadata'},
                'test_ids': ['old.source.test']}],
            'test_mapping': ['old.source.test']}],
            'baseline_gradle_tasks': ['test'], 'test_evidence': {'old.source.test': {}},
            'uncertainties': ['Save migration needs target verification']},
            'v29_assertion_observation': {'status': 'passed'}}
        requirements = requirements_from_legacy_contract(legacy)
        serialized = json.dumps(requirements)
        self.assertNotIn('old.source.test', serialized)
        self.assertNotIn('file_sha256', serialized)
        self.assertEqual(requirements['behaviors'][0]['assertions'][0]['assertion_id'], 'health.persist')
        result = BehaviorFreezeHandler()(self.command(payload={'behavior_requirements': requirements}))
        self.assertEqual(result.status, 'completed', result.detail)
        frozen = json.loads((self.root / result.outputs['artifact_refs']['behavior_requirements']['path']).read_text())
        self.assertEqual(frozen['source_commit'], 'host-source')
        self.assertEqual(frozen['source_assumption'], 'user_confirmed_functional')
        self.assertEqual(frozen['verification_basis'], 'source_reading')
        self.assertEqual(frozen['acceptance_status'], 'unverified')
        self.assertNotIn('source_verification', frozen)

    def test_freeze_keeps_diagnostic_review_optional_but_requires_behavior_input(self):
        result = BehaviorFreezeHandler()(self.command())
        self.assertEqual(result.status, 'failed')
        candidate = self.root / 'artifacts/carried-requirements.json'
        candidate.write_text(json.dumps(self.requirements()))
        result = BehaviorFreezeHandler()(self.command(artifact_refs={
            'behavior_requirements_candidate': {'path': 'artifacts/carried-requirements.json'}}))
        self.assertEqual(result.status, 'completed', result.detail)
        self.assertNotIn('process_executed', result.outputs)

    def test_conversion_retains_selected_contract_without_reenabling_excluded_source_assertions(self):
        retained = {'id': 'health', 'source_evidence': 'Health.save', 'assertion_contracts': [{
            'assertion_id': 'health.persist', 'text': 'Restored health equals saved health',
            'source_anchor': {'path': 'src/Health.java', 'start_line': 10, 'end_line': 14}}]}
        excluded = {'id': 'excluded', 'source_evidence': 'Historical source defect',
                    'assertion_contracts': [{'assertion_id': 'excluded.source.defect',
                        'text': 'Excluded historical assertion',
                        'source_anchor': {'path': 'src/Old.java', 'start_line': 1, 'end_line': 2}}]}
        converted = requirements_from_legacy_contract({
            'contract': {'behaviors': [retained]},
            'source_contract': {'behaviors': [retained, excluded]}})
        self.assertEqual([row['behavior_id'] for row in converted['behaviors']], ['health'])
        self.assertNotIn('excluded.source.defect', json.dumps(converted))

    def test_confirmed_carried_assertion_cannot_be_removed_by_a_new_extraction(self):
        original = self.requirements()
        candidate = self.root / 'artifacts/carried-requirements.json'
        candidate.write_text(json.dumps({'requirements': original}))
        changed = self.requirements()
        changed['behaviors'][0]['assertions'][0]['assertion_id'] = 'different'
        result = BehaviorFreezeHandler()(self.command(payload={'behavior_requirements': changed},
            artifact_refs={'behavior_requirements': {'path': 'artifacts/carried-requirements.json'}}))
        self.assertEqual(result.status, 'failed')
        self.assertIn('health.persist', result.detail)

    def test_policy_excludes_skill_generation_and_duplicate_assertions_are_invalid(self):
        self.assertTrue(source_reading_policy(self.command()))
        self.assertTrue(source_reading_policy({'definition': {'workflow_version': 34},
                                             'request': {'workflow_mode': 'artifact_verification'}}))
        self.assertFalse(source_reading_policy({'definition': {'workflow_version': 34},
                                              'request': {'workflow_mode': 'skill_generation'}}))
        document = self.requirements()
        document['behaviors'][0]['assertions'].append(document['behaviors'][0]['assertions'][0])
        with self.assertRaisesRegex(ValueError, 'duplicate assertion_id'):
            validate_requirements(document)

    def test_planning_obligations_cover_source_behavior_and_each_assertion_without_source_tests(self):
        self.assertEqual(_obligations(self.requirements(), {'rules': [{'id': 'persist'}]}),
                         {'health', 'assertion:health.persist', 'rubric:persist'})

    def test_actual_report_materialization_reaches_freeze_without_approval_or_source_results(self):
        baseline = self.root / 'baseline'
        baseline.mkdir()
        extract = self.command('behavior_extract')
        dialogue = prepare_dialogue(extract, self.root, STAGE_PROMPTS['behavior_extract'])
        self.assertEqual(dialogue['contract']['output_path'], '.modport/behavior-requirements.json')
        self.assertNotIn('test_evidence', json.dumps(dialogue['contract']['schema']))
        materialize_report(dialogue, baseline, json.dumps(self.requirements()))
        review = self.command('behavior_review')
        review_dialogue = prepare_dialogue(review, self.root, STAGE_PROMPTS['behavior_review'])
        self.assertIsNone(review_dialogue['schema_path'])
        materialize_report(review_dialogue, baseline, 'Diagnostic: target save migration remains unverified.')
        result = BehaviorFreezeHandler()(self.command())
        self.assertEqual(result.status, 'completed', result.detail)
        frozen = json.loads((self.root / result.outputs['artifact_refs']['behavior_requirements']['path']).read_text())
        self.assertEqual(frozen['review_ref']['path'], 'baseline/.modport/behavior-review.md')
        self.assertFalse((self.root / 'artifacts/baseline-contract-tests.json').exists())

    def test_current_prompts_use_requirements_and_do_not_request_baseline_suite(self):
        for stage in ('behavior_extract', 'behavior_review', 'migration_plan', 'implementation'):
            command = self.command(stage)
            build_prompt(STAGE_PROMPTS[stage], command, self.root, {}, {})
            packet = json.loads((self.root / 'artifacts/executions' / command.command_id /
                                 'task-instructions.json').read_text())
            combined = packet['task'] + packet['protected_context']
            self.assertNotIn('Workflow v31 shared test-matrix protocol', combined)
            self.assertNotIn('original baseline suite must execute', combined)
            self.assertNotIn('fresh original-source execution', combined)
            if stage == 'implementation':
                self.assertIn('target-only harness sources', combined)
                self.assertIn('shared runtime sessions', combined)
                self.assertNotIn('Port the frozen characterization harness', combined)
            if stage == 'behavior_extract':
                self.assertIn('source-reading protocol', combined)
                self.assertNotIn('characterization-author-contract.json', combined)


if __name__ == '__main__':
    unittest.main()
