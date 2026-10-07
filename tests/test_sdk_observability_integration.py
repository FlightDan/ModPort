"""Current workflow adapter authority and real local byte-drain witnesses."""
from __future__ import annotations
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from dispatcher_sdk.execution_kernel.budget import BudgetEnvelope, DeadlineConstraint, sample_clock
from dispatcher_sdk.observability.activity import ActivityRecorder
from dispatcher_sdk.observability.contracts import ObservationIdentity, ObservationOptions
from modport.contracts import OperationInput
from modport.execution_budget import (current_deadline_budget, current_sdk_context, execution_budget,
                                    remaining_timeout, reserve_settlement)
from modport.kernel_runtime import SDKHandler
from modport.telemetry import report_sdk_bytes, run_process
from modport import summary_transport
from modport.workflow import WORKFLOW_VERSION


class Context:
    def __init__(self, seconds=100, reserve=25):
        checkpoint = sample_clock()
        self.envelope = BudgetEnvelope((DeadlineConstraint('execution:e', 'execution',
            checkpoint.wall_at + seconds, reserve),), checkpoint, checkpoint.wall_at)
        self.command = SimpleNamespace(execution_id='r:source:1', timeout_seconds=seconds,
            payload={}, to_dict=lambda: {'payload': {}})
        self.lease = SimpleNamespace(expires_at=checkpoint.wall_at + 1)
        self.activity = ActivityRecorder(SimpleNamespace(options=ObservationOptions()),
            ObservationIdentity('r:source:1', 1, 1, 'revision'))
    @property
    def budget(self):
        return self.envelope.view()
    def derive_budget(self, **kwargs):
        return self.envelope.derive(**kwargs)


def operation(root='/tmp/unused-observability-test'):
    return OperationInput('r', 'source', 'source', 'r:source:1', root,
                          options={'workflow_version': WORKFLOW_VERSION})


class SDKObservabilityIntegrationTests(unittest.TestCase):
    def test_sdk_authority_ignores_lease_and_reserve_is_not_repeated(self):
        context = Context()
        command = operation()
        frozen = command.to_dict()
        with execution_budget(context):
            budget = current_deadline_budget(command)
            self.assertEqual(budget.settlement_reserve, 20)
            self.assertGreater(remaining_timeout(command, 100), 54)
            first = reserve_settlement(command, 5)
            second = reserve_settlement(command, 5)
            self.assertEqual(first, second)
            self.assertGreater(remaining_timeout(command, 100), 49)
        self.assertEqual(command.to_dict(), frozen)
        self.assertIsNone(current_sdk_context())

    def test_adapter_binds_context_before_input_hydration_and_restores_it(self):
        context = Context()
        adapter = SDKHandler(lambda command: None, 'test', enforce_memory=False)
        def hydrate(payload, ctx):
            self.assertIs(current_sdk_context(), context)
            self.assertGreater(context.budget.remaining_work_seconds, 70)
            report_sdk_bytes('stdout', b'pre-hydration')
            return {'observed': True}
        with patch.object(SDKHandler, '_execute', side_effect=hydrate):
            self.assertEqual(adapter({}, context), {'observed': True})
        self.assertIsNone(current_sdk_context())
        self.assertEqual(context.activity.snapshot()['metrics']['stdout_bytes']['count'], 13)

    def test_raw_nonnewline_bytes_arrive_before_process_completion_without_private_tail(self):
        context = Context()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            completed = threading.Event()
            failure = []
            def invoke():
                try:
                    with execution_budget(context):
                        run_process([sys.executable, '-c',
                            "import os,time;os.write(1,b'private-thinking');time.sleep(.35)"],
                            cwd=root, log=root/'process.log', timeout=2)
                except BaseException as error:
                    failure.append(error)
                finally:
                    completed.set()
            thread = threading.Thread(target=invoke)
            thread.start()
            deadline = time.monotonic() + 1
            while time.monotonic() < deadline:
                snapshot = context.activity.snapshot()
                metric = snapshot['metrics'].get('stdout_bytes', {})
                if metric.get('count') == 16:
                    break
                time.sleep(.01)
            self.assertFalse(completed.is_set())
            self.assertEqual(metric.get('count'), 16)
            self.assertNotIn('private-thinking', str(snapshot))
            thread.join(2)
            self.assertFalse(thread.is_alive())
            self.assertEqual(failure, [])
            self.assertGreaterEqual(context.activity.snapshot()['metrics']['tool_requests']['count'], 1)

    def test_reports_degrade_without_changing_business_result(self):
        context = Context()
        context.activity = SimpleNamespace(report_bytes=lambda *args, **kwargs: (_ for _ in ()).throw(OSError('busy')))
        with execution_budget(context):
            self.assertIsNone(report_sdk_bytes('stdout', b'actual bytes'))

    def test_summary_managed_sender_inherits_sdk_activity_context(self):
        from test_summary_transport import FakeOpenCodeServer
        from modport.opencode_runtime import OpenCodeServer
        context = Context()
        class ManagedSender(FakeOpenCodeServer):
            _directory = OpenCodeServer._directory
            send_message = OpenCodeServer.send_message
            def _request(self, method, path, **kwargs):
                self.observed_context = current_sdk_context()
                return self.response
        server = ManagedSender(delay=.02)
        request = SimpleNamespace(timeout=2, idle_timeout=1, target_tokens=100,
            output_byte_limit=1000, prompt='local fixture', model='gpt-6-luna', reasoning_effort='max')
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            server.cwd = str(root)
            with execution_budget(context), patch('modport.opencode_runtime.OpenCodeServer.start', return_value=server):
                result = summary_transport.summarize(request, command=operation(str(root)), root=root,
                    worktree=root, log_path=root/'summary.json')
            self.assertEqual(result, '{"summary":"fixed"}')
            self.assertIs(server.observed_context, context)
            metrics = context.activity.snapshot()['metrics']
            self.assertEqual(metrics['model_requests']['count'], 1)
            self.assertEqual(metrics['model_events']['count'], 1)
            self.assertNotIn('stdout_bytes', metrics)
            self.assertNotIn('fixed', str(context.activity.snapshot()))

    def test_current_workflow_progress_path_records_worker_entry(self):
        from modport import execution_progress
        with tempfile.TemporaryDirectory() as directory:
            payload = operation(directory).to_dict()
            with patch.object(execution_progress, 'write_execution_progress', return_value=True) as write:
                self.assertTrue(execution_progress.record_command_progress(directory,
                    {'payload': payload}, 'worker_entered'))
                write.assert_called_once()



if __name__ == '__main__':
    unittest.main()
