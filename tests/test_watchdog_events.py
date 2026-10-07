"""Real SDK inactivity sampling with the production activity and mailbox bridge."""
from pathlib import Path
import tempfile
import threading
import time
import unittest

from dispatcher_sdk.execution_kernel import Kernel, CASConflictError
from dispatcher_sdk.observability import ObservationOptions
from dispatcher_sdk.orchestrator import Orchestrator, OrchestratorHost
from modport.application_state_storage import hydrate_run_snapshot
from modport.contracts import OperationResult
from modport.execution_budget import current_sdk_context
from modport.kernel_runtime import sdk_handlers
from modport.models import Budget, MigrationRequest
from modport.operations import MigrationOperations
from modport.telemetry import report_sdk_model
from modport.watchdog_events import accept_notification, install_runtime_watches, notifications
from modport.workflow import compile_migration_workflow


class QuietModel:
    __execution_kernel_revision__ = 'watchdog-real-model-activity-witness'

    def __init__(self):
        self.entered, self.release, self.respond = threading.Event(), threading.Event(), threading.Event()

    def __call__(self, operation):
        report_sdk_model('request')
        self.entered.set()
        while not self.release.wait(.02):
            current_sdk_context().activity.heartbeat()
            if self.respond.is_set():
                report_sdk_model('tool_activity')
        return OperationResult('completed', operation.run_id, operation.task_id,
                               operation.stage_id, operation.command_id)


class WatchdogEventsTests(unittest.TestCase):
    def until(self, check, seconds=8):
        limit = time.monotonic() + seconds
        while time.monotonic() < limit:
            value = check()
            if value:
                return value
            time.sleep(.03)
        self.fail('bounded SDK observation did not arrive')

    def test_real_response_watch_ignores_heartbeat_and_invalidates_delayed_cancel(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            assignment = QuietModel()
            with Kernel.open_sqlite(root / 'kernel.sqlite3', sdk_handlers({'modport.migration_plan': assignment}),
                    isolation_mode='thread', observation_options=ObservationOptions(flush_interval=.05)) as runtime:
                sdk = Orchestrator(root / 'orchestrator.sqlite3', runtime.kernel, runtime=runtime)
                self.addCleanup(sdk.close)
                request = MigrationRequest('watchdog', 'https://example.invalid/mod', '1.20.1', '26.1.2',
                    budget=Budget(max_seconds=600, max_agent_assignments=10))
                header = {'run_id': 'run', 'run_dir': str(root), 'request': request.to_dict(),
                    'definition': compile_migration_workflow(request).to_dict(),
                    'registry_revision': runtime.registry_revision, 'deadline_epoch': time.time() + 600,
                    'initial_refs': {}, 'prior_findings': [], 'rubric_sha256': 'host-provenance',
                    'watchdog_policy': {'enabled': True, 'inactivity_seconds': .15}}
                snapshot = sdk.create_run('run', command_id='create', input=header, definition=header['definition'])
                owner = MigrationOperations()
                app = owner._new_application()
                operations = owner._schedule(snapshot, header, app, 'migration_plan', dependencies=[])
                sdk.apply_operations('run', command_id='schedule', expected_revision=snapshot['revision'],
                                     operations=operations, application_state=app)
                with OrchestratorHost(sdk, callback=lambda notice: accept_notification(root, notice), worker_count=1):
                    try:
                        self.assertTrue(assignment.entered.wait(5))
                        sdk.sync()
                        snapshot = hydrate_run_snapshot(root, sdk.get_run('run'))
                        install_runtime_watches(runtime, sdk, header, snapshot)
                        notice = self.until(lambda: next((row for row, _ in notifications(root, 'run')
                                                         if row['kind'] == 'stalled'), None))
                        self.assertEqual(notice['target']['task_id'], 'migration_plan')
                        self.assertEqual(notice['target']['generation'], 0)
                        self.assertFalse(assignment.release.is_set())
                        assignment.respond.set()
                        self.until(lambda: any(row['state'] == 'activity' for row in runtime.stall_windows(
                            notice['execution_id'])['windows']))
                        with self.assertRaises(CASConflictError):
                            runtime.cancel_if_stalled(notice, reason='stale supervisor reply')
                        self.assertEqual(runtime.kernel.get(notice['execution_id']).state, 'running')
                    finally:
                        assignment.release.set()
