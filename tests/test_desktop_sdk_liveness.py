"""Desktop process facts consume actual SDK observations, without a real Run."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest

from dispatcher_sdk.execution_kernel import Kernel
from dispatcher_sdk.observability import ActivityRecorder, ObservationIdentity, inspect_execution
from dispatcher_sdk.orchestrator import Orchestrator

from modport.application_state_storage import hydrate_run_snapshot
from modport.contracts import OperationResult
from modport.desktop_state import DesktopState, publish_observation_binding, publish_snapshot
from modport.evidence import atomic_json
from modport.kernel_runtime import sdk_handlers
from modport.models import Budget, MigrationRequest
from modport.operations import MigrationOperations
from modport.workflow import WORKFLOW_VERSION, compile_migration_workflow


class LivenessAssignment:
    __execution_kernel_revision__ = 'desktop-sdk-liveness-fixture'

    def __call__(self, operation):
        return OperationResult('completed', operation.run_id, operation.task_id,
                               operation.stage_id, operation.command_id)


@unittest.skipUnless(os.name == 'posix' and Path('/proc/self/stat').exists(),
                     'Linux process birth identity required')
class DesktopSdkLivenessTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.store = DesktopState(temporary.name)
        self.run_id = 'desktop-' + 'c' * 32
        self.root = self.store.run_dir(self.run_id)
        self.root.mkdir()
        self.runtime = Kernel.open_sqlite(self.root / 'kernel.sqlite3',
            sdk_handlers({'modport.migration_plan': LivenessAssignment()}), isolation_mode='thread')
        self.addCleanup(self.runtime.close)
        self.sdk = Orchestrator(self.root / 'orchestrator.sqlite3',
                                self.runtime.kernel, runtime=self.runtime)
        self.addCleanup(self.sdk.close)
        request = MigrationRequest('desktop-liveness', 'https://example.invalid/mod',
            '1.20.1', '26.1.2', budget=Budget(max_seconds=600, max_agent_assignments=10))
        self.header = {'run_id': self.run_id, 'run_dir': str(self.root),
            'started_at': time.time(), 'deadline_epoch': time.time() + 600,
            'request': request.to_dict(), 'definition': compile_migration_workflow(request).to_dict(),
            'registry_revision': self.runtime.registry_revision,
            'initial_refs': {}, 'prior_findings': [], 'rubric_sha256': 'host-fixture-provenance'}
        self.assertEqual(WORKFLOW_VERSION, self.header['definition']['workflow_version'])
        atomic_json(self.root / 'run.json', self.header)
        self.store.register(self.run_id, 'Actual SDK liveness fixture')
        self.sdk.create_run(self.run_id, command_id='create', input=self.header,
                            definition=self.header['definition'])
        owner = MigrationOperations()
        app = owner._new_application()
        snapshot = self.sdk.get_run(self.run_id)
        self.task_id = 'desktop.liveness'
        operations = owner._schedule(snapshot, self.header, app, 'migration_plan',
                                     task_id=self.task_id, dependencies=[])
        self.sdk.apply_operations(self.run_id, command_id='schedule',
            expected_revision=snapshot['revision'], operations=operations, application_state=app)
        self.sdk.flush()
        self.execution_id = next(item['command']['execution_id'] for item in operations
                                 if item['kind'] == 'add_task')
        self.lease = self.runtime.kernel.claim_and_start('desktop-liveness-fixture',
            execution_id=self.execution_id, registry_revision=self.runtime.registry_revision,
            lease_seconds=600)
        self.assertIsNotNone(self.lease)
        self.sdk.sync()
        publish_snapshot(self.header,
                         hydrate_run_snapshot(self.root, self.sdk.get_run(self.run_id)), force=True)
        self.binding = self.runtime.observation_storage
        self.assertTrue(self.binding['available'], self.binding['unknown_reason'])
        publish_observation_binding(self.header, self.runtime)

    def start_observed_worker(self, *, fence=None):
        worker = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'],
                                  stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                  stderr=subprocess.DEVNULL)

        def stop_worker():
            if worker.poll() is None:
                worker.kill()
            worker.wait(timeout=5)

        self.addCleanup(stop_worker)
        identity = ObservationIdentity(self.execution_id, self.lease.attempt,
            self.lease.fence if fence is None else fence, run_id=self.run_id,
            task_id=self.task_id, generation=0, task_attempt=0)
        recorder = ActivityRecorder(self.runtime.observation_journal, identity,
                                    source_id='desktop-liveness-fixture', start=False, bind_current=True)
        self.addCleanup(recorder.close)
        self.assertEqual('persisted', recorder.flush()['state'])
        observed = recorder.observe_process(worker, role='worker', process_id='worker')
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            report = inspect_execution(self.binding['path'], self.execution_id,
                kernel_path=self.binding['kernel_path'], source_id=self.binding['source_id'])
            if any(item['process_id'] == 'worker' and item['state'] == 'alive'
                   for item in report.get('processes', [])):
                self.assertEqual(worker.pid, observed.snapshot()['pid'])
                return worker, observed
            time.sleep(.01)
        self.fail('SDK did not persist the registered live subprocess: ' + repr(report))

    def status_item(self):
        status = self.store.status(self.run_id)
        item = next(item for group in status['stages'].values() for item in group['items']
                    if item['id'] == self.task_id)
        return status, item

    def test_real_sdk_worker_is_live_then_exited_while_execution_stays_running(self):
        worker, _ = self.start_observed_worker()
        status, item = self.status_item()
        self.assertEqual('running', item['sdk_state'])
        self.assertEqual('alive', item['worker_process']['state'], repr(item))
        self.assertEqual(1, item['active_agents'])
        self.assertEqual(worker.pid, item['worker_process']['identity']['pid'])
        self.assertEqual('running', status['execution_health']['state'])
        worker.terminate()
        worker.wait(timeout=5)
        status, item = self.status_item()
        self.assertEqual('running', self.runtime.kernel.get(self.execution_id).state)
        self.assertEqual('running', item['sdk_state'])
        self.assertEqual('exited', item['worker_process']['state'], repr(item))
        self.assertEqual(0, item['active_agents'])
        self.assertEqual('interrupted', status['execution_health']['state'])
        self.assertEqual('unverified', status['acceptance_status'])

    def test_real_sdk_old_fence_does_not_prove_current_worker_live(self):
        self.assertGreater(self.lease.fence, 0)
        self.start_observed_worker(fence=self.lease.fence - 1)
        status, item = self.status_item()
        self.assertEqual('running', item['sdk_state'])
        self.assertIsNone(item['active_agents'])
        self.assertEqual('worker_observation_identity_unverified', item['worker_process']['reason'])
        self.assertEqual('unknown', status['execution_health']['state'])


if __name__ == '__main__':
    unittest.main()
