"""Host preparation deadlines and progress never charge model execution time."""
import json
import signal
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from modport.contracts import OperationInput, OperationResult
from modport.input_preparation import (prepare_inputs, preparation_phase,
                                       preparation_checkpoint, model_work)


class Handler:
    def __init__(self, work):
        self.work = work

    @prepare_inputs
    def __call__(self, command):
        self.work(command)
        return OperationResult('completed', command.run_id, command.task_id,
                               command.stage_id, command.command_id)


class InputPreparationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.command = OperationInput('run', 'task', 'contract_diagnose', 'command', str(self.root))

    def progress(self):
        return json.loads((self.root / 'artifacts/executions/command/input-preparation.json').read_text())

    def test_slow_host_operation_interrupts_with_precise_phase(self):
        if signal.getsignal(signal.SIGALRM) != signal.SIG_DFL:
            self.skipTest('process already owns SIGALRM')
        def work(command):
            with preparation_phase('reference_validation'):
                preparation_checkpoint(count='reference_checks', amount=3)
                time.sleep(2)
        started = time.monotonic()
        with patch.dict('os.environ', {'MODPORT_INPUT_PREPARATION_TIMEOUT_SECONDS': '0.08'}):
            result = Handler(work)(self.command)
        self.assertEqual(result.error_code, 'input_preparation_timeout')
        self.assertLess(time.monotonic() - started, 1.5)
        self.assertIn('reference_validation', result.detail)
        record = self.progress()
        self.assertEqual(record['status'], 'timed_out')
        self.assertEqual(record['interrupt_mode'], 'signal')
        self.assertEqual(record['counters']['reference_checks'], 3)
        self.assertEqual(signal.getsignal(signal.SIGALRM), signal.SIG_DFL)
        self.assertEqual(signal.getitimer(signal.ITIMER_REAL), (0.0, 0.0))

    def test_summary_and_coder_time_are_excluded_from_host_budget(self):
        def work(command):
            with preparation_phase('prompt_build'):
                preparation_checkpoint(count='prompt_bytes', amount=50)
                with model_work('summary_model'):
                    time.sleep(0.2)
                with model_work('coding_model'):
                    time.sleep(0.2)
        with patch.dict('os.environ', {'MODPORT_INPUT_PREPARATION_TIMEOUT_SECONDS': '0.15'}):
            result = Handler(work)(self.command)
        self.assertEqual(result.status, 'completed')
        record = self.progress()
        self.assertGreaterEqual(record['excluded_model_seconds'], 0.4)
        self.assertLess(record['host_seconds'], 0.15)
        self.assertEqual(record['phases']['prompt_build']['calls'], 1)

    def test_thread_workers_check_deadline_between_operations(self):
        def work(command):
            with preparation_phase('manifest_build'):
                time.sleep(0.05)
                preparation_checkpoint()
        outcomes = []
        with patch.dict('os.environ', {'MODPORT_INPUT_PREPARATION_TIMEOUT_SECONDS': '0.02'}):
            thread = threading.Thread(target=lambda: outcomes.append(Handler(work)(self.command)))
            thread.start()
            thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertEqual(outcomes[0].error_code, 'input_preparation_timeout')
        self.assertEqual(self.progress()['interrupt_mode'], 'checkpoints')

    def test_nested_handler_cannot_reset_host_budget(self):
        def child(command):
            with preparation_phase('child_prompt'):
                time.sleep(0.2)
        def parent(command):
            with preparation_phase('manifest_build'):
                time.sleep(0.04)
                return Handler(child)(command)
        with patch.dict('os.environ', {'MODPORT_INPUT_PREPARATION_TIMEOUT_SECONDS': '0.08'}):
            result = Handler(parent)(self.command)
        self.assertEqual(result.error_code, 'input_preparation_timeout')
        self.assertEqual(self.progress()['phase'], 'child_prompt')

    def test_run_deadline_prevents_preparation_before_work(self):
        from dataclasses import replace
        command = replace(self.command, options={'deadline_epoch': time.time() - 1})
        work = unittest.mock.Mock()
        result = Handler(work)(command)
        self.assertEqual(result.error_code, 'budget_exhausted')
        work.assert_not_called()

    def test_existing_signal_handler_is_preserved(self):
        original = signal.getsignal(signal.SIGALRM)
        def existing(signum, frame):
            pass
        signal.signal(signal.SIGALRM, existing)
        try:
            result = Handler(lambda command: preparation_checkpoint())(self.command)
            self.assertEqual(result.status, 'completed')
            self.assertIs(signal.getsignal(signal.SIGALRM), existing)
            self.assertEqual(self.progress()['interrupt_mode'], 'checkpoints')
        finally:
            signal.signal(signal.SIGALRM, original)

    def test_invalid_configuration_never_starts_work(self):
        for value in ('0', '-1', 'nan', 'inf', 'unlimited', 'bad', '301', '1e100'):
            with self.subTest(value=value), patch.dict('os.environ', {
                    'MODPORT_INPUT_PREPARATION_TIMEOUT_SECONDS': value}):
                work = unittest.mock.Mock()
                result = Handler(work)(self.command)
                self.assertEqual(result.error_code, 'input_preparation_configuration_invalid')
                work.assert_not_called()

    def test_slow_initial_and_final_audit_remain_bounded(self):
        if signal.getsignal(signal.SIGALRM) != signal.SIG_DFL:
            self.skipTest('process already owns SIGALRM')
        original = Path.write_text
        for slow_call in (1, 2):
            calls = []
            def slow_write(path, *args, **kwargs):
                if path.name == 'input-preparation.tmp':
                    calls.append(path)
                    if len(calls) == slow_call:
                        time.sleep(2)
                return original(path, *args, **kwargs)
            with self.subTest(slow_call=slow_call), patch.object(Path, 'write_text', slow_write), patch.dict(
                    'os.environ', {'MODPORT_INPUT_PREPARATION_TIMEOUT_SECONDS': '0.08'}):
                started = time.monotonic()
                result = Handler(lambda command: None)(self.command)
            self.assertEqual(result.error_code, 'input_preparation_timeout')
            self.assertLess(time.monotonic() - started, 1)
            self.assertEqual(len(calls), slow_call)
            self.assertEqual(signal.getsignal(signal.SIGALRM), signal.SIG_DFL)
            self.assertEqual(signal.getitimer(signal.ITIMER_REAL), (0.0, 0.0))

    def test_model_installed_signal_handler_and_timer_survive_cleanup(self):
        original = signal.getsignal(signal.SIGALRM)
        original_timer = signal.getitimer(signal.ITIMER_REAL)
        if original != signal.SIG_DFL or original_timer != (0.0, 0.0):
            self.skipTest('process already owns SIGALRM')
        def backend_alarm(signum, frame):
            pass
        def work(command):
            with model_work('summary_model'):
                self.assertEqual(signal.getsignal(signal.SIGALRM), signal.SIG_DFL)
                signal.signal(signal.SIGALRM, backend_alarm)
                signal.setitimer(signal.ITIMER_REAL, 30, 30)
            preparation_checkpoint()
        try:
            result = Handler(work)(self.command)
            self.assertEqual(result.status, 'completed')
            self.assertIs(signal.getsignal(signal.SIGALRM), backend_alarm)
            remaining, interval = signal.getitimer(signal.ITIMER_REAL)
            self.assertGreater(remaining, 20)
            self.assertEqual(interval, 30)
            self.assertEqual(self.progress()['interrupt_mode'], 'checkpoints')
        finally:
            signal.setitimer(signal.ITIMER_REAL, 0)
            signal.signal(signal.SIGALRM, original)
