"""Current SDK cancellation recovers an interrupted progress supervisor safely."""
import json
from pathlib import Path
import tempfile
import time
import unittest

from dispatcher_sdk.orchestrator import Orchestrator, OrchestratorHost
from modport.contracts import OperationInput
from modport.evidence import atomic_json
from modport.kernel_runtime import open_runtime
from modport.models import Budget, MigrationRequest
from modport.operations import MigrationOperations
from modport.progress_supervisor import invoke_progress_supervisor
from modport.workflow import compile_migration_workflow, WORKFLOW_VERSION


class InterruptedProgressSupervisor:
    __execution_kernel_revision__ = 'interrupted-progress-supervisor-v1'

    def __call__(self, command):
        def stage_handler(prompt):
            def execute(operation):
                root = Path(operation.run_dir)
                report = (root / 'artifacts' / 'executions' / operation.command_id
                          / 'dialogue' / 'final-report.txt')
                report.parent.mkdir(parents=True, exist_ok=True)
                request = operation.payload['progress_supervision']
                report.write_text(json.dumps({**{key: request[key] for key in (
                    'review_id', 'target_task_id', 'target_execution_id')},
                    'decision': 'terminate', 'reason': 'Partial response before cancellation.'}))
                (root / 'progress-supervisor-started').write_text(operation.command_id)
                while True:
                    time.sleep(0.05)
            return execute
        return invoke_progress_supervisor(command, stage_handler)


class ProgressSupervisorRecoverySDKTests(unittest.TestCase):
    def test_user_cancelled_progress_supervisor_recovers_without_goal_manifest(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            handlers = {'modport.supervisor': InterruptedProgressSupervisor()}
            owner = MigrationOperations(handlers=handlers, isolation_mode='process')
            request = MigrationRequest('supervisor-recovery', 'https://example.invalid/probe.git',
                '1.20.1', '1.21.1', workflow_mode='artifact_verification',
                budget=Budget(max_seconds=1200, max_agent_assignments=8, max_rework_rounds=0))
            definition = compile_migration_workflow(request).to_dict()
            self.assertEqual(WORKFLOW_VERSION, definition['workflow_version'])
            now = time.time()
            with open_runtime(root, handlers=handlers, isolation_mode='process',
                              now=owner.clock, memory_policy=owner.memory_policy) as runtime:
                sdk = Orchestrator(root / 'orchestrator.sqlite3', runtime.kernel, runtime=runtime)
                host = OrchestratorHost(sdk)
                try:
                    header = {'format_version': 2, 'run_id': 'supervisor-recovery',
                        'run_dir': str(root), 'request': request.to_dict(), 'definition': definition,
                        'registry_revision': runtime.registry_revision, 'prior_findings': [],
                        'initial_refs': {}, 'rubric_sha256': 'host-provided',
                        'started_at': now, 'deadline_epoch': now + 1200}
                    atomic_json(root / 'run.json', header)
                    sdk.create_run(header['run_id'], command_id='create', input=header, definition=definition)
                    operation = OperationInput(header['run_id'], 'progress.review.1', 'supervisor',
                        'active-progress-supervisor', str(root), payload={'progress_supervision': {
                            'review_id': 'review.1', 'target_task_id': 'coder.api',
                            'target_execution_id': 'target-author', 'observation': {},
                            'idle_windows': 3, 'interval_seconds': 600}},
                        options={'workflow_version': WORKFLOW_VERSION, 'agent_assignment': 1,
                            'deadline_epoch': header['deadline_epoch'],
                            'workspace': 'workspaces/progress-supervision/review.1',
                            'progress_supervision_policy': definition['progress_supervision_policy']})
                    command = runtime.command('modport.supervisor', execution_id=operation.command_id,
                        idempotency_key=operation.command_id, correlation_id=operation.run_id,
                        timeout_seconds=1200, payload=operation.to_dict())
                    state = sdk.get_run(header['run_id'])
                    sdk.apply_operations(header['run_id'], command_id='start',
                        expected_revision=state['revision'], expected_generation=state['generation'],
                        operations=[{'kind': 'add_task', 'task_id': operation.task_id,
                                     'command': command.to_dict()},
                                    {'kind': 'dispatch', 'task_id': operation.task_id}],
                        application_state=owner._new_application())
                    host.start()
                    bound = time.monotonic() + 15
                    while not (root / 'progress-supervisor-started').exists() and time.monotonic() < bound:
                        time.sleep(0.02)
                    self.assertTrue((root / 'progress-supervisor-started').exists(),
                                    'SDK did not enter progress supervisor handler')
                    cancelled = owner.cancel(root, header['run_id'])
                    self.assertEqual('waiting', cancelled.status)
                    report = sdk.inspect_cancellation(header['run_id'], execution_id=operation.command_id)
                    entry, = report.executions
                    self.assertEqual('confirmed', entry.command_delivered.status)
                    self.assertEqual('confirmed', entry.execution_authority_revoked.status)
                    self.assertEqual('confirmed', entry.local_process_tree_reaped.status)
                    self.assertEqual('confirmed', entry.cleanup.status)
                finally:
                    host.stop(timeout=5)
                    sdk.close()
            recovered = owner.recover(root, header['run_id'])
            self.assertEqual('cancelled', recovered.status)
            receipt = json.loads((root / 'artifacts' / 'executions' / operation.command_id
                                  / 'receipt.json').read_text())
            response = receipt['response']
            self.assertEqual('failed', response['status'])
            self.assertEqual('supervisor_interrupted', response['error_code'])
            self.assertIsNone(response['outputs']['progress_supervisor_decision'])
            self.assertEqual('unknown', response['outputs']['external_outcome'])
            self.assertEqual('unverified', response['outputs']['acceptance_status'])
            self.assertNotIn('supervised_goal_revisions', response['outputs'])
            refs = response['outputs']['artifact_refs']
            partial = json.loads((root / refs['progress_supervision_interrupted_report']['path']).read_text())
            self.assertEqual('terminate', partial['decision'])
            frozen = json.loads((root / refs['progress_supervision_interrupted_request']['path']).read_text())
            self.assertEqual(operation.payload['progress_supervision'], frozen['progress_supervision'])
            self.assertFalse((root / 'artifacts' / 'supervised-goals').exists())
            self.assertEqual(header['deadline_epoch'], operation.options['deadline_epoch'])


if __name__ == '__main__':
    unittest.main()
