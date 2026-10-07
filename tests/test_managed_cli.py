"""Service control must not turn settled failures into author retry loops."""

from pathlib import Path
import json
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from modport.cli import main


class ManagedCliTests(unittest.TestCase):
    def test_recent_heartbeat_does_not_claim_invisible_driver_exited(self):
        run = SimpleNamespace(run_id='r', run_dir=Path('/missing-test-run'),
            status='running', snapshot={'application_state': {}})
        with patch('modport.cli.MigrationOperations') as factory, \
                patch('modport.runner.read_driver_health', return_value={
                    'pid': 123, 'birth': 'old', 'status': 'running',
                    'timestamp': time.time()}), \
                patch('modport.run_monitor.process_alive', return_value=False), \
                patch('builtins.print') as printed:
            factory.return_value.status.return_value = run
            self.assertEqual(0, main(['status', '--run-dir', str(run.run_dir), '--run-id', 'r']))
        report = json.loads(printed.call_args.args[0])
        self.assertEqual('running', report['overall_status'])
        self.assertEqual('uncertain', report['runner_health']['status'])
        self.assertFalse(report['runner_health']['driver_alive'])

    def test_other_pid_namespace_does_not_trust_matching_local_pid(self):
        run = SimpleNamespace(run_id='r', run_dir=Path('/missing-test-run'),
            status='running', snapshot={'application_state': {}})
        with patch('modport.cli.MigrationOperations') as factory, \
                patch('modport.runner.read_driver_health', return_value={
                    'pid': 123, 'birth': 'boot:1', 'pid_namespace': 'pid:[driver]',
                    'status': 'running', 'timestamp': time.time()}), \
                patch('modport.run_monitor.pid_namespace', return_value='pid:[observer]'), \
                patch('modport.run_monitor.process_alive', return_value=True) as alive, \
                patch('builtins.print') as printed:
            factory.return_value.status.return_value = run
            self.assertEqual(0, main(['status', '--run-dir', str(run.run_dir), '--run-id', 'r']))
        report = json.loads(printed.call_args.args[0])
        alive.assert_not_called()
        self.assertEqual('running', report['overall_status'])
        self.assertEqual('uncertain', report['runner_health']['status'])
        self.assertEqual('driver_pid_namespace_unverified', report['runner_health']['reason'])

    def test_dead_driver_is_not_presented_as_healthy_running(self):
        run = SimpleNamespace(run_id='r', run_dir=Path('/missing-test-run'),
            status='running', snapshot={'application_state': {}})
        with patch('modport.cli.MigrationOperations') as factory, \
                patch('modport.runner.read_driver_health', return_value={
                    'pid': 123, 'birth': 'old', 'status': 'running', 'timestamp': 1}), \
                patch('modport.run_monitor.process_alive', return_value=False), \
                patch('builtins.print') as printed:
            factory.return_value.status.return_value = run
            self.assertEqual(0, main(['status', '--run-dir', str(run.run_dir), '--run-id', 'r']))
        report = json.loads(printed.call_args.args[0])
        self.assertEqual('running', report['execution']['state'])
        self.assertEqual('execution_interrupted', report['overall_status'])
        self.assertEqual('interrupted', report['runner_health']['status'])
        self.assertEqual('unverified', report['acceptance_status'])

    def test_drive_wait_and_settled_failure_require_attention_not_restart(self):
        for status in ('waiting', 'running', 'failed', 'cancelled', 'succeeded'):
            with self.subTest(status=status):
                run = SimpleNamespace(run_id='r', run_dir=Path('/missing-test-run'),
                    status=status, snapshot={'application_state': {'acceptance_status': 'unverified'}})
                with patch('modport.cli.MigrationOperations') as factory, patch('builtins.print'):
                    factory.return_value.resume.return_value = run
                    code = main(['drive', '--run-dir', str(run.run_dir), '--run-id', 'r'])
                self.assertEqual(0 if status == 'succeeded' else 78, code)
                factory.return_value.recover.assert_not_called()
                factory.return_value.retry.assert_not_called()
