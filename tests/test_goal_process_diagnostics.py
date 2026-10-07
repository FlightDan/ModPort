"""Real app-server transport process diagnostics without model traffic."""

from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from modport.goal_runtime import _Transport
from modport.process_diagnostics import CgroupMemoryEvents


class GoalProcessDiagnosticTests(unittest.TestCase):
    def _transport(self, code, *, before=None, after=None, before_error=None):
        real_popen = subprocess.Popen

        def launch(_args, **kwargs):
            return real_popen([sys.executable, '-c', code], **kwargs)

        lock = tempfile.TemporaryFile()
        self.addCleanup(lock.close)
        patches = [
            patch('modport.goal_runtime.subprocess.Popen', side_effect=launch),
            patch('modport.goal_runtime.current_lock_fds', return_value=()),
        ]
        if before_error is None:
            patches.append(patch('modport.goal_runtime.read_process_memory_events',
                                 return_value=before))
        else:
            patches.append(patch('modport.goal_runtime.read_process_memory_events',
                                 side_effect=before_error))
        patches.append(patch('modport.goal_runtime.read_memory_events', return_value=after))
        for active in patches:
            active.start()
            self.addCleanup(active.stop)
        return _Transport(Path('/tmp'), float('inf'), lambda _message: None,
                          lock_fd=lock.fileno())

    def test_shared_cgroup_oom_observation_does_not_attribute_exit_137(self):
        before = CgroupMemoryEvents('/test/shared', {'oom_kill': 4}, 1)
        after = CgroupMemoryEvents('/test/shared', {'oom_kill': 5}, 2)
        transport = self._transport(
            'import sys,time;time.sleep(.1);sys.exit(137)', before=before, after=after)
        transport.process.wait(timeout=2)

        evidence = transport.close()

        self.assertEqual(137, evidence['returncode'])
        self.assertEqual(signal.SIGKILL, evidence['signal_number'])
        self.assertEqual('cgroup_oom_observed', evidence['classification'])
        self.assertEqual('unknown', evidence['attribution'])
        self.assertFalse(evidence['host_requested_termination'])
        self.assertIsNone(evidence['host_requested_signal'])
        self.assertEqual('host_cleanup', evidence['cleanup_reason'])
        self.assertIsNotNone(evidence['target_birth'])
        self.assertEqual(before.to_dict(), evidence['cgroup_before'])
        self.assertEqual(after.to_dict(), evidence['cgroup_after'])

    def test_deadline_kill_survives_collection_error(self):
        transport = self._transport(
            'import time;time.sleep(10)', before_error=RuntimeError('probe failed'))

        evidence = transport.close(deadline_exceeded=True)

        self.assertEqual(-signal.SIGKILL, evidence['returncode'])
        self.assertEqual('deadline_exceeded', evidence['classification'])
        self.assertEqual('not_applicable', evidence['attribution'])
        self.assertTrue(evidence['host_requested_termination'])
        self.assertEqual(signal.SIGKILL, evidence['host_requested_signal'])
        self.assertEqual('host_cleanup', evidence['cleanup_reason'])
        self.assertIsNone(evidence['cgroup_before'])
        self.assertEqual(
            [{'phase': 'cgroup_before', 'error': 'RuntimeError'}],
            evidence['collection_errors'])

    def test_routine_host_cleanup_preserves_raw_signal_and_reason(self):
        transport = self._transport('import time;time.sleep(10)')

        evidence = transport.close(cleanup_reason='goal_accepted')

        self.assertEqual(-signal.SIGKILL, evidence['returncode'])
        self.assertEqual(signal.SIGKILL, evidence['signal_number'])
        self.assertEqual('signal_exit', evidence['classification'])
        self.assertTrue(evidence['host_requested_termination'])
        self.assertEqual(signal.SIGKILL, evidence['host_requested_signal'])
        self.assertEqual('goal_accepted', evidence['cleanup_reason'])


if __name__ == '__main__':
    unittest.main()
