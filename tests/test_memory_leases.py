import fcntl
import os
from pathlib import Path
import signal
import tempfile
import time
import unittest

from modport.contracts import OperationInput
from modport.memory_admission import MemoryPolicy, MemorySnapshot
from modport.memory_leases import MemoryLeaseError, memory_permit


class MemoryLeaseTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.directory = self.root / 'leases'
        self.policy = MemoryPolicy(heavy_slot_bytes=100, host_guard_bytes=0)
        self.snapshot = MemorySnapshot(available_bytes=100, ceiling_bytes=100,
                                       source='test')

    def operation(self, *, deadline=None):
        options = {} if deadline is None else {'deadline_epoch': deadline}
        return OperationInput('run', 'task', 'coder', 'command', str(self.root),
                              options=options)

    @unittest.skipUnless(hasattr(os, 'fork'), 'requires POSIX flock process semantics')
    def test_crashed_lease_reserves_capacity_through_cleanup_grace(self):
        reader, writer = os.pipe()
        child = os.fork()
        if child == 0:
            os.close(reader)
            try:
                with memory_permit(self.operation(), probe=lambda: self.snapshot,
                        policy=self.policy, directory=self.directory,
                        wait_seconds=.01, stale_grace_seconds=.2):
                    os.write(writer, b'1')
                    os.kill(os.getpid(), signal.SIGKILL)
            finally:
                os._exit(2)

        os.close(writer)
        try:
            self.assertEqual(os.read(reader, 1), b'1')
            _, status = os.waitpid(child, 0)
            self.assertEqual(os.WTERMSIG(status), signal.SIGKILL)
            started = time.monotonic()
            with memory_permit(self.operation(), probe=lambda: self.snapshot,
                    policy=self.policy, directory=self.directory,
                    wait_seconds=.01, stale_grace_seconds=.2):
                elapsed = time.monotonic() - started
            self.assertGreaterEqual(elapsed, .15)
            self.assertEqual(list(self.directory.glob('*.lease')), [])
            self.assertEqual(list(self.directory.glob('*.stale-*')), [])
        finally:
            os.close(reader)
            try:
                os.kill(child, signal.SIGKILL)
            except ProcessLookupError:
                pass
            try:
                os.waitpid(child, 0)
            except ChildProcessError:
                pass

    def test_blocked_manager_lock_still_observes_operation_deadline(self):
        self.directory.mkdir(mode=0o700)
        manager_path = self.directory / 'manager.lock'
        manager = os.open(manager_path, os.O_CREAT | os.O_RDWR, 0o600)
        fcntl.flock(manager, fcntl.LOCK_EX)
        started = time.monotonic()
        try:
            with self.assertRaisesRegex(MemoryLeaseError, 'deadline expired'):
                with memory_permit(self.operation(deadline=time.time() + .12),
                        probe=lambda: self.snapshot, policy=self.policy,
                        directory=self.directory, wait_seconds=.02):
                    self.fail('blocked manager must not admit')
        finally:
            fcntl.flock(manager, fcntl.LOCK_UN)
            os.close(manager)
        self.assertLess(time.monotonic() - started, .75)

    def test_blocked_manager_lock_observes_explicit_maximum_wait(self):
        self.directory.mkdir(mode=0o700)
        manager_path = self.directory / 'manager.lock'
        manager = os.open(manager_path, os.O_CREAT | os.O_RDWR, 0o600)
        fcntl.flock(manager, fcntl.LOCK_EX)
        started = time.monotonic()
        try:
            with self.assertRaisesRegex(MemoryLeaseError, 'maximum wait expired'):
                with memory_permit(self.operation(), probe=lambda: self.snapshot,
                        policy=self.policy, directory=self.directory,
                        wait_seconds=.02, max_wait_seconds=.1):
                    self.fail('blocked manager must not admit')
        finally:
            fcntl.flock(manager, fcntl.LOCK_UN)
            os.close(manager)
        self.assertLess(time.monotonic() - started, .75)

    def test_zero_maximum_wait_allows_one_immediate_attempt(self):
        with memory_permit(self.operation(), probe=lambda: self.snapshot,
                policy=self.policy, directory=self.directory,
                wait_seconds=.02, max_wait_seconds=0):
            self.assertTrue(list(self.directory.glob('*.lease')))

    def test_malformed_unlocked_lease_fails_closed(self):
        self.directory.mkdir(mode=0o700)
        for suffix in ('.lease', '.stale-0'):
            with self.subTest(suffix=suffix):
                lease = self.directory / ('a' * 32 + suffix)
                lease.write_bytes(b'not-json')
                lease.chmod(0o600)

                with self.assertRaisesRegex(MemoryLeaseError, 'invalid memory reservation'):
                    with memory_permit(self.operation(), probe=lambda: self.snapshot,
                            policy=self.policy, directory=self.directory,
                            stale_grace_seconds=0):
                        self.fail('malformed lease must not be discarded or admitted')
                self.assertTrue(lease.exists())
                lease.unlink()


if __name__ == '__main__':
    unittest.main()
