from copy import deepcopy
from dataclasses import replace
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from modport.contracts import OperationInput, OperationResult
from modport.evidence import atomic_json, candidate_fingerprint, file_digest, verified_path
from modport.gap_review import GapApprovedDeliveryHandler, GapReviewHandler, REPORT_PATH
from modport.rubric import acceptance_rubric


class GapReviewTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.worktree = self.root / 'worktree'
        (self.worktree / '.modport').mkdir(parents=True)
        (self.worktree / 'Mod.java').write_text('class Mod {}')
        self.rubric = acceptance_rubric()
        self.gap = {'skill': 'platform', 'index': 0, 'applicable': True,
                    'status': 'unresolved', 'kind': 'verification',
                    'resolution_stage': 'client_smoke',
                    'closure_criteria': ['Verify custom renderer appearance in the client.']}
        self.analysis = {'schema_version': 2, 'gap_assessments': [self.gap]}
        self.refs = {
            'acceptance_rubric': self.artifact('rubric.json', self.rubric),
            'mod_analysis': self.artifact('analysis.json', self.analysis),
            'client_observation': self.artifact('client.json', {'observation': 'Expected renderer appearance verified.'}),
        }
        self.refs['acceptance_rubric']['metadata'] = {'rubric_sha256': self.rubric['rubric_sha256']}
        self.command = OperationInput('run', 'gap_review', 'gap_review', 'run:gap:1', str(self.root),
            artifact_refs=self.refs, options={'acceptance_rubric_sha256': self.rubric['rubric_sha256']},
            upstream_results={'client_smoke': {'status': 'completed', 'outputs': {
                'artifact_refs': {'client_observation': self.refs['client_observation']}}}})
        self.report = {'schema_version': 1, 'reviewer_id': 'gap-review-agent', 'review_id': 'review-1',
            'candidate_sha256': candidate_fingerprint(self.worktree),
            'analysis_sha256': self.refs['mod_analysis']['sha256'],
            **{key: self.rubric[key] for key in ('rubric_id', 'rubric_version', 'rubric_sha256')},
            'verdict': 'approved', 'findings': [], 'gap_resolutions': [{
                'skill': 'platform', 'index': 0, 'status': 'resolved',
                'evidence': ['Inspected client observation; expected renderer appears.'],
                'artifact_ids': ['client_observation']}]}

    def artifact(self, name, value):
        path = self.root / 'artifacts' / name
        atomic_json(path, value)
        return {'path': path.relative_to(self.root).as_posix(), 'sha256': file_digest(path)}

    def run_review(self, report=None, command=None, tamper=None, snapshot=True):
        report = self.report if report is None else report
        def agent(_, operation):
            atomic_json(self.worktree / REPORT_PATH, report)
            ref = self.artifact('review.json', report)
            if tamper:
                tamper()
            return OperationResult('completed', outputs={'artifact_refs': {
                'stage_output:gap_review:' + REPORT_PATH: ref} if snapshot else {}})
        with patch('modport.handlers.CodexStageHandler.__call__', agent):
            return GapReviewHandler()(command or self.command)

    def test_approved_report_is_snapshotted_and_removed_without_candidate_changes(self):
        result = self.run_review()
        self.assertEqual(result.status, 'completed', result.detail)
        self.assertEqual(result.outputs['verdict'], 'approved')
        self.assertEqual(candidate_fingerprint(self.worktree), self.report['candidate_sha256'])
        self.assertFalse((self.worktree / REPORT_PATH).exists())
        refs = result.outputs['artifact_refs']
        raw = next(ref for key, ref in refs.items() if key.endswith(REPORT_PATH))
        self.assertEqual(json.loads(verified_path(self.root, raw).read_text()), self.report)
        decision = json.loads(verified_path(self.root, refs['gap_review']).read_text())
        self.assertEqual(decision['reviewer_id'], 'gap-review-agent')
        self.assertEqual(decision['review_id'], self.command.command_id)
        self.assertEqual(decision['rubric_id'], self.rubric['rubric_id'])
        self.assertEqual(decision['rubric_version'], self.rubric['rubric_version'])
        self.assertEqual(json.loads(decision['raw_report']), self.report)

    def test_rejected_unresolved_gap_is_retained_as_repair_finding(self):
        self.report['verdict'] = 'rejected'
        row = self.report['gap_resolutions'][0]
        row.update(status='unresolved', artifact_ids=[], evidence=['No visual observation establishes closure.'])
        result = self.run_review()
        self.assertEqual(result.status, 'completed', result.detail)
        self.assertEqual(result.outputs['verdict'], 'rejected')
        self.assertIn(row, result.outputs['prior_findings'])
        self.assertTrue(any('report' in finding for finding in result.outputs['prior_findings']))

    def test_approval_cannot_hide_unresolved_gap_and_freeform_findings_are_retained(self):
        unresolved = deepcopy(self.report)
        unresolved['gap_resolutions'][0]['status'] = 'unresolved'
        self.assertEqual(self.run_review(unresolved).error_code, 'gap_review_invalid')

        report = deepcopy(self.report)
        report['findings'] = [{'message': 'Renderer behavior is incorrect.'}]
        result = self.run_review(report)
        self.assertEqual(result.status, 'completed', result.detail)
        self.assertEqual(result.outputs['prior_findings'], report['findings'])
        decision = json.loads(verified_path(
            self.root, result.outputs['artifact_refs']['gap_review']).read_text())
        self.assertEqual(json.loads(decision['raw_report']), report)

    def test_missing_duplicate_unknown_and_invalid_identities_fail(self):
        row = self.report['gap_resolutions'][0]
        for rows in ([], [row, row], [dict(row, index=1)], [dict(row, index=True)], [None]):
            with self.subTest(rows=rows):
                self.assertEqual(self.run_review(dict(self.report, gap_resolutions=rows)).error_code, 'gap_review_invalid')

    def test_fabricated_empty_and_nonstring_evidence_fail(self):
        row = self.report['gap_resolutions'][0]
        for changes in ({'artifact_ids': ['invented']}, {'artifact_ids': []}, {'artifact_ids': [None]},
                        {'evidence': []}, {'evidence': [' ']}, {'status': []}):
            with self.subTest(changes=changes):
                report = dict(self.report, gap_resolutions=[dict(row, **changes)])
                self.assertEqual(self.run_review(report).error_code, 'gap_review_invalid')

    def test_verification_requires_exact_completed_declared_stage_producer(self):
        original = self.command.upstream_results['client_smoke']
        cases = [ {}, {'target_build': original}, {'client_smoke': dict(original, status='failed')},
                 {'client_smoke': {'status': 'completed', 'outputs': {'artifact_refs': {}}}},
                 {'client_smoke': {'status': 'completed', 'outputs': {'artifact_refs': {
                     'client_observation': dict(self.refs['client_observation'], path='other.json')}}}} ]
        for upstream in cases:
            with self.subTest(upstream=upstream):
                result = self.run_review(command=replace(self.command, upstream_results=upstream))
                self.assertEqual(result.error_code, 'gap_review_invalid')

    def test_stage_preserving_alias_survives_later_artifact_key_overwrite(self):
        original = self.refs['client_observation']
        later = self.artifact('later.json', {'result': 'Different later execution'})
        alias = 'gap_evidence:client_smoke:client_observation'
        command = replace(self.command, artifact_refs={
            **self.refs, 'client_observation': later, alias: original})
        # Ordinary key now points at another producer's artifact and cannot close this gap.
        self.assertEqual(self.run_review(command=command).error_code, 'gap_review_invalid')
        report = deepcopy(self.report)
        report['gap_resolutions'][0]['artifact_ids'] = [alias]
        result = self.run_review(report, command)
        self.assertEqual(result.status, 'completed', result.detail)
        # An alias must still identify the producing stage's artifact path.
        command = replace(command, artifact_refs={**command.artifact_refs, alias: later})
        self.assertEqual(self.run_review(report, command).error_code, 'gap_review_invalid')

    def test_report_identity_fields_are_host_owned_and_snapshot_is_required(self):
        for key in ('schema_version', 'reviewer_id', 'review_id', 'rubric_id', 'rubric_version'):
            with self.subTest(key=key):
                result = self.run_review(dict(self.report, **{key: None}))
                self.assertEqual(result.status, 'completed', result.detail)
                decision = json.loads(verified_path(
                    self.root, result.outputs['artifact_refs']['gap_review']).read_text())
                self.assertEqual(decision['schema_version'], 1)
                self.assertEqual(decision['reviewer_id'], 'gap-review-agent')
                self.assertEqual(decision['review_id'], self.command.command_id)
                self.assertEqual(decision['rubric_id'], self.rubric['rubric_id'])
                self.assertEqual(decision['rubric_version'], self.rubric['rubric_version'])
        self.assertEqual(self.run_review(snapshot=False).error_code, 'gap_review_invalid')

    def test_report_hash_fields_are_optional_metadata(self):
        report = {key: value for key, value in self.report.items() if not key.endswith('_sha256')}
        self.assertEqual(self.run_review(report).status, 'completed')
        upstream = deepcopy(self.command.upstream_results)
        upstream['client_smoke']['outputs']['artifact_refs']['client_observation']['sha256'] = 'old'
        self.assertEqual(self.run_review(report, replace(self.command, upstream_results=upstream)).status, 'completed')

    def test_candidate_and_output_updates_allow_review(self):
        for name in ('Mod.java', 'build/output.log', '.modport/code-review.json'):
            def tamper():
                path = self.worktree / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text('reviewer edit')
            with self.subTest(name=name):
                result = self.run_review(tamper=tamper)
                self.assertEqual(result.status, 'completed', result.detail)

    def test_input_evidence_updates_do_not_require_matching_hash(self):
        result = self.run_review(tamper=lambda: (self.root / self.refs['client_observation']['path']).write_text('{"observation":"updated evidence"}'))
        self.assertEqual(result.status, 'completed', result.detail)

    def test_not_applicable_reclassification_requires_evidence_artifacts(self):
        row = self.report['gap_resolutions'][0]
        row.update(status='not_applicable', evidence=['Independent source inspection confirms renderer is absent.'])
        self.assertEqual(self.run_review().status, 'completed')
        row['artifact_ids'] = []
        self.assertEqual(self.run_review().error_code, 'gap_review_invalid')

    def test_prior_resolved_and_not_applicable_analysis_rows_are_not_omitted(self):
        for status, applicable in (('resolved', True), ('not_applicable', False)):
            self.gap.update(status=status, applicable=applicable)
            ref = self.artifact('analysis.json', self.analysis)
            command = replace(self.command, artifact_refs={**self.refs, 'mod_analysis': ref}, upstream_results={})
            report = dict(self.report, analysis_sha256=ref['sha256'])
            with self.subTest(status=status):
                self.assertEqual(self.run_review(report, command).status, 'completed')
                self.assertEqual(self.run_review(dict(report, gap_resolutions=[]), command).error_code, 'gap_review_invalid')

    def test_analysis_revision_cannot_omit_durable_project_gap(self):
        host_gap = {'gap_id': 'knowledge:platform:removed', 'entry_id': 'removed',
                    'skill': 'platform', 'index': 7, 'applicable': True,
                    'status': 'unresolved', 'project_status': 'unresolved',
                    'kind': 'knowledge', 'resolution_stage': 'mod_analysis',
                    'closure_criteria': ['Determine whether the removed API is required.']}
        analysis_ref = self.artifact('analysis.json', {'schema_version': 2, 'gap_assessments': []})
        command = replace(self.command,
            artifact_refs={**self.refs, 'mod_analysis': analysis_ref},
            payload={'project_research_gaps': [host_gap]}, upstream_results={})
        omitted = dict(self.report, gap_resolutions=[])
        self.assertEqual(self.run_review(omitted, command).error_code, 'gap_review_invalid')

        covered = dict(self.report, gap_resolutions=[{
            'gap_id': host_gap['gap_id'], 'status': 'resolved',
            'evidence': ['Inspected authenticated project evidence for the retained gap.'],
            'artifact_ids': ['client_observation']}])
        result = self.run_review(covered, command)
        self.assertEqual(result.status, 'completed', result.detail)

    def test_irrelevant_mod_gap_requires_no_research_or_execution_producer(self):
        self.gap.update(applicable=False, status='not_applicable', closure_criteria=[])
        ref = self.artifact('analysis.json', self.analysis)
        inventory = self.artifact('source-inventory.json', {'features': [], 'source': 'class Mod {}'})
        command = replace(self.command, artifact_refs={**self.refs, 'mod_analysis': ref, 'source_inventory': inventory}, upstream_results={})
        report = deepcopy(self.report)
        report['analysis_sha256'] = ref['sha256']
        report['gap_resolutions'][0].update(status='not_applicable',
            evidence=['Independent source inspection confirms the mod has no custom renderer.'],
            artifact_ids=['source_inventory'])
        result = self.run_review(report, command)
        self.assertEqual(result.status, 'completed', result.detail)
        self.assertEqual(result.outputs['verdict'], 'approved')

    def test_invalid_or_unauthenticated_analysis_never_launches_reviewer(self):
        for changes in ({'schema_version': 1}, {'gap_assessments': [self.gap, self.gap]},
                        {'gap_assessments': [dict(self.gap, closure_criteria=[])]},
                        {'gap_assessments': [dict(self.gap, resolution_stage='acceptance_build')]}):
            ref = self.artifact('analysis.json', dict(self.analysis, **changes))
            command = replace(self.command, artifact_refs={**self.refs, 'mod_analysis': ref})
            with self.subTest(changes=changes), patch('modport.handlers.CodexStageHandler') as agent:
                self.assertEqual(GapReviewHandler()(command).error_code, 'gap_review_invalid')
                agent.assert_not_called()
        with patch('modport.handlers.CodexStageHandler') as agent:
            self.assertEqual(GapReviewHandler()(self.command).error_code, 'gap_review_invalid')
            agent.assert_not_called()

    def test_delivery_requires_current_approved_gap_evidence(self):
        with patch('modport.gap_review.handlers.DeliveryHandler') as delivery:
            delivery.return_value.return_value = OperationResult('completed')
            handler = GapApprovedDeliveryHandler(delivery.return_value)
            self.assertEqual(handler(self.command).error_code, 'gap_review_stale')
            delivery.return_value.assert_not_called()
            ref = self.artifact('approved-gap-review.json', self.report)
            command = replace(self.command, artifact_refs={**self.refs, 'gap_review': ref})
            self.assertEqual(handler(command).status, 'completed')
            delivery.return_value.assert_called_once()
            (self.worktree / 'Mod.java').write_text('changed after review')
            self.assertEqual(handler(command).status, 'completed')
            self.assertEqual(delivery.return_value.call_count, 2)

    def test_v17_delivery_does_not_require_gap_approval(self):
        delivery = unittest.mock.Mock(return_value=OperationResult('completed'))
        command = replace(self.command, options={**self.command.options, 'workflow_version': 17})
        result = GapApprovedDeliveryHandler(delivery)(command)
        self.assertEqual(result.status, 'completed')
        delivery.assert_called_once_with(command)

    def test_v17_invalid_analysis_still_runs_reviewer_and_retains_raw_report(self):
        invalid = self.artifact('invalid-analysis.json', {'schema_version': 1})
        command = replace(self.command,
            artifact_refs={**self.refs, 'mod_analysis': invalid},
            options={**self.command.options, 'workflow_version': 17})

        def agent(_command):
            (self.worktree / REPORT_PATH).write_text('Evidence is incomplete.', encoding='utf-8')
            return OperationResult('completed', outputs={})

        with patch('modport.handlers.CodexStageHandler') as stage:
            stage.return_value.side_effect = agent
            result = GapReviewHandler()(command)
        self.assertEqual(result.status, 'completed', result.detail)
        self.assertEqual(result.outputs['acceptance_status'], 'unverified')
        self.assertEqual(result.outputs['raw_report'], 'Evidence is incomplete.')
        stage.assert_called_once()


