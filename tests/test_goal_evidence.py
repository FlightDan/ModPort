"""Coder host proof files survive normalization and repair evidence copying."""
import copy
from hashlib import sha256
from pathlib import Path
import tempfile
import unittest

from modport.evidence import verified_path
from modport.goal_validation import export_goal_evidence
from modport.repair_evidence import snapshot_repair_evidence


class GoalEvidenceTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.files = {}
        self.candidate = 'artifacts/goal-checks/candidate/workspace'
        report = self.write_ref(self.candidate + '/.modport/goal-reports/task.json', b'acceptance report')
        report['path'] = '.modport/goal-reports/task.json'
        static = self.write_ref(self.candidate + '/metadata/output.json', b'{"static":"candidate"}')
        static.update(path='metadata/output.json', type='json_valid', passed=True)
        self.evidence = {
            'scope': 'task-check acceptance',
            'candidate': {'head': 'a' * 40, 'workspace': self.candidate,
                          'candidate_manifest': self.write_ref(
                              'artifacts/goal-checks/candidate/candidate-files.json', b'candidate manifest')},
            'acceptance_report': {**report, 'report': {'acceptance': []}},
            'checks': {'static': static},
        }
        for name in ('first', 'second'):
            base = 'artifacts/goal-checks/' + name
            workspace = base + '/workspace'
            report = self.write_ref(workspace + '/module/build/test-results/test/TEST-Suite.xml',
                                    (name + ' JUnit report').encode())
            report.update(path='module/build/test-results/test/TEST-Suite.xml', tests=1,
                          failures=0, errors=0, skipped=0)
            self.evidence['checks'][name] = {
                'type': 'gradle_regression', 'passed': True, 'tasks': [':module:test'],
                'workspace': workspace,
                'candidate_manifest': self.write_ref(base + '/candidate-files.json', (name + ' manifest').encode()),
                'stdout': self.write_ref(base + '/stdout.log', (name + ' stdout').encode()),
                'stderr': self.write_ref(base + '/stderr.log', (name + ' stderr').encode()),
                'regression_execution': {**self.write_ref(workspace + '/build/.modport-regression/nonce.json',
                                                         (name + ' execution').encode()),
                                         'nonce': 'b' * 64, 'tasks': {':module:test': {'tests': 1}}},
                'reports': [report],
            }

    def write_ref(self, relative, contents):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(contents)
        self.files[relative] = contents
        return {'path': relative, 'sha256': sha256(contents).hexdigest()}

    def test_distinct_snapshot_reports_and_candidate_proof_export_with_run_paths(self):
        original = copy.deepcopy(self.evidence)
        normalized, refs = export_goal_evidence(self.root, self.evidence)
        self.assertEqual(original, self.evidence)
        self.assertEqual(self.candidate + '/metadata/output.json', normalized['checks']['static']['path'])
        self.assertEqual(self.candidate + '/.modport/goal-reports/task.json',
                         normalized['acceptance_report']['path'])
        self.assertEqual(set(self.files), {ref['path'] for ref in refs.values()})
        self.assertEqual(len(self.files), len(refs))
        for name in ('first', 'second'):
            check = normalized['checks'][name]
            self.assertEqual(check['workspace'] + '/module/build/test-results/test/TEST-Suite.xml',
                             check['reports'][0]['path'])
            self.assertEqual(original['checks'][name]['stdout'], check['stdout'])
            self.assertEqual(original['checks'][name]['regression_execution'], check['regression_execution'])
        for alias, ref in refs.items():
            self.assertTrue(alias.startswith('goal_host_evidence:'))
            self.assertEqual(self.files[ref['path']], verified_path(self.root, ref).read_bytes())

    def test_flat_exports_copy_complete_proof_without_expanding_candidate_manifest(self):
        original = copy.deepcopy(self.evidence)
        normalized, refs = export_goal_evidence(self.root, self.evidence)
        packet = snapshot_repair_evidence(self.root, {'proof': normalized, 'artifact_refs': refs})
        child = self.root / 'child'
        child.mkdir()
        # Retry copies exported refs. It does not discover files by opening
        # candidate manifests or the host validation summary.
        for ref in packet['artifact_refs'].values():
            destination = child / ref['path']
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(verified_path(self.root, ref).read_bytes())
        def verify(value):
            if isinstance(value, list):
                for item in value:
                    verify(item)
            elif isinstance(value, dict):
                if 'path' in value and 'sha256' in value:
                    contents = verified_path(child, value).read_bytes()
                    self.assertEqual(value['sha256'], sha256(contents).hexdigest())
                else:
                    for item in value.values():
                        verify(item)
        verify(packet['proof'])
        for name in ('first', 'second'):
            report = packet['proof']['checks'][name]['reports'][0]
            self.assertEqual((name + ' JUnit report').encode(), verified_path(child, report).read_bytes())
        self.assertEqual(original, self.evidence)
        self.assertEqual(set(self.files.values()),
                         {verified_path(child, ref).read_bytes() for ref in packet['artifact_refs'].values()})

    def test_missing_proof_and_paths_escaping_candidate_or_run_are_rejected(self):
        for mutate in (
            lambda proof: proof['checks']['first']['reports'][0].update(path='../outside.xml'),
            lambda proof: proof['checks']['first'].update(workspace='../outside'),
            lambda proof: proof['checks']['first']['stdout'].update(path='/tmp/outside.log'),
            lambda proof: proof['acceptance_report'].update(path='../../../outside.json'),
            lambda proof: proof['checks']['static'].update(path='metadata/missing.json'),
        ):
            proof = copy.deepcopy(self.evidence)
            mutate(proof)
            with self.subTest(proof=proof), self.assertRaises((OSError, ValueError)):
                export_goal_evidence(self.root, proof)

    def test_symlinked_proof_file_is_rejected(self):
        path = self.root / self.evidence['checks']['first']['stdout']['path']
        path.unlink()
        path.symlink_to(self.root / self.evidence['checks']['second']['stdout']['path'])
        with self.assertRaises((OSError, ValueError)):
            export_goal_evidence(self.root, self.evidence)
