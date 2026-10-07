"""Storage admission and replay checks at actual host entry points."""
import copy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from modport.contracts import OperationInput
from modport.continuation import continue_from_planner, _prepared_payload_digest
from modport.evidence import atomic_json, digest
from modport.operations import MigrationOperations, _verify_recovery_payload, _recovery_payload_digest
from modport.payload_storage import pack_input
from modport.storage_budget import StorageBudgetError


class StorageIntegrationTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)

    def oversized(self):
        with (self.root / 'orchestrator.sqlite3').open('wb') as output:
            output.truncate(512 * 1024 * 1024)

    def test_tick_rejects_before_sdk_observation(self):
        self.oversized()
        operations = MigrationOperations()
        with patch.object(operations, '_audited_observation') as observe:
            with self.assertRaises(StorageBudgetError):
                operations.tick(Mock(), {'run_dir': str(self.root)})
        observe.assert_not_called()
        self.assertEqual(512 * 1024 * 1024, (self.root / 'orchestrator.sqlite3').stat().st_size)

    def test_resume_does_not_allocate_a_status_backup(self):
        operations = MigrationOperations()
        with patch.object(operations, 'status') as status, patch.object(operations, 'execute') as execute:
            operations.resume(self.root, 'r')
        status.assert_not_called()
        self.assertEqual('r', execute.call_args.args[0].run_id)
        self.assertEqual(self.root, execute.call_args.args[0].run_dir)

    def broken_operations(self):
        value = OperationInput('logical', 'task', 'coder', 'segment:task:1', str(self.root),
                               payload={'original': 'x' * 100000}).to_dict()
        command = {'execution_id': value['command_id'], 'payload': pack_input(self.root, value)}
        for path in (self.root / 'audit-blobs').glob('*.json.gz'):
            path.unlink()
        return [{'kind': 'add_task', 'task_id': 'task', 'command': command}]

    def test_continuation_missing_blob_fails_before_opening_sdk_writer(self):
        operations = self.broken_operations()
        app = {'counter': 1}
        header = {'run_id': 'next', 'run_dir': str(self.root),
                  'continuation': {'prepared_payload_sha256': _prepared_payload_digest(app, operations)}}
        header['header_sha256'] = digest(header)
        packet = {'application_state': app, 'operations': operations, 'header': header}
        atomic_json(self.root / 'artifacts/continuations/next/prepared.json', packet)
        host = Mock()
        host._header.return_value = {'definition': {'workflow_version': 19}}
        with patch('modport.continuation.open_runtime') as writer:
            with self.assertRaises(ValueError):
                continue_from_planner(host, self.root, 'prior', next_run_id='next', reason='retry')
        writer.assert_not_called()

    def test_recovery_valid_outer_digest_cannot_hide_missing_command_blob(self):
        operations = self.broken_operations()
        app = {'counter': 1}
        decision = {'kind': 'reopen'}
        decision['prepared_payload_sha256'] = _recovery_payload_digest(app, operations, decision)
        with self.assertRaises(ValueError):
            _verify_recovery_payload(self.root, {
                'application_state': app, 'operations': operations, 'decision': decision})

    def test_recover_checks_capacity_before_session(self):
        self.oversized()
        operations = MigrationOperations()
        with patch.object(operations, '_header'), patch.object(operations, 'session') as session:
            with self.assertRaises(StorageBudgetError):
                operations.recover(self.root, 'r')
        session.assert_not_called()

    def test_packed_commands_require_binding_even_when_app_is_inline(self):
        value = OperationInput('logical', 'task', 'coder', 'cmd', str(self.root),
                               payload={'large': 'x' * 100000}).to_dict()
        operations = [{'kind': 'add_task', 'task_id': 'task',
                       'command': {'execution_id': 'cmd', 'payload': pack_input(self.root, value)}}]
        with self.assertRaisesRegex(ValueError, 'immutable binding'):
            _verify_recovery_payload(self.root, {
                'application_state': {}, 'operations': operations, 'decision': {}})

    def test_resume_rejects_large_artifact_tree_before_session(self):
        artifact = self.root / 'artifacts/large.bin'
        artifact.parent.mkdir()
        with artifact.open('wb') as output:
            output.truncate(20 * 1024 ** 3 + 1)
        operations = MigrationOperations()
        with patch.object(operations, '_header'), patch.object(operations, 'session') as session:
            with self.assertRaises(StorageBudgetError):
                operations.resume(self.root, 'r')
        session.assert_not_called()

    def test_scheduled_task_identity_matches_business_payload(self):
        from modport.payload_storage import verify_operations
        value = OperationInput('logical', 'task', 'coder', 'cmd', str(self.root)).to_dict()
        with self.assertRaisesRegex(ValueError, 'task identity'):
            verify_operations(self.root, [{'kind': 'add_task', 'task_id': 'another',
                'command': {'execution_id': 'cmd', 'payload': value}}])

    def submission(self):
        from fixtures_modport import registry
        from modport.models import MigrationRequest
        host = MigrationOperations(handlers=registry(), isolation_mode='thread')
        request = MigrationRequest('example', 'https://example.invalid/mod.git',
                                   '1.20.1', '26.1.2', source_revision='a' * 40)
        return host, request

    def test_existing_empty_directory_accepts_submission(self):
        host, request = self.submission()
        run = host.submit(request, run_dir=self.root, run_id='new')
        self.assertEqual('new', run.run_id)
        self.assertTrue((self.root / 'run.json').is_file())

    def test_rejected_nonempty_directory_is_not_modified(self):
        host, request = self.submission()
        (self.root / 'keep.txt').write_text('preserve this')
        with self.assertRaisesRegex(ValueError, 'empty directory'):
            host.submit(request, run_dir=self.root, run_id='new')
        self.assertEqual(['keep.txt'], sorted(p.name for p in self.root.iterdir()))

    def test_invalid_identifier_does_not_create_storage_diagnostics(self):
        host, request = self.submission()
        with self.assertRaisesRegex(ValueError, 'invalid run_id'):
            host.submit(request, run_dir=self.root, run_id='../bad')
        self.assertEqual([], list(self.root.iterdir()))

    def test_unsupported_existing_run_is_not_modified_by_control_admission(self):
        from modport.operations import LegacyRunError
        (self.root / 'control.sqlite3').write_bytes(b'legacy evidence')
        host = MigrationOperations()
        calls = [lambda: host.resume(self.root, 'old'),
                 lambda: host.recover(self.root, 'old'),
                 lambda: host.load_retry_parent(self.root, 'old'),
                 lambda: host.reopen(self.root, 'old', reason='retry')]
        for call in calls:
            with self.assertRaises(LegacyRunError):
                call()
            self.assertEqual(['control.sqlite3'], sorted(p.name for p in self.root.iterdir()))
