"""Current target execution accepts actual runtime results without candidate/source identity gates."""
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from modport.workflow import WORKFLOW_VERSION
from modport.author_contracts import characterization_evidence_schema
from modport.contracts import OperationInput
from modport.handlers import (_collect_v29_case_results,
    _test_evidence_declarations, _validate_evidence_record)


def declaration(test_id='live'):
    return {'path': '.modport/evidence/' + test_id + '.json', 'evidence_kind': 'runtime',
        'executor': 'junit', 'runtime_operations': ['invoke damage'],
        'test_source_files': ['.modport/harness/HealthTest.java'],
        'result_identity': {'kind': 'junit_xml', 'gradle_task': 'test',
                            'classname': 'example.HealthTest', 'name': test_id}}


def runtime_record():
    return {'test_id': 'live', 'evidence_kind': 'runtime', 'executor': 'junit',
        'execution_nonce': 'current-execution', 'execution_inputs': {'damage': 2},
        'runtime_operations': ['invoke damage'], 'observations': {'health': 8}, 'status': 'passed',
        'runtime_witnesses': [{'operation': 'invoke damage', 'event_index': 0,
            'invocation': 'Health.damage(2)', 'observations': {'health': 8},
            'execution_nonce': 'current-execution'}]}


class TargetRuntimeEvidenceTests(unittest.TestCase):
    def validate(self, record):
        return _validate_evidence_record(test_id='live', declaration=declaration(), record=record,
            source_commit='host-source-provenance', execution_nonce='current-execution',
            executor_fingerprint=None, workflow_version=WORKFLOW_VERSION)

    def test_runtime_record_accepts_absent_or_unmatched_source_fingerprint(self):
        record = runtime_record()
        self.assertEqual('MODPORT_RUNTIME_WITNESS current-execution live', self.validate(record))
        record['source_fingerprint'] = 'historical-source-field'
        self.assertEqual('MODPORT_RUNTIME_WITNESS current-execution live', self.validate(record))

    def test_stale_execution_missing_witness_and_wrong_test_still_fail(self):
        for change, expected in (
                ({'execution_nonce': 'stale'}, 'stale'),
                ({'runtime_witnesses': []}, 'operation-level witnesses'),
                ({'test_id': 'another-test'}, 'passing record')):
            record = runtime_record()
            record.update(change)
            with self.subTest(change=change), self.assertRaisesRegex(ValueError, expected):
                self.validate(record)
        record = runtime_record()
        record['runtime_witnesses'][0]['execution_nonce'] = 'stale'
        with self.assertRaisesRegex(ValueError, 'incomplete witness'):
            self.validate(record)

    def test_host_bound_declarations_do_not_require_author_rubric_identity(self):
        contract = {'behaviors': [{'side': 'server', 'test_mapping': ['live']}],
            'baseline_evidence_files': ['.modport/evidence/live.json'],
            'test_evidence': {'live': declaration()},
            'rubric_id': 'old-author-field', 'rubric_version': -1}
        rubric = {'rubric_id': 'host-rubric', 'rubric_version': 7,
                  'test_evidence_schema': characterization_evidence_schema(workflow_version=WORKFLOW_VERSION)}
        actual = _test_evidence_declarations(contract, rubric,
                                            workflow_version=WORKFLOW_VERSION, gradle_tasks=['test'])
        self.assertEqual({'live'}, set(actual))

    def test_actual_xml_collection_requires_results_without_candidate_probes_or_case_hashes(self):
        with TemporaryDirectory() as folder:
            root = Path(folder)
            workspace = root / 'worktree'
            xml = workspace / 'build/test-results/test/TEST-Health.xml'
            xml.parent.mkdir(parents=True)
            xml.write_text('<testsuite>'
                '<testcase classname="example.HealthTest" name="live" />'
                '<testcase classname="example.HealthTest" name="skipped"><skipped /></testcase>'
                '<testcase classname="example.HealthTest" name="failed"><failure message="health mismatch" /></testcase>'
                '</testsuite>')
            declarations = {test_id: declaration(test_id)
                            for test_id in ('live', 'skipped', 'failed', 'missing')}
            contract = {'behaviors': [{'assertion_contracts': [
                {'assertion_id': test_id + '.health', 'text': 'Health reflects damage',
                 'test_ids': [test_id], 'source_anchors': [{'path': 'Health.java', 'symbol': 'damage'}]}
                for test_id in declarations]}]}
            command = OperationInput('run', 'verify', 'artifact_test_execute', 'verify', str(root),
                                     options={'workflow_version': WORKFLOW_VERSION})
            def snapshot(run_root, source_root, operation, relative):
                self.assertTrue((source_root / relative).is_file())
                return source_root / relative, {'path': 'archived/' + relative}
            with patch('modport.opencode_shell_mcp._workspace_candidate_identity',
                       side_effect=AssertionError('candidate identity must not be probed')) as probe, \
                    patch('modport.opencode_shell_mcp.sha256',
                          side_effect=AssertionError('XML identity must not be hashed')), \
                    patch('modport.handlers.sha256',
                          side_effect=AssertionError('case identity must not be hashed')), \
                    patch('modport.handlers._snapshot_stage_output', side_effect=snapshot):
                cases, assertions, errors, refs, unchanged, after = _collect_v29_case_results(
                    root=root, workspace=workspace, command=command, contract=contract,
                    declarations=declarations, executor_provenance={},
                    source_commit='host-source-provenance', candidate_before=None,
                    execution_nonce='current-execution', contract_valid=True,
                    reviewed_target=True, wiring_refs={}, phase='target')
            probe.assert_not_called()
            self.assertIsNone(unchanged)
            self.assertIsNone(after)
            self.assertEqual('passed', cases['live']['status'])
            self.assertEqual('host-source-provenance', cases['live']['source_commit'])
            self.assertNotIn('case_identity', cases['live'])
            self.assertNotIn('candidate_id', cases['live'])
            self.assertEqual('passed', assertions['live.health']['status'])
            self.assertEqual('skipped', cases['skipped']['status'])
            self.assertEqual('unverified', assertions['skipped.health']['status'])
            self.assertEqual('failed', assertions['failed.health']['status'])
            self.assertEqual('unverified', cases['missing']['status'])
            self.assertTrue(any('matched 0 results' in error for error in errors))
            self.assertEqual(3, len(refs))


if __name__ == '__main__':
    unittest.main()
