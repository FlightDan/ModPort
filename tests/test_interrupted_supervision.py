"""Actual stopped watchdog worker/driver recovery through current SDK control."""
import json
from dataclasses import replace
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from dispatcher_sdk.orchestrator import Orchestrator
from dispatcher_sdk.execution_kernel import Kernel
from modport.application_state_storage import hydrate_run_snapshot
from modport.contracts import OperationResult, json_copy
from modport.evidence import atomic_json
from modport.interrupted_execution import reconcile_interrupted_executions
from modport.interrupted_supervision import request_cancellation, settle
from modport.kernel_runtime import open_runtime, sdk_handlers
from modport.models import Budget, MigrationRequest
from modport.operations import MigrationOperations
from modport.watchdog_routing import begin, decide
from modport.workflow import WORKFLOW_VERSION, compile_migration_workflow


class InterruptedSupervisor:
    __execution_kernel_revision__ = 'actual-interrupted-watchdog-supervisor'

    def __call__(self, operation):
        root = Path(operation.run_dir)
        (root / ('diagnosis-attempt-' + str(operation.attempt))).write_text(operation.command_id)
        if operation.attempt == 1:
            os._exit(77)
        return OperationResult('failed', operation.run_id, operation.task_id, operation.stage_id,
            operation.command_id, detail='Fresh fixture diagnosis returned a raw failure; no business decision',
            error_code='fixture_diagnosis_failed')


def crash_supervisor_driver(root, watchdog_enabled=True):
    root = Path(root)
    handlers = {'modport.supervisor': InterruptedSupervisor()}
    owner = MigrationOperations(handlers=handlers, isolation_mode='process')
    request = MigrationRequest('interrupted-supervisor', 'https://example.invalid/mod', '1.20.1', '26.1.2',
        validation_scope='compile_package', budget=Budget(max_seconds=3000, max_agent_assignments=8))
    definition = compile_migration_workflow(request).to_dict()
    with open_runtime(root, handlers=handlers, isolation_mode='process', now=owner.clock,
                      memory_policy=owner.memory_policy) as runtime:
        sdk = Orchestrator(root / 'orchestrator.sqlite3', runtime.kernel, runtime=runtime)
        now = time.time()
        header = {'format_version': 2, 'run_id': 'physical-segment', 'logical_run_id': 'logical-migration',
            'run_dir': str(root), 'request': request.to_dict(), 'definition': definition,
            'registry_revision': runtime.registry_revision, 'prior_findings': [], 'initial_refs': {},
            'rubric_sha256': 'host-provenance', 'started_at': now, 'deadline_epoch': now + 3000,
            'watchdog_policy': {'enabled': watchdog_enabled, 'inactivity_seconds': 600}}
        atomic_json(root / 'run.json', header)
        state = sdk.create_run(header['run_id'], command_id='create', input=header, definition=definition)
        app = owner._new_application()
        operations = begin(owner, state, header, app, {'incident_id': 'lost-driver', 'kind': 'driver_lost',
            'reason': 'A watchdog diagnostic fixture investigates a prior driver loss',
            'target_task_id': None, 'target_execution_id': None})
        sdk.apply_operations(header['run_id'], command_id='diagnose', expected_revision=state['revision'],
                             operations=operations, application_state=app)
        sdk.flush()
        # The real process supervisor has reaped the crashed worker and written
        # its durable cleanup phase when the normal settlement lookup occurs.
        # Lose the driver exactly there, before the SDK can park the stage Effect.
        with patch.object(runtime.kernel, 'effect_ids_for_attempt',
                          side_effect=lambda *args, **options: os._exit(83)):
            runtime.run_once()
        raise AssertionError('bounded driver crash injection was not consumed')


@unittest.skipUnless(os.name == 'posix' and Path('/proc/self/stat').exists(),
                     'exact Linux SDK process-birth observations required')
