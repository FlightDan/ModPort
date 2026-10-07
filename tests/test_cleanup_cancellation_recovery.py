"""Current process cleanup cancellation preserves partial work without redispatch."""
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
from modport.workflow import WORKFLOW_VERSION, compile_migration_workflow


class InterruptedCleanup:
    __execution_kernel_revision__ = 'cleanup-cancellation-process-fixture-v1'

    def __call__(self, command):
        root = Path(command.run_dir)
        files = {
            root / 'worktree' / 'Partial.java': 'Unaccepted integrated candidate changes.\n',
            root / command.options['workspace'] / 'Partial.java': 'Unaccepted isolated cleanup changes.\n',
            root / 'artifacts' / 'executions' / command.command_id / 'partial-report.txt':
                'Cleanup interrupted before collecting a complete candidate.\n',
        }
        for path, contents in files.items():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(contents, encoding='utf-8')
        (root / 'cleanup-handler-started').write_text(command.command_id, encoding='utf-8')
        bound = time.monotonic() + 45
        while time.monotonic() < bound:
            time.sleep(0.05)
        raise RuntimeError('bounded fixture was not cancelled within 45 seconds')


class CleanupCancellationRecoverySDKTests(unittest.TestCase):
    def exercise_cleanup(self, stage):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            handlers = {'modport.' + stage: InterruptedCleanup()}
            owner = MigrationOperations(handlers=handlers, isolation_mode='process')
            request = MigrationRequest('cleanup-recovery', 'https://example.invalid/probe.git',
                '1.20.1', '1.21.1', budget=Budget(max_seconds=1200,
                    max_agent_assignments=8, max_rework_rounds=0))
            definition = compile_migration_workflow(request).to_dict()
            self.assertEqual(WORKFLOW_VERSION, definition['workflow_version'])
            self.assertIn(stage, {row['stage_id'] for row in definition['stages']})
            now = time.time()
            run_id = stage + '-cancellation-recovery'
            with open_runtime(root, handlers=handlers, isolation_mode='process',
                              now=owner.clock, memory_policy=owner.memory_policy) as runtime:
                sdk = Orchestrator(root / 'orchestrator.sqlite3', runtime.kernel, runtime=runtime)
                host = OrchestratorHost(sdk)
                try:
                    header = {'format_version': 2, 'run_id': run_id,
                        'run_dir': str(root), 'request': request.to_dict(), 'definition': definition,
                        'registry_revision': runtime.registry_revision, 'prior_findings': [],
                        'initial_refs': {}, 'rubric_sha256': 'host-provided',
                        'started_at': now, 'deadline_epoch': now + 1200}
                    atomic_json(root / 'run.json', header)
                    sdk.create_run(run_id, command_id='create', input=header, definition=definition)
                    operation = OperationInput(run_id, stage, stage, 'interrupted-' + stage,
                        str(root), options={'workflow_version': WORKFLOW_VERSION,
                            'agent_assignment': 1, 'deadline_epoch': header['deadline_epoch'],
                            'workspace': 'workspaces/code-cleanup/' + stage})
                    command = runtime.command('modport.' + stage, execution_id=operation.command_id,
                        idempotency_key=operation.command_id, correlation_id=run_id,
                        timeout_seconds=1200, payload=operation.to_dict())
                    app = owner._new_application()
                    app.update(active_stage=stage, agent_assignments=1)
                    state = sdk.get_run(run_id)
                    sdk.apply_operations(run_id, command_id='start',
                        expected_revision=state['revision'], expected_generation=state['generation'],
                        operations=[{'kind': 'add_task', 'task_id': stage, 'command': command.to_dict()},
                                    {'kind': 'dispatch', 'task_id': stage}], application_state=app)
                    host.start()
                    started = root / 'cleanup-handler-started'
                    bound = time.monotonic() + 15
                    while not started.exists() and time.monotonic() < bound:
                        time.sleep(0.02)
                    self.assertTrue(started.is_file(), 'SDK did not enter the cleanup process')
                    self.assertEqual(operation.command_id, started.read_text(encoding='utf-8'))
                    preserved = {
                        path: path.read_bytes() for path in (
                            root / 'worktree' / 'Partial.java',
                            root / operation.options['workspace'] / 'Partial.java',
                            root / 'artifacts' / 'executions' / operation.command_id / 'partial-report.txt')}
                    cancelled = owner.cancel(root, run_id)
                    self.assertEqual('waiting', cancelled.status)
                    proof_fields = ('request_committed', 'command_delivered',
                                    'execution_authority_revoked', 'local_process_tree_reaped', 'cleanup')
                    bound = time.monotonic() + 15
                    while True:
                        report = sdk.inspect_cancellation(run_id, execution_id=operation.command_id)
                        entries = report.executions
                        if (not report.truncated and len(entries) == 1
                                and all(getattr(entries[0], field).status == 'confirmed'
                                        for field in proof_fields)):
                            break
                        if time.monotonic() >= bound:
                            self.fail('SDK did not confirm all five cancellation and cleanup facts')
                        time.sleep(0.02)
                    entry = entries[0]
                    self.assertEqual('recovery_required', entry.execution_state)
                    self.assertEqual([], list(entry.issues))
                    self.assertFalse(entry.effects_truncated)
                    self.assertFalse(entry.receipts_truncated)
                    recovery, = sdk.inspect_recoveries(run_id)
                    self.assertEqual(stage, recovery.task_id)
                    original_effect_request = dict(recovery.effect.request)
                    original_input = recovery.execution.command.to_dict()
                    receipt_path = (root / 'artifacts' / 'executions'
                                    / operation.command_id / 'receipt.json')
                    self.assertFalse(receipt_path.exists())
                finally:
                    host.stop(timeout=5)
                    sdk.close()
            recovered = owner.recover(root, run_id)
            self.assertEqual('cancelled', recovered.status)
            receipt = json.loads(receipt_path.read_text(encoding='utf-8'))
            self.assertEqual(original_effect_request, receipt['effect_request'])
            response = receipt['response']
            self.assertEqual('failed', response['status'])
            self.assertEqual(stage + '_interrupted', response['error_code'])
            outputs = response['outputs']
            self.assertTrue(outputs['agent_cancelled'])
            self.assertTrue(outputs['partial_outputs_unaccepted'])
            self.assertEqual('unknown', outputs['external_outcome'])
            self.assertFalse(outputs['artifacts_complete'])
            self.assertEqual('unverified', outputs['acceptance_status'])
            partial_ref = outputs['artifact_refs']['interrupted_agent']
            note = json.loads((root / partial_ref['path']).read_text(encoding='utf-8'))
            self.assertEqual({field: 'confirmed' for field in proof_fields}, note['proof'])
            partial = note['partial_outputs']
            self.assertIn('unaccepted', partial['disposition'])
            self.assertIn('unknown', partial['disposition'])
            self.assertEqual('untrusted_directory', partial['workspace']['state'])
            self.assertEqual('untrusted_directory', partial['cleanup_workspaces']['state'])
            self.assertEqual('untrusted_directory', partial['execution_artifacts']['state'])
            for path, contents in preserved.items():
                self.assertEqual(contents, path.read_bytes(), 'recovery modified partial cleanup evidence')
            state = recovered.snapshot
            self.assertEqual({stage}, set(state['tasks']))
            self.assertEqual(1, len(state['tasks'][stage]['attempts']))
            self.assertEqual(original_input, state['tasks'][stage]['attempts'][0]['command'])
            self.assertEqual(1, state['application_state']['agent_assignments'])
            self.assertEqual(header['deadline_epoch'], state['input']['deadline_epoch'])
            self.assertFalse((root / 'artifacts' / 'final-cleanup' / 'checkpoint.json').exists())

    def test_code_cleanup_process_cancellation_recovers_without_receipt(self):
        self.exercise_cleanup('code_cleanup')

    def test_final_cleanup_process_cancellation_recovers_without_receipt(self):
        self.exercise_cleanup('final_cleanup')


if __name__ == '__main__':
    unittest.main()