if __name__ == '__main__':
    unittest.main()


class ProjectBypassReviewTests(unittest.TestCase):
    setUp = GapReviewTests.setUp
    artifact = GapReviewTests.artifact
    run_review = GapReviewTests.run_review

    def prepare_bypass(self):
        self.gap.update(kind='knowledge', resolution_stage='mod_analysis', entry_id='gap.api', gap_id='knowledge:gap.api')
        self.refs['mod_analysis'] = self.artifact('analysis.json', self.analysis)
        self.obligation = {'gap_id': 'verify:knowledge:gap.api:behavior', 'research_gap_id': 'knowledge:gap.api',
                           'due_stage': 'client_smoke', 'verification_status': 'pending'}
        self.command = replace(self.command, artifact_refs=self.refs,
            payload={'project_research_gaps': [{**self.gap, 'project_status': 'bypassed'}],
                     'gap_obligations': [self.obligation]})
        self.report['gap_resolutions'] = [
            {'gap_id': 'knowledge:gap.api', 'status': 'bypassed', 'evidence': ['Alternate implementation independently reviewed'],
             'artifact_ids': ['client_observation']},
            {'gap_id': self.obligation['gap_id'], 'status': 'resolved', 'evidence': ['Actual client observation proves behavior'],
             'artifact_ids': ['client_observation']}]

    def test_universal_unknown_can_pass_project_bypass_with_actual_evidence(self):
        self.prepare_bypass()
        result = self.run_review()
        self.assertEqual(result.status, 'completed', result.detail)
        self.assertEqual(result.outputs['verified_gap_obligations'], [self.obligation['gap_id']])
        self.assertEqual(self.gap['status'], 'unresolved')

    def test_bypass_without_completed_due_stage_or_reviewed_obligation_fails(self):
        self.prepare_bypass()
        for changes in ({'upstream_results': {}}, {'payload': {'gap_obligations': [self.obligation]}},
                        {'payload': {'project_research_gaps': [{**self.gap, 'project_status': 'bypassed'}]}}):
            with self.subTest(changes=changes):
                self.assertEqual(self.run_review(command=replace(self.command, **changes)).error_code, 'gap_review_invalid')
        self.report['gap_resolutions'][1]['status'] = 'unresolved'
        self.assertEqual(self.run_review().error_code, 'gap_review_invalid')