class InterruptedSupervisionTests(unittest.TestCase):
    def fixture(self, *, watchdog_enabled=True):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        child = subprocess.run([sys.executable, '-c',
            'from tests.test_interrupted_supervision import crash_supervisor_driver; '
            'import sys; crash_supervisor_driver(sys.argv[1], sys.argv[2] == "True")',
            str(root), str(watchdog_enabled)],
            cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, timeout=20)
        self.assertEqual(83, child.returncode, child.stdout + child.stderr)
        header = json.loads((root / 'run.json').read_text())
        self.assertEqual(WORKFLOW_VERSION, header['definition']['workflow_version'])
        owner = MigrationOperations(handlers={'modport.supervisor': InterruptedSupervisor()}, isolation_mode='process')
        runtime = open_runtime(root, handlers=owner.handlers, isolation_mode='process', now=owner.clock,
                               memory_policy=owner.memory_policy)
        runtime = runtime.__enter__()
        self.addCleanup(runtime.close)
        sdk = Orchestrator(root / 'orchestrator.sqlite3', runtime.kernel, runtime=runtime)
        self.addCleanup(sdk.close)
        runtime.reap()
        sdk.sync()
        report = reconcile_interrupted_executions(root, header, runtime, sdk)
        self.assertEqual(1, report['changed'], report)
        self.assertEqual('recovery_required', report['executions'][0]['status'], report)
        return root, header, owner, runtime, sdk

    def test_actual_crashed_watchdog_cancel_failed_receipt_tick_and_fresh_diagnosis(self):
        root, header, owner, runtime, sdk = self.fixture()

        def read():
            sdk.sync()
            return hydrate_run_snapshot(root, sdk.get_run(header['run_id']))

        state = read()
        app = json_copy(state['application_state'])
        incident = app['watchdog']['active']
        episode = app['watchdog']['episodes'][incident]
        task_id = episode['supervisor_task_id']
        original = state['tasks'][task_id]['attempts'][-1]
        execution_id = original['command']['execution_id']
        actions, pending = decide(owner, state, header, app, sdk)
        self.assertTrue(pending)
        self.assertEqual([task_id], [action['task_id'] for action in actions if action['kind'] == 'cancel'])
        self.assertNotIn('decision', episode)
        self.assertEqual([], request_cancellation(owner, state, header, app, sdk, episode))
        sdk.apply_operations(header['run_id'], command_id='mechanical-cancel', expected_revision=state['revision'],
            operations=actions, application_state=app)
        # Cancellation authority must be delivered and revoked; persistence alone
        # cannot settle the diagnostic Effect even with independently dead workers.
        self.assertFalse(settle(root, header, sdk, read()))
        sdk.flush()
        state = read()
        controls = sdk.inspect_cancellation(header['run_id'], execution_id=execution_id).executions[0]
        for name in ('request_committed', 'command_delivered', 'execution_authority_revoked'):
            self.assertEqual('confirmed', getattr(controls, name).status)
        original_cleanup = {name: getattr(controls, name).status for name in ('local_process_tree_reaped', 'cleanup')}
        state = owner.tick(sdk, header)
        self.assertEqual('cancelled', state['tasks'][task_id]['attempts'][0]['state'])
        receipt = json.loads((root / 'artifacts/executions' / execution_id / 'receipt.json').read_text())
        self.assertEqual('failed', receipt['response']['status'])
        self.assertEqual('watchdog_diagnosis_interrupted', receipt['response']['error_code'])
        self.assertNotIn('watchdog_decision', receipt['response']['outputs'])
        self.assertTrue(receipt['response']['outputs']['partial_outputs_unaccepted'])
        self.assertEqual('unverified', receipt['response']['outputs']['acceptance_status'])
        note = json.loads((root / 'artifacts/executions' / execution_id / 'interrupted-watchdog-diagnosis.json').read_text())
        self.assertEqual(original_cleanup, {name: note['proof'][name] for name in original_cleanup})
        self.assertTrue(note['stopped_execution']['confirmed'])
        self.assertEqual(1, state['application_state']['agent_assignments'])
        self.assertEqual([task_id], list(state['tasks']))
        episode = state['application_state']['watchdog']['episodes'][incident]
        self.assertNotIn('decision', episode)
        retry_at = episode['retry_at']
        self.assertGreaterEqual(retry_at, owner.clock() + 59)
        self.assertFalse(settle(root, header, sdk, state))
        # Consume the original retry boundary; the new assignment is the same
        # diagnostic task under the original deadline/cumulative host budget.
        owner.clock = lambda: retry_at + .01
        state = owner.tick(sdk, header)
        self.assertEqual(2, len(state['tasks'][task_id]['attempts']))
        self.assertEqual(2, state['application_state']['agent_assignments'])
        self.assertEqual(header['deadline_epoch'], state['input']['deadline_epoch'])
        bound = time.monotonic() + 5
        while state['tasks'][task_id]['attempts'][-1]['state'] == 'pending_dispatch' and time.monotonic() < bound:
            sdk.flush()
            state = read()
            if state['tasks'][task_id]['attempts'][-1]['state'] == 'pending_dispatch':
                time.sleep(.02)
        runtime.run_once()
        state = read()
        self.assertEqual('succeeded', state['tasks'][task_id]['attempts'][-1]['state'],
                         sdk.delivery_messages(pending_only=True))
        self.assertTrue((root / 'diagnosis-attempt-2').exists())
        fresh = state['tasks'][task_id]['attempts'][-1]['result']['value']
        self.assertEqual('failed', fresh['status'])
        self.assertEqual('fixture_diagnosis_failed', fresh['error_code'])
        self.assertNotIn('watchdog_decision', fresh['outputs'])
        self.assertEqual([task_id], list(state['tasks']))

    def test_paused_watchdog_stopped_diagnosis_uses_current_failure_policy(self):
        root, header, owner, runtime, sdk = self.fixture(watchdog_enabled=False)
        self.assertFalse(header['watchdog_policy']['enabled'])
        self.assertEqual('supervisor_first', header['definition']['failure_supervision_policy']['mode'])

        def read():
            sdk.sync()
            return hydrate_run_snapshot(root, sdk.get_run(header['run_id']))

        state = read()
        app = json_copy(state['application_state'])
        incident = app['watchdog']['active']
        episode = app['watchdog']['episodes'][incident]
        task_id = episode['supervisor_task_id']
        execution_id = state['tasks'][task_id]['attempts'][-1]['command']['execution_id']
        wrong = {**episode, 'request': {**episode['request'], 'incident_id': 'stale-incident'}}
        self.assertEqual([], request_cancellation(owner, state, header, app, sdk, wrong))
        actions, pending = decide(owner, state, header, app, sdk)
        self.assertTrue(pending)
        self.assertEqual([task_id], [row['task_id'] for row in actions if row['kind'] == 'cancel'])
        proof = episode['supervisor_interruptions'][execution_id]['stopped_execution']
        self.assertTrue(proof['confirmed'])
        self.assertEqual(execution_id, proof['execution_id'])
        self.assertNotIn('decision', episode)
        sdk.apply_operations(header['run_id'], command_id='paused-mechanical-cancel',
            expected_revision=state['revision'], operations=actions, application_state=app)
        self.assertFalse(settle(root, header, sdk, read()))
        sdk.flush()
        report = sdk.inspect_cancellation(header['run_id'], execution_id=execution_id)
        controls = report.executions[0]
        for name in ('request_committed', 'command_delivered', 'execution_authority_revoked'):
            self.assertEqual('confirmed', getattr(controls, name).status)
        with patch.object(sdk, 'inspect_cancellation', return_value=replace(report, truncated=True)):
            self.assertFalse(settle(root, header, sdk, read()))
        self.assertFalse((root / 'artifacts/executions' / execution_id / 'receipt.json').exists())
        state = owner.tick(sdk, header)
        self.assertEqual('cancelled', state['tasks'][task_id]['attempts'][0]['state'])
        receipt = json.loads((root / 'artifacts/executions' / execution_id / 'receipt.json').read_text())
        self.assertEqual('watchdog_diagnosis_interrupted', receipt['response']['error_code'])
        self.assertNotIn('watchdog_decision', receipt['response']['outputs'])
        self.assertEqual('unverified', receipt['response']['outputs']['acceptance_status'])
        note = json.loads((root / 'artifacts/executions' / execution_id / 'interrupted-watchdog-diagnosis.json').read_text())
        self.assertTrue(note['stopped_execution']['confirmed'])
        self.assertEqual(execution_id, note['execution_id'])
        self.assertEqual(1, state['application_state']['agent_assignments'])
        self.assertEqual([task_id], list(state['tasks']))
        episode = state['application_state']['watchdog']['episodes'][incident]
        self.assertNotIn('decision', episode)
        owner.clock = lambda: episode['retry_at'] + .01
        state = owner.tick(sdk, header)
        self.assertEqual(2, len(state['tasks'][task_id]['attempts']))
        self.assertEqual(2, state['application_state']['agent_assignments'])
        self.assertEqual(header['deadline_epoch'], state['input']['deadline_epoch'])
        # The failed mechanical report can authorize only fresh diagnosis.
        # No product task or repair decision was accepted from partial output.
        self.assertIsNone(state['tasks'][task_id]['attempts'][-1]['result'])
        self.assertNotIn('decision', state['application_state']['watchdog']['episodes'][incident])
        self.assertEqual([task_id], list(state['tasks']))
        self.assertFalse(state['input']['watchdog_policy']['enabled'])

    def test_wrong_active_episode_and_disabled_recovery_policies_do_not_cancel(self):
        root, header, owner, runtime, sdk = self.fixture()
        state = hydrate_run_snapshot(root, sdk.get_run(header['run_id']))
        app = json_copy(state['application_state'])
        episode = app['watchdog']['episodes'][app['watchdog']['active']]
        wrong = {**episode, 'request': {**episode['request'], 'incident_id': 'another-incident'}}
        self.assertEqual([], request_cancellation(owner, state, header, app, sdk, wrong))
        stopped = {**header, 'watchdog_policy': {'enabled': False},
            'definition': {**header['definition'], 'failure_supervision_policy': {'mode': 'disabled'}}}
        self.assertEqual([], request_cancellation(owner, state, stopped, app, sdk, episode))
        self.assertFalse(settle(root, stopped, sdk, state))
        self.assertEqual('recovery_required', runtime.kernel.get(
            state['tasks'][episode['supervisor_task_id']]['attempts'][-1]['command']['execution_id']).state)

    def test_healthy_running_supervisor_does_not_query_optional_observation(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        runtime = Kernel.open_sqlite(root / 'kernel.sqlite3',
            sdk_handlers({'modport.supervisor': InterruptedSupervisor()}), isolation_mode='thread')
        self.addCleanup(runtime.close)
        sdk = Orchestrator(root / 'orchestrator.sqlite3', runtime.kernel, runtime=runtime)
        self.addCleanup(sdk.close)
        owner = MigrationOperations(handlers={'modport.supervisor': InterruptedSupervisor()}, isolation_mode='thread')
        request = MigrationRequest('healthy-supervisor', 'https://example.invalid/mod', '1.20.1', '26.1.2',
            validation_scope='compile_package', budget=Budget(max_seconds=600, max_agent_assignments=8))
        header = {'run_id': 'healthy', 'run_dir': str(root), 'request': request.to_dict(),
            'definition': compile_migration_workflow(request).to_dict(),
            'registry_revision': runtime.registry_revision, 'prior_findings': [], 'initial_refs': {},
            'rubric_sha256': 'host-provenance', 'deadline_epoch': time.time() + 600,
            'watchdog_policy': {'enabled': True}}
        state = sdk.create_run('healthy', command_id='create', input=header, definition=header['definition'])
        app = owner._new_application()
        actions = begin(owner, state, header, app, {'incident_id': 'healthy-diagnosis', 'kind': 'driver_lost',
            'reason': 'Bounded healthy fixture', 'target_task_id': None, 'target_execution_id': None})
        sdk.apply_operations('healthy', command_id='diagnose', expected_revision=state['revision'],
                             operations=actions, application_state=app)
        sdk.flush()
        self.assertIsNotNone(runtime.kernel.claim_and_start('healthy-driver',
            registry_revision=runtime.registry_revision, lease_seconds=600))
        sdk.sync()
        state = hydrate_run_snapshot(root, sdk.get_run('healthy'))
        app = json_copy(state['application_state'])
        episode = app['watchdog']['episodes'][app['watchdog']['active']]
        with patch('modport.interrupted_supervision.stopped_execution_evidence',
                   side_effect=AssertionError('healthy supervisor triggered observation')) as observation, \
                patch.object(sdk, 'inspect_recoveries',
                             side_effect=AssertionError('healthy supervisor scanned SDK recovery')) as recovery:
            actions, pending = decide(owner, state, header, app, sdk)
            self.assertTrue(pending)
            self.assertEqual([], actions)
            self.assertEqual([], request_cancellation(owner, state, header, app, sdk, episode))
            self.assertFalse(settle(root, header, sdk, state))
            observation.assert_not_called()
            recovery.assert_not_called()
        self.assertNotIn('interrupted_supervisor_diagnostic', episode)


if __name__ == '__main__':
    unittest.main()
