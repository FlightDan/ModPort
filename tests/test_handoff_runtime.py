"""Fresh SDK Run integration for evidence-only imports."""
from dataclasses import replace
import json
from pathlib import Path
import sqlite3
import unittest

import test_artifact_handoff as fixture
from fixtures_modport import registry
from modport.artifact_handoff import prepare_handoff
from modport.contracts import OperationInput
from modport.evidence import atomic_json
from modport.handlers import ValidateInputHandler
from modport.models import MigrationRequest
from modport.operations import MigrationOperations


class HandoffRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixture.ArtifactHandoffTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.request = MigrationRequest(
            'example', 'https://example.invalid/mod.git', '1.20.1', '1.21.1',
            source_revision=self.fixture.source_commit)
        header = json.loads((self.fixture.root / 'run.json').read_text())
        header['request'] = self.request.to_dict()
        atomic_json(self.fixture.root / 'run.json', header)
        self.package = self.fixture.base / 'package'
        prepare_handoff(self.fixture.root, self.package, ['reports/latest.json'])
        self.root = self.fixture.base / 'fresh'
        self.ops = MigrationOperations(handlers=registry(), isolation_mode='thread')

    def submit(self):
        return self.ops.submit(self.request, run_dir=self.root, run_id='fresh',
                               artifact_handoff=self.package)

    def command(self, run):
        header = run.snapshot['input']
        return OperationInput('fresh', 'source', 'source', 'fresh.source', str(self.root),
            payload={'request': self.request.to_dict()}, artifact_refs=header['initial_refs'],
            options={'acceptance_rubric_sha256': header['rubric_sha256']})

    def test_new_schema_three_run_has_no_inherited_scheduler_history(self):
        run = self.submit()
        self.assertEqual('fresh', run.snapshot['run_id'])
        self.assertEqual(0, run.snapshot['revision'])
        self.assertEqual({}, run.snapshot['tasks'])
        self.assertIsNone(run.snapshot['application_state'])
        header = run.snapshot['input']
        self.assertIsNone(header['parent_run_id'])
        self.assertEqual('unverified', header['artifact_handoff']['acceptance_status'])
        self.assertNotIn('locked_manifest', header['initial_refs'])
        self.assertIn('handoff:reports/latest.json', header['initial_refs'])
        with sqlite3.connect((self.root/'orchestrator.sqlite3').as_uri()+'?mode=ro', uri=True) as db:
            self.assertEqual([('orchestrator', 3)], db.execute('select * from sdk_schema_meta').fetchall())
            self.assertEqual([('fresh',)], db.execute('select run_id from sdk_runs').fetchall())

    def test_source_reconstructs_distinct_baseline_and_target_without_remote_clone(self):
        run = self.submit()
        command = self.command(run)
        for _ in range(2):
            result = ValidateInputHandler()(command)
            self.assertEqual('completed', result.status, result.detail)
        self.assertEqual(self.fixture.source_commit, fixture.git(self.root/'baseline', 'rev-parse', 'HEAD'))
        self.assertEqual(self.fixture.target_commit, fixture.git(self.root/'worktree', 'rev-parse', 'HEAD'))
        self.assertFalse((self.root/'baseline/src.txt').exists())
        self.assertEqual('migrated\n', (self.root/'worktree/src.txt').read_text())
        source = json.loads((self.root/'artifacts/source.json').read_text())
        self.assertEqual(self.request.source_repository, source['source_repository'])
        self.assertFalse(source['artifact_handoff']['scheduler_history_imported'])

    def test_request_mismatch_rejected_before_creating_new_root(self):
        for request in (replace(self.request, target_minecraft='9.9'),
                        replace(self.request, source_revision=self.fixture.target_commit)):
            with self.assertRaisesRegex(ValueError, 'artifact handoff'):
                self.ops.submit(request, run_dir=self.root, artifact_handoff=self.package)
            self.assertFalse(self.root.exists())

    def test_installed_evidence_tampering_rejected_before_checkout(self):
        run = self.submit()
        (self.root/'artifacts/handoff/files/reports/latest.json').write_text('tampered')
        result = ValidateInputHandler()(self.command(run))
        self.assertEqual('failed', result.status)
        self.assertEqual('command_artifact_invalid', result.error_code)
        self.assertFalse((self.root/'repository.git').exists())

    def test_parent_scheduler_cannot_be_combined_with_artifact_import(self):
        with self.assertRaisesRegex(ValueError, 'scheduler parent'):
            self.ops.submit(self.request, run_dir=self.root, parent=object(), artifact_handoff=self.package)
        self.assertFalse(self.root.exists())


if __name__ == '__main__':
    unittest.main()
