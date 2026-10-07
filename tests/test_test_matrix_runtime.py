"""Current workflow selection handoff using real files, Git IDs and sealed receipts."""
from dataclasses import replace
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from modport.contracts import OperationInput, OperationResult
from modport.evidence import atomic_json, file_digest
from modport.handlers import FreezeContractHandler, ReviewHandler, _snapshot_stage_output
from modport.opencode_shell_mcp import _workspace_candidate_identity
from modport.test_matrix_runtime import baseline_selection_summary, frozen_selection, seal_assessment


class MatrixRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.workspace = self.root / 'baseline'
        self.workspace.mkdir()
        def git(*args):
            return subprocess.check_output(['git', '-C', str(self.workspace), *args],
                stderr=subprocess.DEVNULL, text=True).strip()
        git('init', '-q')
        (self.workspace / 'Source.java').write_text('class Source { int value = 1; }\n')
        git('add', 'Source.java')
        git('-c', 'user.name=Test', '-c', 'user.email=test@example.invalid',
            'commit', '-qm', 'original source')
        self.source_commit = git('rev-parse', 'HEAD')
        self.contract = {'contract_id': 'source-contract', 'source_fingerprint': self.source_commit,
            'behaviors': [], 'test_evidence': {}, 'baseline_evidence_files': [],
            'baseline_gradle_tasks': ['test']}
        self.matrix = {'schema_version': 1, 'cases': []}
        for name in ('healthy', 'defect'):
            assertion = {'assertion_id': name + '.assert', 'text': name,
                'test_ids': [name], 'source_anchor': {'path': 'Source.java', 'start_line': 1, 'end_line': 1}}
            self.contract['behaviors'].append({'id': name, 'test_mapping': [name],
                'source_evidence': 'Source.java:1', 'preconditions': ['Source initialized'],
                'action': ['Read Source.value'], 'side': 'both',
                'assertions': [name], 'assertion_contracts': [assertion]})
            self.contract['test_evidence'][name] = {'path': '.modport/evidence/' + name + '.json',
                'evidence_kind': 'runtime', 'executor': 'junit',
                'result_identity': {'kind': 'junit_xml', 'gradle_task': 'test',
                    'classname': 'Case', 'name': name}}
            self.contract['baseline_evidence_files'].append('.modport/evidence/' + name + '.json')
            self.matrix['cases'].append({'test_id': name, 'behavior_id': name,
                'entry_point': 'Source.value', 'action': 'read', 'conditions': [],
                'assertion_ids': [name + '.assert']})
        atomic_json(self.workspace / '.modport/functional-contract.json', self.contract)
        atomic_json(self.workspace / '.modport/test-matrix.json', self.matrix)
        atomic_json(self.root / 'artifacts/source.json', {'source_commit': self.source_commit})
        candidate = _workspace_candidate_identity(self.workspace)
        xml = self.root / 'artifacts/executions/verify/baseline-results.xml'
        xml.parent.mkdir(parents=True)
        xml.write_text('<testsuite><testcase classname="Case" name="healthy"/>'
                       '<testcase classname="Case" name="defect"><failure>source defect</failure>'
                       '</testcase></testsuite>')
        self.report = {'source_commit': self.source_commit, 'execution_nonce': 'fresh',
            'candidate_before': candidate, 'candidate_after': candidate,
            'candidate_unchanged': True, 'exit_code': 1, 'case_results': {}}
        for name, outcome in [('healthy', 'passed'), ('defect', 'failed')]:
            self.report['case_results'][name] = {
                'test_id': name, 'status': outcome, 'test_outcome': outcome, 'category': 'unknown',
                'source_commit': self.source_commit, 'candidate_id': candidate['candidate_id'],
                'candidate_unchanged': True, 'execution_nonce': 'fresh',
                'result_identity': self.contract['test_evidence'][name]['result_identity'],
                'xml_artifact_ref': {'path': xml.relative_to(self.root).as_posix(), 'sha256': file_digest(xml)}}
        report_path = self.root / 'artifacts/executions/verify/baseline-contract-tests.json'
        atomic_json(report_path, self.report)
        self.verifier = OperationResult('failed', 'run', 'contract_verify', 'contract_verify', 'verify',
            outputs={'artifact_refs': {'baseline_contract_tests_candidate': {
                'path': report_path.relative_to(self.root).as_posix(), 'sha256': file_digest(report_path)}}})
        self.command = OperationInput('run', 'contract_review', 'contract_review', 'review', str(self.root),
            options={'workflow_version': 31}, upstream_results={'contract_verify': self.verifier.to_dict()})
        self.assessment = {'schema_version': 1, 'decisions': [
            {'test_id': 'healthy', 'decision': 'keep', 'reason': 'observable normal behavior'},
            {'test_id': 'defect', 'decision': 'source_defect', 'reason': 'original implementation defect',
             'evidence': ['baseline-results.xml:Case.defect']} ]}
        self.assessment_path = self.workspace / '.modport/test-assessment.json'
        atomic_json(self.assessment_path, self.assessment)
        alias, ref = _snapshot_stage_output(self.root, self.workspace, self.command,
                                            '.modport/test-assessment.json')
        review = {'review_id': 'review', 'verdict': 'rejected', 'assertion_reviews': [
            {'assertion_id': name + '.assert',
             'source_anchor': {'path': 'Source.java', 'start_line': 1, 'end_line': 1},
             'status': 'supported' if name == 'healthy' else 'unsupported',
             'reasoning': 'exact original source comparison'} for name in ('healthy', 'defect')]}
        review_path = self.root / 'artifacts/executions/review/contract_review-decision.json'
        atomic_json(review_path, review)
        atomic_json(self.workspace / '.modport/contract-review.json', review)
        self.review_result = OperationResult('completed', 'run', 'contract_review', 'contract_review', 'review',
            outputs={'artifact_refs': {alias: ref, 'contract_review': {
                'path': review_path.relative_to(self.root).as_posix(), 'sha256': file_digest(review_path)}}})

    def freeze_command(self):
        ref = seal_assessment(self.command, self.review_result)
        review = replace(self.review_result, outputs={'artifact_refs': {
            **self.review_result.outputs['artifact_refs'], 'baseline_test_selection': ref}})
        return replace(self.command, task_id='contract_freeze', stage_id='contract_freeze', command_id='freeze',
            upstream_results={**self.command.upstream_results, 'contract_review': review.to_dict()})

    def test_failed_original_observation_is_preserved_but_not_migrated(self):
        command = self.freeze_command()
        selected = frozen_selection(command)
        self.assertEqual(selected['selected_test_ids'], ['healthy'])
        self.assertEqual(selected['source_defects'][0]['case_evidence']['test_outcome'], 'failed')
        result = FreezeContractHandler()(command)
        ref = result.outputs['artifact_refs']['functional_contract_lock']
        lock = json.loads((self.root / ref['path']).read_text())
        self.assertEqual(lock['source_contract'], self.contract)
        self.assertEqual(len(lock['source_review']['assertion_reviews']), 2)
        self.assertEqual([row['assertion_id'] for row in lock['review']['assertion_reviews']], ['healthy.assert'])
        self.assertEqual(lock['review']['verdict'], 'rejected')
        self.assertEqual(set(lock['contract']['test_evidence']), {'healthy'})
        self.assertEqual(json.loads((self.workspace / '.modport/functional-contract.json').read_text()), self.contract)
        self.assertEqual(lock['acceptance_status'], 'unverified')
        summary = baseline_selection_summary(lock)
        self.assertEqual(summary['status'], 'passed')
        self.assertEqual(summary['source_anchor_status'], 'resolved')
        self.assertEqual(summary['selected_test_ids'], ['healthy'])
        self.assertEqual(summary['original_case_results']['defect']['status'], 'failed')

    def test_invalid_or_missing_source_resolution_cannot_report_selected_baseline_passed(self):
        result = FreezeContractHandler()(self.freeze_command())
        ref = result.outputs['artifact_refs']['functional_contract_lock']
        lock = json.loads((self.root / ref['path']).read_text())
        for status in ('invalid_or_missing', None):
            with self.subTest(source_anchor_status=status):
                lock['v29_assertion_observation']['source_anchor_status'] = status
                summary = baseline_selection_summary(lock)
                self.assertEqual(summary['verified_behavior_tests'], 1)
                self.assertEqual(summary['status'], 'unverified')

    def test_planner_can_assess_failed_fresh_verifier_after_explicit_rework(self):
        review_path = self.workspace / '.modport/contract-review.json'
        report = json.loads(review_path.read_text())
        report['verdict'] = 'approved'
        for row in report['assertion_reviews']:
            row['status'] = 'supported'

        def model_report(command):
            atomic_json(review_path, report)
            alias, ref = _snapshot_stage_output(self.root, self.workspace, command,
                                                '.modport/contract-review.json')
            return replace(self.review_result, outputs={'artifact_refs': {
                **self.review_result.outputs['artifact_refs'], alias: ref}})

        revisions = [{'updates': [
            {'stage': 'contract_draft', 'result': {'status': 'completed', 'outputs': {}}},
            {'stage': 'contract_verify', 'result': self.verifier.to_dict()},
        ]}]
        with patch('modport.handlers.CodexStageHandler', return_value=model_report), \
                patch('modport.rework_tools.responses', return_value=revisions):
            result = ReviewHandler(baseline=True)(self.command)
        self.assertEqual(result.outputs['verdict'], 'approved')
        refs = result.outputs['artifact_refs']
        self.assertIn('contract_review', refs)
        selection = json.loads((self.root / refs['baseline_test_selection']['path']).read_text())
        self.assertEqual(selection['selection']['selected_test_ids'], ['healthy'])
        self.assertEqual(selection['selection']['source_defects'][0]['case_evidence']['test_outcome'], 'failed')

    def test_repair_after_assessment_cannot_reuse_old_selection(self):
        command = self.freeze_command()
        (self.workspace / 'Source.java').write_text('class Source { int value = 2; }\n')
        with self.assertRaisesRegex(ValueError, 'candidate changed'):
            frozen_selection(command)

    def test_another_verifier_cannot_supply_same_old_selection(self):
        command = self.freeze_command()
        other = replace(self.verifier, command_id='another')
        command = replace(command, upstream_results={**command.upstream_results, 'contract_verify': other.to_dict()})
        with self.assertRaisesRegex(ValueError, 'another verifier'):
            frozen_selection(command)

    def test_mutated_junit_prevents_defect_exclusion(self):
        command = self.freeze_command()
        (self.root / 'artifacts/executions/verify/baseline-results.xml').write_text('<testsuite/>')
        with self.assertRaisesRegex(ValueError, 'JUnit artifact digest'):
            frozen_selection(command)

    def test_planner_reports_do_not_change_candidate_but_matrix_does(self):
        before = _workspace_candidate_identity(self.workspace)
        atomic_json(self.workspace / '.modport/contract-review.json', {'verdict': 'rejected'})
        self.assertEqual(before, _workspace_candidate_identity(self.workspace))
        self.matrix['cases'][0]['conditions'] = ['new condition']
        atomic_json(self.workspace / '.modport/test-matrix.json', self.matrix)
        self.assertNotEqual(before, _workspace_candidate_identity(self.workspace))


if __name__ == '__main__':
    unittest.main()
