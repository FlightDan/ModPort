"""Portable authoring checks for the native Windows transport branch.

These tests replace the Win32 launcher; native Job/ACL/pipe acceptance still
requires a Windows machine. They exercise production branch orchestration.
"""
import io
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from modport.opencode_runtime import OpenCodeCleanupError, OpenCodeServer


class NativeJob:
    pid = 900013
    birth = 'windows:creation-time'
    def __init__(self, *, fail_cleanup=False):
        self.args = [sys.executable, 'serve']
        self.returncode = None
        self.stdout = io.BytesIO(b'output from stdout\n')
        self.stderr = io.BytesIO(b'output from stderr\n')
        self.cleanup_confirmed = False
        self.closed = False
        self.fail_cleanup = fail_cleanup
        self.terminations = 0
    def poll(self):
        return self.returncode
    def terminate_tree(self, timeout=5):
        self.terminations += 1
        if self.fail_cleanup:
            raise TimeoutError('Job descendants remain')
        self.cleanup_confirmed = True
    def wait(self, timeout=None):
        self.returncode = 1
        return self.returncode
    def close(self):
        self.closed = True
        self.stdout.close()
        self.stderr.close()


class WindowsTransportAuthoringTests(unittest.TestCase):
    def start_server(self, root, job):
        with patch('modport.opencode_runtime._windows_host', return_value=True), \
                patch('modport.opencode_runtime._available_loopback_port', return_value=19021), \
                patch('modport.windows_process.launch', return_value=job) as launch, \
                patch('modport.opencode_runtime.subprocess.Popen', side_effect=AssertionError('Windows must use native Job')), \
                patch('modport.opencode_runtime._proc_identity', return_value={'containment': 'windows_job'}), \
                patch.object(OpenCodeServer, '_request', return_value={'version': '1.18.32'}):
            server = OpenCodeServer.start(cwd=root, xdg_root=root/'xdg', executable=sys.executable, lock_fd=12)
            self.assertNotIn('pass_fds', launch.call_args.kwargs)
            self.assertNotIn('start_new_session', launch.call_args.kwargs)
            self.assertIn('stdin', launch.call_args.kwargs)
            self.assertIn('OPENCODE_DB', launch.call_args.kwargs['environment'])
            return server

    def test_native_job_launch_drains_both_pipes_and_closes_owned_job(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            job = NativeJob()
            server = self.start_server(root, job)
            for thread in server._log_threads:
                thread.join(timeout=1)
            log = (root/'xdg'/'server.stderr.log').read_text()
            self.assertIn('output from stdout', log)
            self.assertIn('output from stderr', log)
            self.assertEqual(server.process_birth, job.birth)
            with patch('modport.opencode_runtime._windows_host', return_value=True), \
                    patch('modport.opencode_runtime.os.killpg', side_effect=AssertionError('no PID group signalling')):
                diagnostic = server.close()
            self.assertTrue(diagnostic['cleanup_confirmed'])
            self.assertTrue(diagnostic['job_empty'])
            self.assertTrue(diagnostic['log_readers_stopped'])
            self.assertTrue(job.closed)
            self.assertEqual(job.terminations, 1)

    def test_exited_leader_keeps_cleanup_unknown_when_job_members_remain(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            job = NativeJob(fail_cleanup=True)
            server = self.start_server(root, job)
            with patch('modport.opencode_runtime._windows_host', return_value=True):
                diagnostic = server.close()
            self.assertTrue(diagnostic['leader_exited'])
            self.assertFalse(diagnostic['job_empty'])
            self.assertFalse(diagnostic['cleanup_confirmed'])
            self.assertIsNone(server.process_diagnostic)
            self.assertFalse(job.closed)
            job.fail_cleanup = False
            with patch('modport.opencode_runtime._windows_host', return_value=True):
                self.assertTrue(server.close()['cleanup_confirmed'])

    def test_goal_live_identity_probe_never_signals_windows_pid(self):
        from modport import goal_runtime
        class OS:
            name = 'nt'
            def kill(self, *args):
                raise AssertionError('Windows identity probes must not signal a PID')
        with patch.object(goal_runtime, 'os', OS()), \
                patch.object(goal_runtime, '_process_birth', return_value='windows:creation-time'):
            self.assertTrue(goal_runtime._previous_process_alive({'owned_pid': 22, 'owned_process_birth': 'windows:creation-time'}))
            self.assertFalse(goal_runtime._previous_process_alive({'owned_pid': 22, 'owned_process_birth': 'other-birth'}))
            self.assertFalse(goal_runtime._previous_process_alive({'owned_pid': 22, 'producer_stopped': True}))

    def test_current_linux_goal_uses_portable_lock_and_confirms_session_cleanup(self):
        from test_goal_runtime import FakeOpenCode
        from modport.contracts import OperationInput
        from modport.goal_runtime import run_goal
        from modport.workflow import WORKFLOW_VERSION
        class CurrentTransport(FakeOpenCode):
            def ownership_record(self):
                return {'pid': self.process.pid, 'birth': self.process_birth,
                        'cwd': str(self.cwd), 'argv': ['/fixture/opencode']}
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            worktree = root/'worktree'
            worktree.mkdir()
            (worktree/'.git').mkdir()
            CurrentTransport.instances = []
            CurrentTransport.sessions_by_root = {}
            CurrentTransport.behaviors = []
            command = OperationInput('current', 'coder', 'coder', 'current:coder:1', str(root),
                options={'workflow_version': WORKFLOW_VERSION, 'model': 'gpt-6-luna', 'reasoning_effort': 'max'})
            with patch('modport.goal_runtime.OpenCodeServer', CurrentTransport):
                result = run_goal(command=command, root=root, worktree=worktree, prompt='Implement scoped change', objective='Complete change', timeout=5, validate=lambda: {'accepted': True, 'failures': [], 'evidence': {}})
            self.assertEqual(result.returncode, 0, result.metadata.get('error'))
            self.assertTrue(result.metadata['producer_stopped'])
            self.assertTrue(result.metadata['thread_id'].startswith('ses'))
            self.assertEqual(result.metadata['cleanup_state'], 'confirmed')

    def test_native_process_birth_uses_platform_api(self):
        from modport.opencode_runtime import _process_birth
        with patch('modport.opencode_runtime._windows_host', return_value=True), \
                patch('modport.platform_runtime.process_birth', return_value='windows:creation-time') as birth:
            self.assertEqual(_process_birth(123), 'windows:creation-time')
            birth.assert_called_once_with(123)


if __name__ == '__main__':
    unittest.main()
