"""Current host observation records reach target verification unchanged."""
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from modport.handlers import _verify_locked_artifacts
from modport.models import LockedManifest, MigrationRequest
from modport.artifact_verification import ArtifactTestReportHandler
from modport.contracts import OperationInput
from modport.workflow import WORKFLOW_VERSION


class ObservationTransportTests(unittest.TestCase):
    def test_current_observation_does_not_require_retired_approval_fields(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            artifacts = root / 'artifacts'
            artifacts.mkdir()
            java = root / 'toolchains/gradle-cache/jdks/current/bin/java'
            java.parent.mkdir(parents=True)
            java.touch()
            request = MigrationRequest('example', 'https://example.invalid/repo.git',
                                       '1.20.1', '26.1.2')
            manifest = LockedManifest(request, '26.1.2.106', source_commit='original-source',
                                      java_toolchain={'executable': 'jdks/current/bin/java'})
            (artifacts / 'locked-manifest.json').write_text(json.dumps(manifest.to_dict()))
            (artifacts / 'source.json').write_text(json.dumps({'source_commit': 'original-source'}))
            observation = artifacts / 'functional-contract-observation.json'
            observation.write_text(json.dumps({
                'acceptance_status': 'unverified',
                'review': {'verdict': 'rejected', 'findings': ['missing behavior coverage']},
                'baseline_verification': {'status': 'failed', 'outputs': {'case_results': {}}},
                'contract': {'behaviors': [{
                    'id': 'world.entry', 'source_evidence': 'src/World.java:1',
                    'preconditions': ['client started'], 'action': ['enter a world'],
                    'assertions': ['player exists'], 'side': 'client',
                    'test_mapping': ['world.entry'],
                }]},
            }))
            ref = {'path': observation.relative_to(root).as_posix(),
                   'sha256': 'host-supplied-reference'}
            # Reference resolution belongs to the existing host artifact store.
            with patch('modport.handlers._resolve_artifact_ref',
                       return_value=(observation, ref)), \
                    patch('modport.handlers.ReviewRecord', side_effect=AssertionError(
                        'observation was fed into the retired approved-review consumer')):
                result = _verify_locked_artifacts(root, rubric={}, contract_ref=ref)
            self.assertEqual('host-supplied-reference', result['contract_lock_sha256'])


class ReportOutcomeTests(unittest.TestCase):
    def test_failed_skipped_or_missing_witness_cannot_be_reported_completed(self):
        for problem in ('skipped', 'failed', 'missing_witness', None):
            with self.subTest(problem=problem), TemporaryDirectory() as directory:
                root = Path(directory)
                contract = {'test_evidence': {'world.entry': {'evidence_kind': 'runtime'}},
                    'behaviors': [{'assertion_contracts': [{'assertion_id': 'player.exists',
                        'test_ids': ['world.entry']}]}]}
                source = {'status': 'completed', 'outputs': {
                    'process_executed': True,
                    'case_results': {'world.entry': {'status': 'passed', 'test_outcome': 'passed'}},
                    'assertion_results': {'player.exists': {'status': 'passed'}},
                    'evidence_records': {'world.entry': {
                        'evidence_kind': 'runtime', 'path': '.modport/evidence/world.entry.json'}}}}
                target = json.loads(json.dumps(source))
                if problem in ('skipped', 'failed'):
                    target['outputs']['case_results']['world.entry'].update(
                        status=problem, test_outcome=problem)
                    target['outputs']['assertion_results']['player.exists']['status'] = 'unverified'
                if problem == 'missing_witness':
                    target['outputs']['evidence_records'] = {}
                (root / 'selection.json').write_text(json.dumps({'selection': {
                    'migration_contract': contract, 'selected_test_ids': ['world.entry'],
                    'uncovered_assertion_ids': []}}))
                (root / 'lock.json').write_text(json.dumps({'contract': contract,
                                                           'uncovered_assertion_ids': []}))
                command = OperationInput('run', 'report', 'artifact_test_report', 'report',
                    str(root), options={'workflow_version': WORKFLOW_VERSION,
                        'validation_policy': {'scope': 'artifact_verification',
                                              'required_behavior_completion': True}},
                    upstream_results={
                        'contract_verify': source, 'artifact_test_execute': target,
                        'contract_review': {'outputs': {'artifact_refs': {
                            'baseline_test_selection': {'path': 'selection.json'}}}},
                        'contract_freeze': {'outputs': {'artifact_refs': {
                            'functional_contract_lock': {'path': 'lock.json'}}}},
                    })
                with patch('modport.artifact_verification.artifact_input', return_value=({}, None)), \
                        patch('modport.artifact_verification_policy.verified_path',
                              side_effect=lambda root, ref: Path(root) / ref['path']):
                    result = ArtifactTestReportHandler()(command)
                report = json.loads((root / 'artifacts/artifact-verification-report.json').read_text())
                expected = 'passed' if problem is None else 'failed'
                self.assertEqual(expected, report['required_behavior_status'])
                self.assertEqual('completed' if problem is None else 'failed', result.status)
                self.assertEqual('unverified', report['acceptance_status'])


if __name__ == '__main__':
    unittest.main()
