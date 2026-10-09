"""Current gap-review producer inputs and post-author consumption, without runtime."""
from dataclasses import replace
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from modport.contracts import OperationInput, OperationResult
from modport.evidence import atomic_json
from modport.gap_review import GapReviewHandler, REPORT_PATH, _review_inputs
from modport.workflow import WORKFLOW_VERSION


class SourceReadingGapReviewTests(unittest.TestCase):
    def setUp(self):
        folder = TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        (self.root / 'worktree/.modport').mkdir(parents=True)
        self.rubric = {'rubric_id': 'target-behavior', 'rubric_version': 1}
        self.preparation = {'schema_version': 1, 'source_loader_version': '21.0.113-beta',
            'source_java': '21', 'version_evidence': [{
                'field': 'source_java', 'path': 'build.gradle', 'line': 15, 'value': '21'}],
            'unresolved_dependencies': ['Infiniverse uses an unresolved dependency version range.']}
        self.refs = {
            'source_preparation': self.artifact('preparation.json', self.preparation),
            'migration_inventory': self.artifact('inventory.json', {
                'schema_version': 1, 'source_scan_complete': True,
                'diagnostics': ['version-specific scan incomplete'], 'issues': []}),
            'migration_plan': self.artifact('plan.json', {
                'schema_version': 1, 'deferred_obligations': []}),
            'mod_scan_report': self.artifact('scan.json', {
                'schema_version': 1, 'scan_complete': False, 'classification': 'candidates-only',
                'compatibility_verified': False, 'skills': {}}),
        }
        self.command = OperationInput('run', 'gap_review', 'gap_review', 'gap-current', str(self.root),
            options={'workflow_version': WORKFLOW_VERSION, 'business_gates_disabled': True},
            payload={'project_research_gaps': [], 'gap_obligations': []}, artifact_refs=self.refs)
        self.report = {'verdict': 'rejected', 'gap_resolutions': [],
            'findings': ['Preparation dependency uncertainty remains unestablished.',
                         'The version-specific scan is incomplete.',
                         'Required target runtime cases have no passing evidence.'],
            'report': 'No runtime acceptance established; registered gap lists are empty.'}

    def artifact(self, name, value):
        relative = 'artifacts/' + name
        atomic_json(self.root / relative, value)
        return {'path': relative}

    def review(self, command=None, refreshed=None):
        dispatched = []

        def author(handler, operation):
            dispatched.append(operation)
            self.assertNotIn('Read the authenticated mod_analysis artifact', handler.prompt)
            self.assertIn('Infiniverse uses an unresolved dependency version range.', handler.prompt)
            self.assertIn('"scan_complete": false', handler.prompt)
            self.assertEqual([], operation.payload['project_research_gaps'])
            atomic_json(self.root / 'worktree' / REPORT_PATH, self.report)
            snapshot = self.artifact('review.json', self.report)
            return OperationResult('completed', operation.run_id, operation.task_id,
                operation.stage_id, operation.command_id, outputs={'artifact_refs': {
                    'stage_output:gap_review:' + REPORT_PATH: snapshot}})

        command = command or self.command
        with patch('modport.handlers._acceptance_rubric_for', return_value=self.rubric), \
                patch('modport.handlers.CodexStageHandler.__call__', author), \
                patch('modport.rework_tools.refresh_review_command', return_value=refreshed or command), \
                patch('modport.gap_review._review_inputs', wraps=_review_inputs) as consumed:
            result = GapReviewHandler()(command)
        self.assertEqual([command], dispatched)
        self.assertEqual(2, consumed.call_count)
        return result

    def test_empty_registered_gaps_reach_dispatch_and_preserve_rejected_runtime_findings(self):
        result = self.review()
        self.assertEqual('failed', result.status, result.detail)
        self.assertEqual('gap_review_rejected', result.error_code)
        self.assertEqual('rejected', result.outputs['verdict'])
        self.assertEqual('unverified', result.outputs['acceptance_status'])
        self.assertEqual([], result.outputs['verified_gap_obligations'])
        observations = result.outputs['review_input_observations']
        self.assertEqual(self.preparation['unresolved_dependencies'],
                         observations['source_preparation']['unresolved_dependencies'])
        self.assertFalse(observations['mod_scan_report']['scan_complete'])
        decision = json.loads((self.root / result.outputs['artifact_refs']['gap_review']['path']).read_text())
        self.assertEqual([], decision['gap_resolutions'])
        self.assertEqual(self.report['findings'], decision['findings'])
        self.assertNotIn('authenticated mod_analysis artifact is missing',
                         result.outputs['business_diagnostics'])

    def test_post_report_reads_refreshed_current_preparation(self):
        updated = {**self.preparation, 'unresolved_dependencies': ['Updated dependency observation.']}
        refreshed = replace(self.command, artifact_refs={**self.refs,
            'source_preparation': self.artifact('updated-preparation.json', updated)})
        result = self.review(refreshed=refreshed)
        self.assertEqual(['Updated dependency observation.'],
            result.outputs['review_input_observations']['source_preparation']['unresolved_dependencies'])

    def test_empty_analysis_interface_does_not_waive_due_stage_evidence(self):
        obligation = {'gap_id': 'verify:client', 'resolution_stage': 'client_smoke',
                      'closure_criteria': ['Inspect actual client presentation.']}
        command = replace(self.command, payload={**self.command.payload, 'gap_obligations': [obligation]})
        self.report['gap_resolutions'] = [{'gap_id': 'verify:client', 'status': 'resolved',
            'artifact_ids': ['migration_plan'], 'evidence': ['The migration plan discusses presentation.']}]
        result = self.review(command)
        self.assertIn("gap 'verify:client' lacks completed client_smoke evidence",
                      result.outputs['business_diagnostics'])
        self.assertEqual('unverified', result.outputs['acceptance_status'])
        self.assertNotIn('verified_gap_obligations', result.outputs)


if __name__ == '__main__':
    unittest.main()
