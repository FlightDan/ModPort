"""Independent notification delivery, conservative recovery and process survival."""
import copy
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

from dispatcher_sdk.execution_kernel import SQLiteKernel
from dispatcher_sdk.orchestrator import Orchestrator
from modport.desktop_driver import PersistentSupervisor
from modport.desktop_state import DesktopState, read_json
from modport.evidence import atomic_json
from modport.platform_runtime import process_birth
from modport.run_monitor import pid_namespace
from modport.watchdog_events import accept_notification, notifications
from modport.watchdog_process import DesktopWatchdog, run_watchdog
from modport.workflow import WORKFLOW_VERSION


class WatchdogSDK:
    def __init__(self, run_id):
        self.summary = {'run_id': run_id, 'state': 'running', 'generation': 0}
        self.pending = []

    def get_run_summary(self, run_id):
        if run_id != self.summary['run_id']:
            raise ValueError('wrong Run')
        return copy.deepcopy(self.summary)

    def deliver_notifications(self, callback, **kwargs):
        delivered = 0
        for value in list(self.pending):
            callback(value)
            self.pending.remove(value)
            delivered += 1
        return delivered


class WatchdogProcessTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.store = DesktopState(self.temporary.name)
        self.instance = 'desktop-' + 'b' * 32
        self.root = self.store.run_dir(self.instance)
        self.root.mkdir()
        self.header = {'run_id': self.instance, 'run_dir': str(self.root),
                       'deadline_epoch': time.time() + 600, 'started_at': time.time(),
                       'request': {'budget': {'max_seconds': 600, 'max_tokens': 1000}},
                       'definition': {'workflow_version': WORKFLOW_VERSION}}
        atomic_json(self.root / 'run.json', self.header)
        self.store.register(self.instance, 'Independent watchdog')
        self.sdk = WatchdogSDK(self.instance)
        self.supervisor = Mock()
        self.operations = Mock()
        self.health = {'run_id': self.instance, 'run_dir': str(self.root),
                       'pid': 42, 'birth': 'boot:worker', 'pid_namespace': pid_namespace(),
                       'timestamp': time.time(), 'status': 'failed'}
        self.watchdog = DesktopWatchdog(self.store, self.instance, self.sdk,
            operations=self.operations, supervisor=self.supervisor)

    def step_with_driver(self, identity):
        with patch('modport.watchdog_process.read_driver_health', return_value=self.health), \
                patch('modport.watchdog_process.process_identity_state', return_value=identity):
            return self.watchdog.step()

    def test_notifications_are_durably_accepted_while_driver_lives(self):
        notification = {'notification_id': 'sdk-terminal-1', 'run_id': self.instance,
                        'generation': 0, 'kind': 'terminal', 'state': 'failed', 'task_id': 'coder.1'}
        self.sdk.pending.append(notification)
        value = self.step_with_driver(True)
        self.assertEqual(value['notifications_delivered'], 1)
        self.assertEqual(list(notifications(self.root, self.instance))[0][0], notification)
        self.operations.watchdog_recover.assert_not_called()
        self.supervisor.launch_driver.assert_not_called()

    def test_dead_driver_incident_is_durable_before_same_instance_restart(self):
        def launch(instance):
            self.assertEqual(instance, self.instance)
            incident = list(notifications(self.root, self.instance))[0][0]
            self.assertEqual(incident['kind'], 'driver_lost')
            self.assertEqual(incident['payload']['driver']['birth'], 'boot:worker')
        self.supervisor.launch_driver.side_effect = launch
        result = self.step_with_driver(False)
        self.assertEqual(result['reason'], 'dead_driver_restarted')
        self.supervisor.launch_driver.assert_called_once_with(self.instance)
        again = self.step_with_driver(False)
        self.assertEqual(again['reason'], 'driver_restart_already_requested')
        restored = DesktopWatchdog(self.store, self.instance, self.sdk,
            operations=self.operations, supervisor=self.supervisor)
        self.assertEqual(restored.last_restart, result['last_driver_restart'])
        self.assertEqual(len(list(notifications(self.root, self.instance))), 1)

    def test_unknown_and_other_namespace_never_restart(self):
        self.assertEqual(self.step_with_driver(None)['driver_identity'], 'unknown')
        self.health['pid_namespace'] = 'pid:[another]'
        self.assertEqual(self.step_with_driver(False)['driver_identity'], 'unknown')
        self.supervisor.launch_driver.assert_not_called()
        self.operations.watchdog_recover.assert_not_called()
        self.assertEqual(list(notifications(self.root, self.instance)), [])

    def test_fresh_launcher_marker_does_not_restart_previous_dead_driver(self):
        atomic_json(self.root / 'desktop-driver-state.json',
            {'instance': self.instance, 'execution_run_id': self.instance,
             'pid': 43, 'birth': 'boot:new', 'at': time.time(), 'state': 'launching'})
        self.assertEqual(self.step_with_driver(False)['driver_identity'], 'unknown')
        self.supervisor.launch_driver.assert_not_called()

    def test_terminal_failure_recovery_uses_public_policy_then_authoritative_generation(self):
        self.sdk.summary['state'] = 'failed'
        def recover(root, run_id):
            self.assertEqual((root, run_id), (self.root, self.instance))
            self.sdk.summary.update(state='running', generation=1)
            return {'status': 'reopened', 'run_id': run_id, 'state': 'running', 'generation': 1}
        self.operations.watchdog_recover.side_effect = recover
        result = self.step_with_driver(False)
        self.assertEqual(result['last_driver_restart']['generation'], 1)
        self.supervisor.launch_driver.assert_called_once_with(self.instance)
        self.assertEqual(read_json(self.root / 'run.json'), self.header)

    def test_success_user_cancel_and_budget_exhaustion_never_restart(self):
        for state in ('succeeded', 'cancelled'):
            self.sdk.summary['state'] = state
            self.assertEqual(self.step_with_driver(False)['state'], 'terminal')
        self.sdk.summary['state'] = 'failed'
        self.watchdog.header['deadline_epoch'] = time.time() - 1
        self.assertEqual(self.step_with_driver(False)['reason'], 'original_budget_exhausted')
        self.operations.watchdog_recover.assert_not_called()
        self.supervisor.launch_driver.assert_not_called()

    def test_policy_terminal_or_blocked_does_not_request_driver_restart(self):
        self.sdk.summary['state'] = 'failed'
        for status in ('terminal', 'blocked'):
            self.operations.watchdog_recover.return_value = {'status': status, 'reason': 'concrete recovery diagnosis'}
            result = self.step_with_driver(False)
            self.assertEqual(result['state'], 'terminal' if status == 'terminal' else 'running')
            self.assertEqual(result['reason'], 'concrete recovery diagnosis' if status == 'terminal' else 'recovery_blocked')
        self.supervisor.launch_driver.assert_not_called()

    def test_coordination_wait_diagnosis_remains_observable_with_bounded_rechecks(self):
        atomic_json(self.root / 'desktop-driver-state.json',
                    {'instance': self.instance, 'execution_run_id': self.instance,
                     'pid': 42, 'birth': 'boot:worker', 'pid_namespace': pid_namespace(), 'state': 'waiting'})
        self.operations.watchdog_recover.return_value = {'status': 'blocked', 'reason': 'effect_owner_recovery_required'}
        result = self.step_with_driver(False)
        self.assertEqual(result['state'], 'running')
        self.assertEqual(result['recovery']['reason'], 'effect_owner_recovery_required')
        self.step_with_driver(False)
        self.operations.watchdog_recover.assert_called_once_with(self.root, self.instance)
        self.sdk.pending.append({'notification_id': 'cleanup-arrived', 'run_id': self.instance,
                                 'generation': 0, 'kind': 'recovery_required'})
        self.step_with_driver(False)
        self.assertEqual(self.operations.watchdog_recover.call_count, 2)
        self.supervisor.launch_driver.assert_not_called()

    def test_budget_exhausted_nonterminal_dead_driver_requests_original_settlement(self):
        self.watchdog.header['deadline_epoch'] = time.time() - 1
        result = self.step_with_driver(False)
        self.assertEqual(result['reason'], 'original_budget_settlement_requested')
        self.supervisor.launch_driver.assert_called_once_with(self.instance)
        self.operations.watchdog_recover.assert_not_called()
        self.step_with_driver(False)
        self.supervisor.launch_driver.assert_called_once_with(self.instance)

    def test_concurrent_user_suppression_during_recovery_prevents_restart(self):
        self.sdk.summary['state'] = 'failed'
        def recover(*args):
            self.sdk.summary.update(state='running', generation=1)
            atomic_json(self.root / 'desktop-watchdog-control.json',
                        {'instance': self.instance, 'suppressed': True, 'reason': 'user_cancel'})
            return {'status': 'reopened'}
        self.operations.watchdog_recover.side_effect = recover
        self.assertEqual(self.step_with_driver(False)['reason'], 'user_suppressed')
        self.supervisor.launch_driver.assert_not_called()

    def test_existing_sdk_notification_is_acknowledged_only_after_mailbox_write(self):
        kernel = SQLiteKernel(self.root / 'kernel.sqlite3')
        self.addCleanup(kernel.close)
        sdk = Orchestrator(self.root / 'orchestrator.sqlite3', kernel)
        self.addCleanup(sdk.close)
        sdk.create_run(self.instance, command_id='probe.create', input=self.header,
                       definition=self.header['definition'])
        notification = {'notification_id': 'actual-sdk-stall', 'kind': 'stalled',
                        'target': {'run_id': self.instance, 'task_id': 'coder.1'}}
        sdk.enqueue_stall_notification(notification)
        watchdog = DesktopWatchdog(self.store, self.instance, sdk,
                                    operations=self.operations, supervisor=self.supervisor)
        watchdog.accept_notification = Mock(side_effect=PermissionError('raw mailbox permission failure'))
        with patch('modport.watchdog_process.read_driver_health', return_value=self.health), \
                patch('modport.watchdog_process.process_identity_state', return_value=True):
            refused = watchdog.step()
            self.assertEqual(sdk.load_notification('actual-sdk-stall')['state'], 'pending')
            self.assertIn('raw mailbox permission failure', refused['notification_error']['detail'])
            self.assertEqual(list(notifications(self.root, self.instance)), [])
            watchdog.accept_notification = accept_notification
            sdk.clock = lambda: time.time() + 2
            watchdog.step()
        accepted = list(notifications(self.root, self.instance))
        self.assertEqual(accepted[0][0]['notification_id'], 'actual-sdk-stall')
        self.assertEqual(sdk.load_notification('actual-sdk-stall')['state'], 'delivered')

    def test_startup_admission_failure_records_raw_error_without_creating_sdk_stores(self):
        with self.assertRaisesRegex(ValueError, 'existing SDK stores: kernel.sqlite3'):
            run_watchdog(self.store.root, self.instance)
        marker = read_json(self.root / 'desktop-watchdog-state.json')
        self.assertEqual(marker['state'], 'failed')
        self.assertIn('kernel.sqlite3', marker['error'])
        self.assertIn('kernel.sqlite3', self.store.get(self.instance)['launch_error'])
        self.assertFalse((self.root / 'kernel.sqlite3').exists())
        self.assertFalse((self.root / 'orchestrator.sqlite3').exists())

    def test_actual_independent_process_survives_driver_exit_and_honors_suppression(self):
        import sys
        kernel = SQLiteKernel(self.root / 'kernel.sqlite3')
        self.addCleanup(kernel.close)
        sdk = Orchestrator(self.root / 'orchestrator.sqlite3', kernel)
        self.addCleanup(sdk.close)
        self.header['deadline_epoch'] = time.time() - 1
        atomic_json(self.root / 'run.json', self.header)
        sdk.create_run(self.instance, command_id='survival.create', input=self.header,
                       definition=self.header['definition'])
        driver = subprocess.Popen([sys.executable, '-I', '-c', 'import sys; sys.stdin.buffer.read(1)'],
                                  stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        def cleanup(process):
            if process.poll() is None:
                process.kill()
            process.communicate(timeout=3)
        self.addCleanup(cleanup, driver)
        health = {**self.health, 'pid': driver.pid, 'birth': process_birth(driver.pid),
                  'schema_version': 1, 'status': 'running', 'timestamp': time.time()}
        atomic_json(self.root / 'artifacts' / 'monitor' / 'monitor-driver.json', health)
        command = PersistentSupervisor(self.store).watchdog_command(self.instance)
        # Exercise the production isolated bootstrap and watchdog loop while
        # replacing the native service-manager boundary; no live service is installed.
        command[3] = command[3].replace('from modport.watchdog_process import main;',
            'from modport.desktop_driver import PersistentSupervisor; '
            'PersistentSupervisor.launch_driver=lambda self,instance: {"kind":"test-settlement"}; '
            'from modport.watchdog_process import main;')
        watcher = subprocess.Popen(command + ['--poll-seconds', '0.05'],
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.addCleanup(cleanup, watcher)
        marker = self.root / 'desktop-watchdog-state.json'
        until = time.monotonic() + 5
        while time.monotonic() < until:
            if marker.exists() and read_json(marker).get('pid') == watcher.pid:
                break
            if watcher.poll() is not None:
                self.fail('watchdog exited before handshake: ' + watcher.communicate()[1].decode())
            time.sleep(.02)
        self.assertEqual(read_json(marker)['pid'], watcher.pid)
        driver.communicate(b'x', timeout=3)
        until = time.monotonic() + 5
        while time.monotonic() < until and not list(notifications(self.root, self.instance)):
            time.sleep(.02)
        self.assertIsNone(watcher.poll())
        self.assertEqual(list(notifications(self.root, self.instance))[0][0]['kind'], 'driver_lost')
        atomic_json(self.root / 'desktop-watchdog-control.json',
                    {'instance': self.instance, 'suppressed': True, 'reason': 'user_cancel'})
        _, error = watcher.communicate(timeout=3)
        self.assertEqual(watcher.returncode, 0, error.decode())
        self.assertEqual(read_json(marker)['reason'], 'user_suppressed')
        self.assertEqual(sdk.get_run_summary(self.instance)['state'], 'running')
        self.assertEqual(read_json(self.root / 'run.json'), self.header)


if __name__ == '__main__':
    unittest.main()
