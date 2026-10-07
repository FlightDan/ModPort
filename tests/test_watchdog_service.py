"""Independent service ownership and actual watchdog process-entry receipts."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from modport.desktop_driver import PersistentSupervisor
from modport.desktop_state import DesktopState, read_json
from modport.evidence import atomic_json
from modport.platform_runtime import process_birth


class WatchdogServiceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.store = DesktopState(Path(self.temporary.name) / 'data with spaces %')
        self.instance = 'desktop-' + 'a' * 32
        self.root = self.store.run_dir(self.instance)
        self.root.mkdir()
        self.store.register(self.instance, 'Watchdog service probe')
        self.calls = []

    def execute(self, argv, **kwargs):
        self.calls.append(argv)
        if argv[0] == 'whoami':
            output = '"HOST\\user","S-1-5-21-123-456-789-1001"\n'
        else:
            output = 'active\n' if 'is-active' in argv else ''
        if (('enable' in argv or '/Run' in argv)
                and any('watchdog' in arg for arg in argv)):
            self.write_watchdog_marker()
        return subprocess.CompletedProcess(argv, 0, output, '')

    def write_watchdog_marker(self):
        atomic_json(self.root / 'desktop-watchdog-state.json',
                    {'instance': self.instance, 'pid': os.getpid(), 'birth': process_birth(os.getpid()),
                     'state': 'running', 'at': time.time()})

    def test_linux_installs_independent_services_and_recovery_only_restarts_driver(self):
        supervisor = PersistentSupervisor(self.store, platform_name='Linux', execute=self.execute)
        result = supervisor.launch(self.instance, environment={'OPENAI_API_KEY': 'service-test-key'})
        driver = (self.store.root / 'supervision' / result['unit']).read_text()
        watchdog = (self.store.root / 'supervision' / result['watchdog']['unit']).read_text()
        self.assertIn('modport.desktop_driver', driver)
        self.assertIn('RestartPreventExitStatus=78', driver)
        self.assertIn('RestartSec=30', driver)
        self.assertIn('modport.watchdog_process', watchdog)
        self.assertIn('Restart=on-failure', watchdog)
        self.assertIn('"-I"', watchdog)
        for unwanted in ('BindsTo=', 'PartOf=', 'Requires=', 'RestartPreventExitStatus=78', 'service-test-key'):
            self.assertNotIn(unwanted, watchdog)
        self.assertEqual(result['watchdog']['state'], 'running')
        control = self.root / 'desktop-watchdog-control.json'
        atomic_json(control, {'instance': self.instance, 'suppressed': True, 'reason': 'user_cancel'})
        credentials = self.store.root / 'host-runtime' / (self.instance + '.json')
        credentials_before = credentials.read_bytes()
        self.calls.clear()
        restarted = supervisor.launch_driver(self.instance)
        self.assertTrue(read_json(control)['suppressed'])
        self.assertEqual(credentials.read_bytes(), credentials_before)
        self.assertEqual(restarted['watchdog'], result['watchdog'])
        self.assertFalse(any('watchdog' in arg for command in self.calls for arg in command))

    def test_watchdog_start_failure_is_visible_and_preserves_driver_receipt(self):
        def execute(argv, **kwargs):
            if 'enable' in argv and any('watchdog' in item for item in argv):
                return subprocess.CompletedProcess(argv, 1, '', 'watchdog service admission failed')
            return self.execute(argv, **kwargs)
        supervisor = PersistentSupervisor(self.store, platform_name='Linux', execute=execute)
        with self.assertRaisesRegex(RuntimeError, 'watchdog service admission failed'):
            supervisor.launch(self.instance)
        record = self.store.get(self.instance)
        receipt = json.loads(record['supervisor'])
        self.assertIn(self.instance, receipt['unit'])
        self.assertEqual(receipt['watchdog']['state'], 'failed')
        self.assertIn('watchdog service admission failed', record['launch_error'])
        self.assertFalse(any('disable' in item for command in self.calls for item in command))

    def test_fast_terminal_handshake_uses_sdk_authority_instead_of_stale_projection(self):
        atomic_json(self.root / 'run.json', {'run_id': self.instance})
        marker = self.root / 'desktop-watchdog-state.json'
        atomic_json(marker, {'instance': self.instance, 'execution_run_id': self.instance,
                            'state': 'terminal', 'at': time.time()})
        supervisor = PersistentSupervisor(self.store, platform_name='Linux', execute=self.execute)
        with patch.object(self.store, 'status', side_effect=AssertionError('cached projection is not lifecycle authority')), \
                patch('modport.desktop_driver.read_run_availability', return_value=SimpleNamespace(run_state='failed')) as inspect:
            value = supervisor._wait_entry(self.instance, 'watchdog', requested_at=time.time() - 1)
        self.assertEqual(value['state'], 'terminal')
        inspect.assert_called_once_with(self.root, self.instance, sample_limit=0, effect_scan_limit=0)

    def test_suppression_precedes_stop_and_leaves_driver_cleanup_running(self):
        supervisor = PersistentSupervisor(self.store, platform_name='Linux', execute=self.execute)
        supervisor.launch(self.instance)
        self.calls.clear()
        def execute(argv, **kwargs):
            self.assertTrue(read_json(self.root / 'desktop-watchdog-control.json')['suppressed'])
            return self.execute(argv, **kwargs)
        supervisor.execute = execute
        supervisor.suppress_watchdog(self.instance, reason='user_cancel')
        self.assertEqual(read_json(self.root / 'desktop-watchdog-control.json')['reason'], 'user_cancel')
        self.assertEqual(len(self.calls), 1)
        self.assertIn('disable', self.calls[0])
        self.assertTrue(self.calls[0][-1].endswith('-watchdog.service'))
        self.calls.clear()
        supervisor.stop_driver(self.instance)
        self.assertEqual(len(self.calls), 1)
        self.assertNotIn('watchdog', self.calls[0][-1])

    def test_suppression_remains_durable_when_service_stop_fails(self):
        supervisor = PersistentSupervisor(self.store, platform_name='Linux', execute=self.execute)
        supervisor.launch(self.instance)
        supervisor.execute = lambda argv, **kwargs: subprocess.CompletedProcess(argv, 1, '', 'manager unavailable')
        suppressed = supervisor.suppress_watchdog(self.instance, reason='user_cancel')
        self.assertTrue(read_json(self.root / 'desktop-watchdog-control.json')['suppressed'])
        receipt = json.loads(self.store.get(self.instance)['supervisor'])
        self.assertEqual(receipt['watchdog']['state'], 'suppressed')
        self.assertIn('manager unavailable', receipt['watchdog']['stop_error'])
        self.assertEqual(suppressed, receipt['watchdog'])

    def test_suppression_storage_failure_remains_visible_and_does_not_stop_service(self):
        supervisor = PersistentSupervisor(self.store, platform_name='Linux', execute=self.execute)
        with patch('modport.desktop_driver.atomic_json', side_effect=PermissionError('control write denied')):
            with self.assertRaisesRegex(PermissionError, 'control write denied'):
                supervisor.suppress_watchdog(self.instance, reason='user_cancel')
        self.assertEqual(self.calls, [])

    def test_windows_uses_distinct_tasks_and_locale_independent_stop(self):
        supervisor = PersistentSupervisor(self.store, platform_name='Windows', execute=self.execute)
        birth = process_birth(os.getpid())
        atomic_json(self.root / 'desktop-driver-state.json',
                    {'instance': self.instance, 'pid': os.getpid(), 'birth': birth, 'state': 'running', 'at': time.time()})
        with patch('modport.runner.read_driver_health', return_value={'pid': os.getpid(), 'birth': birth, 'status': 'running'}):
            result = supervisor.launch(self.instance)
        self.assertNotEqual(result['task'], result['watchdog']['task'])
        self.assertEqual(result['watchdog']['native_acceptance'], 'pending')
        xml = (self.store.root / 'supervision' / (result['watchdog']['task'] + '.xml')).read_text(encoding='utf-16')
        self.assertIn('modport.watchdog_process', xml)
        self.assertIn('IgnoreNew', xml)
        self.assertIn('RestartOnFailure', xml)
        self.calls.clear()
        supervisor.suppress_watchdog(self.instance, reason='user_cancel')
        self.assertIn('/DISABLE', self.calls[0])
        self.assertEqual(self.calls[1][0], 'powershell.exe')
        self.assertIn('GetInstances(0).Count', self.calls[1][-1])
        self.assertIn(result['watchdog']['task'], self.calls[1][-1])
        self.assertFalse(any('/FO' in command for command in self.calls))

    def test_actual_process_handshake_rejects_marker_after_process_exit(self):
        supervisor = PersistentSupervisor(self.store, platform_name='Linux', execute=self.execute)
        source = str(Path(__file__).resolve().parents[1] / 'src')
        marker = self.root / 'desktop-watchdog-state.json'
        code = ('import os,sys,time; sys.path.insert(0,' + repr(source) + '); '
                'from pathlib import Path; from modport.evidence import atomic_json; '
                'from modport.platform_runtime import process_birth; '
                'atomic_json(Path(' + repr(str(marker)) + '), '
                + repr({'instance': self.instance, 'state': 'running'})
                + ' | dict(pid=os.getpid(),birth=process_birth(os.getpid()),at=time.time())); '
                'sys.stdin.buffer.read(1)')
        process = subprocess.Popen([sys.executable, '-I', '-c', code], stdin=subprocess.PIPE,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        def cleanup():
            if process.poll() is None:
                process.kill()
            process.communicate(timeout=3)
        self.addCleanup(cleanup)
        value = supervisor._wait_entry(self.instance, 'watchdog', requested_at=time.time())
        self.assertEqual(value['pid'], process.pid)
        self.assertEqual(value['birth'], process_birth(process.pid))
        process.communicate(b'x', timeout=3)
        self.assertEqual(process.returncode, 0)
        with patch('modport.desktop_driver.time.monotonic', side_effect=[0, 0, 13]), \
                patch('modport.desktop_driver.time.sleep'):
            with self.assertRaisesRegex(RuntimeError, 'watchdog entry was not observed'):
                supervisor._wait_entry(self.instance, 'watchdog', requested_at=time.time())


if __name__ == '__main__':
    unittest.main()
