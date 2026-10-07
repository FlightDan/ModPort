"""Disk budgets cover real SDK backup writes, growth and concurrent observers."""

from pathlib import Path
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from modport import snapshot_storage as storage


class SnapshotStorageTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = self.root / 'source.sqlite3'
        with sqlite3.connect(self.source) as connection:
            connection.execute('CREATE TABLE facts (data BLOB)')
            connection.execute('INSERT INTO facts VALUES (?)', (b'original',))

    def grow(self, source, size):
        with sqlite3.connect(source) as connection:
            connection.execute('INSERT INTO facts VALUES (zeroblob(?))', (size,))

    def test_committed_wal_is_copied_and_source_is_unchanged(self):
        connection = sqlite3.connect(self.source)
        self.addCleanup(connection.close)
        connection.execute('PRAGMA journal_mode=WAL')
        connection.execute('INSERT INTO facts VALUES (?)', (b'committed-wal',))
        connection.commit()
        before = {p.name: p.read_bytes() for p in self.root.glob('source.sqlite3*')
                  if not p.name.endswith('-shm')}
        with storage.snapshot_databases([self.source]) as destination:
            with sqlite3.connect(destination / self.source.name) as reader:
                self.assertEqual(reader.execute('SELECT data FROM facts').fetchall(),
                                 [(b'original',), (b'committed-wal',)])
        self.assertFalse(destination.exists())
        self.assertEqual(before, {p.name: p.read_bytes() for p in self.root.glob('source.sqlite3*')
                                  if not p.name.endswith('-shm')})

    def test_large_sparse_source_rejected_before_allocation_or_child(self):
        with self.source.open('r+b') as stream:
            stream.truncate(storage.MAX_SNAPSHOT_BYTES + 1)
        with patch.object(storage.subprocess, 'run') as run, \
                patch.object(storage.tempfile, 'TemporaryDirectory') as temporary:
            with self.assertRaisesRegex(storage.SnapshotLimitError, 'observation limit'):
                with storage.snapshot_databases([self.source]):
                    self.fail('oversized copy admitted')
        run.assert_not_called()
        temporary.assert_not_called()

    def test_wal_and_journal_count_towards_admission(self):
        for suffix in ('-wal', '-journal'):
            with self.subTest(suffix=suffix):
                sidecar = Path(str(self.source) + suffix)
                with sidecar.open('wb') as stream:
                    stream.truncate(storage.MAX_SNAPSHOT_BYTES)
                try:
                    with patch.object(storage.subprocess, 'run') as run:
                        with self.assertRaises(storage.SnapshotLimitError):
                            with storage.snapshot_databases([self.source]):
                                self.fail('oversized sidecar admitted')
                    run.assert_not_called()
                finally:
                    sidecar.unlink()

    def test_low_space_rejected_before_copy(self):
        required = 2 * storage.MAX_SNAPSHOT_BYTES + storage.MIN_FREE_BYTES
        with patch.object(storage.shutil, 'disk_usage', return_value=SimpleNamespace(free=required - 1)), \
                patch.object(storage.subprocess, 'run') as run:
            with self.assertRaisesRegex(storage.SnapshotLimitError, 'free-space reserve'):
                with storage.snapshot_databases([self.source]):
                    self.fail('low space admitted')
        run.assert_not_called()

    def test_space_is_rechecked_after_lock(self):
        with patch.object(storage.shutil, 'disk_usage', side_effect=[
                SimpleNamespace(free=10 * 1024**3), SimpleNamespace(free=1)]), \
                patch.object(storage.subprocess, 'run') as run:
            with self.assertRaises(storage.SnapshotLimitError):
                with storage.snapshot_databases([self.source]):
                    self.fail('stale space estimate admitted')
        run.assert_not_called()

    def test_another_process_cannot_start_a_parallel_snapshot(self):
        code = (
            'from modport.snapshot_storage import _observation_lock, SnapshotLimitError\n'
            'try:\n'
            '    with _observation_lock(): raise SystemExit(2)\n'
            'except SnapshotLimitError: raise SystemExit(0)\n'
        )
        environment = os.environ.copy()
        environment['PYTHONPATH'] = str(Path(storage.__file__).resolve().parent.parent)
        with storage._observation_lock():
            result = subprocess.run([sys.executable, '-c', code], env=environment,
                                    timeout=10, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_growth_after_admission_hits_os_limit_and_cleans_up(self):
        real_run = subprocess.run
        directories = []

        def growing_run(command, **options):
            self.grow(self.source, 128 * 1024)
            directories.append(Path(command[3]))
            return real_run(command, **options)

        with patch.object(storage, 'MAX_SNAPSHOT_BYTES', 64 * 1024), \
                patch.object(storage.subprocess, 'run', side_effect=growing_run):
            with self.assertRaisesRegex(storage.SnapshotLimitError, 'byte budget'):
                with storage.snapshot_databases([self.source]):
                    self.fail('growth bypassed file limit')
        self.assertEqual(len(directories), 1)
        self.assertFalse(directories[0].exists())
        with sqlite3.connect(self.source) as connection:
            self.assertEqual(connection.execute('PRAGMA quick_check').fetchone(), ('ok',))

    def test_two_databases_share_one_budget(self):
        second = self.root / 'second.sqlite3'
        second.write_bytes(self.source.read_bytes())
        real_run = subprocess.run

        def growing_run(command, **options):
            self.grow(self.source, 40 * 1024)
            self.grow(second, 40 * 1024)
            self.assertLess(self.source.stat().st_size, 64 * 1024)
            self.assertLess(second.stat().st_size, 64 * 1024)
            return real_run(command, **options)

        with patch.object(storage, 'MAX_SNAPSHOT_BYTES', 64 * 1024), \
                patch.object(storage.subprocess, 'run', side_effect=growing_run):
            with self.assertRaises(storage.SnapshotLimitError):
                with storage.snapshot_databases([self.source, second]):
                    self.fail('per-file limits bypassed the total budget')

    def test_timeout_reaps_child_before_removing_temporary_files(self):
        real_run = subprocess.run
        real_popen = subprocess.Popen
        children = []
        directories = []

        def track_child(*args, **kwargs):
            child = real_popen(*args, **kwargs)
            children.append(child)
            return child

        def slow_run(command, **options):
            directories.append(Path(command[3]))
            command = [sys.executable, '-c', 'import time; time.sleep(10)', *command[3:]]
            return real_run(command, **options)

        with patch.object(storage, 'COPY_TIMEOUT_SECONDS', 0.05), \
                patch.object(storage.subprocess, 'run', side_effect=slow_run), \
                patch.object(storage.subprocess, 'Popen', side_effect=track_child):
            with self.assertRaisesRegex(storage.SnapshotLimitError, 'timed out'):
                with storage.snapshot_databases([self.source]):
                    self.fail('copy did not time out')
        self.assertTrue(children)
        self.assertTrue(all(child.poll() is not None for child in children))
        self.assertTrue(all(not directory.exists() for directory in directories))

    def test_consumer_failure_cleans_up_and_releases_lock(self):
        with self.assertRaisesRegex(RuntimeError, 'consumer failed'):
            with storage.snapshot_databases([self.source]) as destination:
                raise RuntimeError('consumer failed')
        self.assertFalse(destination.exists())
        with storage._observation_lock():
            pass

    def test_isolated_helper_uses_the_hosts_validated_sdk_checkout(self):
        import dispatcher_sdk
        from modport.sdk_compat import sdk_module_identity
        package = self.root / 'checkout' / 'dispatcher_sdk'
        shutil.copytree(Path(dispatcher_sdk.__file__).resolve().parent, package,
                        ignore=shutil.ignore_patterns('__pycache__'))
        identity = sdk_module_identity()
        identity['module_path'] = str(package / '__init__.py')
        with patch('modport.sdk_compat.sdk_module_identity', return_value=identity):
            with storage.snapshot_databases([self.source]) as destination:
                self.assertTrue((destination / self.source.name).is_file())

    def test_helper_rejects_sdk_source_change_before_copying(self):
        from modport.sdk_compat import sdk_module_identity
        identity = sdk_module_identity()
        identity['source_sha256'] = '0' * 64
        with patch('modport.sdk_compat.sdk_module_identity', return_value=identity):
            with self.assertRaises(storage.SnapshotLimitError):
                with storage.snapshot_databases([self.source]):
                    self.fail('SDK identity mismatch was accepted')


if __name__ == '__main__':
    unittest.main()
