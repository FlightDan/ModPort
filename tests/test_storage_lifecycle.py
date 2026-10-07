from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from modport.evidence import atomic_json, digest, read_json
from modport.storage_lifecycle import (
    automatic_retention,
    record_settled_segment,
    retention_checkpoint,
    settled_segments,
)


class StorageLifecycleTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.root = self.base / 'run'
        self.archive = self.base / 'archive'
        self.root.mkdir()
        device = patch('modport.artifact_retention._device',
            side_effect=lambda path: 1 if Path(path).resolve() == self.root.resolve() else 2)
        device.start()
        self.addCleanup(device.stop)

    def header(self, segment, previous=None):
        value = {'run_id': segment, 'run_dir': str(self.root),
                 'logical_run_id': 'logical'}
        if previous is not None:
            value['continuation'] = {'previous_run_id': previous}
        value['header_sha256'] = digest(value)
        return value

    def install_chain(self, *segments):
        headers = []
        for position, segment in enumerate(segments):
            header = self.header(segment, segments[position - 1] if position else None)
            headers.append(header)
            if position < len(segments) - 1:
                atomic_json(self.root / f'artifacts/continuations/{segment}/run.json', header)
        atomic_json(self.root / 'run.json', headers[-1])
        return headers

    @staticmethod
    def snapshot(header, revision=7, state='failed'):
        return {'run_id': header['run_id'], 'state': state, 'generation': 0,
                'revision': revision, 'input': header,
                'tasks': {'task': {'attempts': [{'state': 'succeeded'}]}}}

    def test_real_chain_records_frozen_settlements_and_keeps_three_hot(self):
        headers = self.install_chain('one', 'two', 'three', 'four')
        old = self.root / 'artifacts/continuations/one'
        (old / 'prepared.json').write_bytes(b'old prepared payload')
        (old / 'rework-sources.json').write_bytes(b'old source catalog')
        atomic_json(old / 'successor.json', {'next_run_id': 'two'})
        for header in headers:
            self.assertTrue(record_settled_segment(self.root, self.snapshot(header)))

        self.assertEqual({'one', 'two', 'three', 'four'}, set(settled_segments(self.root)))
        plan = retention_checkpoint(self.root, archive_root=self.archive, apply=False)
        self.assertEqual(['four', 'three', 'two'], plan['retained_segments'])
        self.assertEqual(2, len([row for row in plan['candidates']
                                if row['status'] == 'eligible']))

        report = retention_checkpoint(self.root, archive_root=self.archive)
        self.assertEqual([], report['errors'])
        self.assertFalse((old / 'prepared.json').exists())
        self.assertFalse((old / 'rework-sources.json').exists())
        self.assertTrue((old / 'run.json').is_file())
        self.assertTrue((old / 'successor.json').is_file())
        for segment in ('two', 'three'):
            self.assertTrue((self.root / f'artifacts/continuations/{segment}/run.json').is_file())
        self.assertEqual('four', read_json(self.root / 'run.json')['run_id'])
        self.assertFalse(any(self.root.glob('*.sqlite3')))

    def test_snapshot_must_equal_the_frozen_local_header(self):
        header = self.install_chain('current')[0]
        changed = dict(header)
        changed['logical_run_id'] = 'forged'
        changed.pop('header_sha256')
        changed['header_sha256'] = digest(changed)
        with self.assertRaisesRegex(ValueError, 'frozen local header'):
            record_settled_segment(self.root, self.snapshot(changed))
        self.assertFalse((self.root / 'artifacts/storage/settled-segments.json').exists())

    def test_unsettled_attempt_and_stale_observation_do_not_grant_authority(self):
        header = self.install_chain('current')[0]
        snapshot = self.snapshot(header, revision=9)
        self.assertTrue(record_settled_segment(self.root, snapshot))
        self.assertFalse(record_settled_segment(self.root,
            self.snapshot(header, revision=8)))
        running = self.snapshot(header, revision=10)
        running['tasks']['task']['attempts'][0]['state'] = 'running'
        self.assertFalse(record_settled_segment(self.root, running))
        record = read_json(self.root / 'artifacts/storage/settled-segments.json')
        self.assertEqual(9, record['segments']['current']['revision'])

        recovered = self.snapshot(header, revision=1, state='succeeded')
        recovered['generation'] = 1
        self.assertTrue(record_settled_segment(self.root, recovered))
        record = read_json(self.root / 'artifacts/storage/settled-segments.json')
        self.assertEqual('succeeded', record['segments']['current']['state'])

    def test_missing_historical_header_revokes_only_that_local_authority(self):
        headers = self.install_chain('one', 'two')
        for header in headers:
            self.assertTrue(record_settled_segment(self.root, self.snapshot(header)))
        (self.root / 'artifacts/continuations/one/run.json').unlink()
        self.assertEqual(['two'], settled_segments(self.root))

    def test_automatic_failure_is_diagnostic_and_does_not_open_sdk_storage(self):
        header = self.install_chain('current')[0]
        malformed = self.snapshot(header)
        malformed['input'] = {'run_id': 'current'}
        report = automatic_retention(self.root, malformed)
        self.assertEqual('maintenance_required', report['status'])
        self.assertIn('header is invalid', report['error'])
        self.assertEqual(report,
            read_json(self.root / 'artifacts/storage/retention-status.json'))
        self.assertFalse(any(self.root.glob('*.sqlite3')))

    def test_no_candidates_report_names_the_three_protected_segments(self):
        self.install_chain('one', 'two', 'three')
        report = retention_checkpoint(self.root, archive_root=self.archive)
        self.assertEqual('no_eligible_artifacts', report['status'])
        self.assertEqual(['three', 'two', 'one'], report['protected_segments'])

    def test_storage_metadata_symlink_never_redirects_registry_or_report(self):
        header = self.install_chain('current')[0]
        outside = self.base / 'outside'
        outside.mkdir()
        (self.root / 'artifacts').mkdir()
        (self.root / 'artifacts/storage').symlink_to(outside)
        with self.assertRaisesRegex(ValueError, 'unsafe'):
            record_settled_segment(self.root, self.snapshot(header))
        self.assertFalse((outside / 'settled-segments.json').exists())
        report = automatic_retention(self.root)
        self.assertEqual('maintenance_required', report['status'])
        self.assertIn('report_write_error', report)
        self.assertFalse((outside / 'retention-status.json').exists())


if __name__ == '__main__':
    unittest.main()
