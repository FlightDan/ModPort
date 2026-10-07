"""Desktop scheduling and worker facts use separate read-only projections."""
import os
from pathlib import Path
import subprocess
import sqlite3
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from modport.desktop_state import DesktopState, publish_snapshot, read_json
from modport.evidence import atomic_json
from modport.workflow import WORKFLOW_VERSION


class DesktopExecutionLivenessTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.store = DesktopState(temporary.name)
        self.instance = 'desktop-' + 'b' * 32
        self.root = self.store.run_dir(self.instance)
        self.root.mkdir()
        self.header = {'run_id': self.instance, 'run_dir': str(self.root),
            'started_at': time.time(), 'request': {'budget': {'max_seconds': 600}},
            'definition': {'workflow_version': WORKFLOW_VERSION}}
        atomic_json(self.root / 'run.json', self.header)
        self.store.register(self.instance, 'Liveness probe')
        self.execution_id = 'liveness-worker'
        self.snapshot = {'run_id': self.instance, 'state': 'running', 'revision': 1,
            'tasks': {'coder.probe': {'attempts': [{'state': 'running',
                'command': {'execution_id': self.execution_id,
                    'payload': {'stage_id': 'coder', 'options': {'model': 'test'}}}}]}}}
        self.summary = SimpleNamespace(execution_id=self.execution_id,
            task_id='coder.probe', state='running', fence=7)
        self.availability = SimpleNamespace(run_state='running', observed_at=time.time(),
            kernel_source='test-host-supplied-source', summaries=(self.summary,),
            snapshot_consistency='non_atomic')
        self.report = {'execution_id': self.execution_id,
            'identity': {'run_id': self.instance, 'task_id': 'coder.probe', 'fence': 7},
            'processes': []}
        atomic_json(self.root / 'artifacts/monitor/sdk-observation-binding.json', {
            'run_id': self.instance, 'storage': {
                'path': str(self.root / 'kernel.sqlite3.observations.sqlite3'),
                'kernel_path': str(self.root / 'kernel.sqlite3'),
                'source_id': 'test-host-supplied-source'}})
        publish_snapshot(self.header, self.snapshot, force=True)

    def status(self):
        with patch('modport.run_monitor.read_run_availability', return_value=self.availability) as availability, \
                patch('dispatcher_sdk.observability.inspect_execution', return_value=self.report) as inspection:
            status = self.store.status(self.instance)
        self.assertEqual(availability.call_args.kwargs, {'sample_limit': 100, 'effect_scan_limit': 0})
        self.assertEqual(inspection.call_args.kwargs['source_id'], 'test-host-supplied-source')
        self.assertLessEqual(inspection.call_args.kwargs['timeout'], .1)
        return status, status['stages']['implementation']['items'][0]

    def worker(self, *, pid=None, state='unknown', namespace=None):
        pid = os.getpid() if pid is None else pid
        from modport.opencode_recovery import _read_stat
        process = _read_stat(pid)
        namespace = namespace or os.readlink('/proc/self/ns/pid')
        self.report['processes'] = [{'process_id': 'worker', 'state': state,
            'last_observed_state': 'alive', 'observed_at': time.time() - 600,
            'registration': {'pid': pid, 'namespace': namespace,
                'birth_identity': {'pid': pid, 'namespace': namespace,
                    'start_ticks': process['start_ticks']}}}]

    def test_published_sdk_running_does_not_claim_live_agent(self):
        persisted = read_json(self.root / 'desktop-status.json')
        item = persisted['stages']['implementation']['items'][0]
        self.assertEqual(item['sdk_active_agents'], 1)
        self.assertIsNone(item['active_agents'])
        self.assertEqual(item['worker_process']['state'], 'unknown')

    @unittest.skipUnless(os.name == 'posix' and Path('/proc/self/stat').exists(), 'Linux process identity required')
    def test_bound_worker_is_confirmed_alive_without_heartbeat(self):
        self.worker()
        status, item = self.status()
        self.assertEqual(item['worker_process']['state'], 'alive')
        self.assertEqual(item['active_agents'], 1)
        self.assertEqual(status['execution_health']['state'], 'running')
        self.assertEqual(status['acceptance_status'], 'unverified')

    @unittest.skipUnless(os.name == 'posix' and Path('/proc/self/stat').exists(), 'Linux process identity required')
    def test_exited_actual_worker_remains_sdk_running_and_is_interrupted(self):
        child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])
        self.addCleanup(lambda: child.poll() is None and child.kill())
        self.worker(pid=child.pid, state='alive')
        child.terminate()
        child.wait(timeout=5)
        status, item = self.status()
        self.assertEqual(status['status'], 'running')
        self.assertEqual(item['sdk_state'], 'running')
        self.assertEqual(item['worker_process']['state'], 'exited')
        self.assertEqual(item['active_agents'], 0)
        self.assertEqual(status['execution_health']['state'], 'interrupted')
        self.assertIn('实际执行已中断', status['notice'])

    def test_only_supervisor_observation_does_not_imply_worker_liveness(self):
        self.report['processes'] = [{'process_id': 'supervisor', 'state': 'alive'}]
        status, item = self.status()
        self.assertIsNone(item['active_agents'])
        self.assertEqual(status['execution_health']['state'], 'unknown')

    @unittest.skipUnless(os.name == 'posix' and Path('/proc/self/stat').exists(), 'Linux process identity required')
    def test_stale_alive_record_in_inaccessible_namespace_stays_unknown(self):
        self.worker(state='alive', namespace='pid:[unavailable]')
        with patch('modport.opencode_recovery._read_stat', side_effect=AssertionError('Must not inspect other namespace')):
            status, item = self.status()
        self.assertIsNone(item['active_agents'])
        self.assertEqual(item['worker_process']['reason'], 'worker_pid_namespace_unverified')
        self.assertEqual(status['execution_health']['unknown_agents'], 1)

    @unittest.skipUnless(os.name == 'posix' and Path('/proc/self/stat').exists(), 'Linux process identity required')
    def test_unreadable_pid_in_matching_namespace_stays_unknown(self):
        self.worker(state='alive')
        with patch('modport.opencode_recovery._read_stat', side_effect=ValueError('permission denied')):
            status, item = self.status()
        self.assertIsNone(item['active_agents'])
        self.assertEqual(item['worker_process']['reason'], 'worker_process_inaccessible')

    def test_durable_exit_is_observed_even_with_stale_projection(self):
        self.report['processes'] = [{'process_id': 'worker', 'state': 'exited', 'observed_at': time.time() - 600}]
        value = read_json(self.root / 'desktop-status.json')
        value['observed_at'] = time.time() - 600
        atomic_json(self.root / 'desktop-status.json', value)
        status, item = self.status()
        self.assertEqual(item['active_agents'], 0)
        self.assertEqual(status['execution_health']['state'], 'interrupted')
        self.assertIn('超过 30 秒', status['notice'])

    def test_old_fence_cannot_prove_current_worker_exit(self):
        self.report['identity']['fence'] = 6
        self.report['processes'] = [{'process_id': 'worker', 'state': 'exited'}]
        status, item = self.status()
        self.assertIsNone(item['active_agents'])
        self.assertEqual(item['worker_process']['reason'], 'worker_observation_identity_unverified')

    def test_terminal_cancellation_does_not_claim_process_cleanup(self):
        self.snapshot['state'] = 'cancelled'
        self.snapshot['tasks']['coder.probe']['attempts'][0]['state'] = 'cancelled'
        publish_snapshot(self.header, self.snapshot, force=True)
        with patch('modport.run_monitor.read_run_availability', side_effect=AssertionError('Terminal projection needs no read')):
            status = self.store.status(self.instance)
        item = status['stages']['implementation']['items'][0]
        self.assertEqual(status['status'], 'cancelled')
        self.assertEqual(item['active_agents'], 0)
        self.assertEqual(item['worker_process']['state'], 'unknown')
        self.assertEqual(item['worker_process']['reason'], 'sdk_execution_terminal_process_cleanup_unknown')

    def test_predecessor_projection_with_sqlite_contention_reports_unknown(self):
        projection = read_json(self.root / 'desktop-status.json')
        projection['execution_run_id'] = 'historical-predecessor'
        atomic_json(self.root / 'desktop-status.json', projection)
        with patch('modport.run_monitor.read_run_availability',
                   side_effect=sqlite3.OperationalError('database is locked')):
            status = self.store.status(self.instance)
        self.assertEqual(status['status'], 'unknown')
        self.assertIn('database is locked', status['notice'])
        self.assertEqual(status['acceptance_status'], 'unverified')


if __name__ == '__main__':
    unittest.main()
