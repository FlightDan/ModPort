"""A reopened Runtime consumes real orphan cleanup without inventing SDK proof."""
from dataclasses import dataclass
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from dispatcher_sdk.execution_kernel import Kernel
from dispatcher_sdk.orchestrator import Orchestrator

from modport.application_state_storage import hydrate_run_snapshot
from modport.contracts import OperationInput, OperationResult, json_copy
from modport.evidence import atomic_json, read_json
from modport.interrupted_execution import reconcile_interrupted_executions
from modport.kernel_runtime import SDKHandler
from modport.models import Budget, MigrationRequest
from modport.operations import MigrationOperations
from modport.watchdog_events import collect_runtime_notifications
from modport.watchdog_routing import decide
from modport.watchdog_settlement import _evidence, _receipt, settle
from modport.workflow import compile_migration_workflow, WORKFLOW_VERSION


@dataclass
class CrashAuthor:
    __execution_kernel_revision__ = 'watchdog-orphan-author-fixture'

    def __call__(self, operation):
        if operation.payload.get('watchdog_recovery'):
            return OperationResult('completed', operation.run_id, operation.task_id,
                operation.stage_id, operation.command_id,
                outputs={'consumed_repair': operation.payload['watchdog_recovery']})
        os._exit(77)


@dataclass
class RepairSupervisor:
    __execution_kernel_revision__ = 'watchdog-orphan-supervisor-fixture'

    def __call__(self, operation):
        incident = operation.payload['watchdog_incident']
        return OperationResult('completed', operation.run_id, operation.task_id,
            operation.stage_id, operation.command_id, outputs={'watchdog_decision': {
                'incident_id': incident['incident_id'], 'action': 'repair_resume',
                'reason': 'The original author crashed and its SDK supervisor reaped the worker tree',
                'instruction': 'Resume this isolated fixture with the corrected dependency',
                'wait_for': [], 'stop_category': None}})


def handlers():
    return {'modport.behavior_extract': CrashAuthor(), 'modport.supervisor': RepairSupervisor()}


def fixture_runtime(root):
    # Explicit fixture bindings stay stable while other agents edit production
    # files. The real ModPort adapter still owns the stage Effect and receipt.
    registry = {name: SDKHandler(handler, handler.__execution_kernel_revision__, enforce_memory=False)
                for name, handler in handlers().items()}
    return Kernel.open_sqlite(root / 'kernel.sqlite3', registry, isolation_mode='process',
        lease_seconds=3600, cancellation_journal_path=str(root / 'cancellation.sqlite3'),
        source_id='orphan-fixture-host')


def crash_driver(root):
    root = Path(root)
    (root / 'baseline/.modport').mkdir(parents=True)
    owner = MigrationOperations(handlers=handlers(), isolation_mode='process')
    request = MigrationRequest('orphan-fixture', 'https://example.invalid/mod', '1.20.1', '26.1.2',
        workflow_mode='artifact_verification', budget=Budget(max_seconds=600, max_agent_assignments=8))
    with fixture_runtime(root) as runtime:
        sdk = Orchestrator(root / 'orchestrator.sqlite3', runtime.kernel, runtime=runtime)
        header = {'run_id': 'orphan-fixture', 'run_dir': str(root), 'started_at': time.time(),
            'deadline_epoch': time.time() + 600, 'request': request.to_dict(),
            'definition': compile_migration_workflow(request).to_dict(),
            'registry_revision': runtime.registry_revision, 'initial_refs': {}, 'prior_findings': [],
            'rubric_sha256': 'host-fixture-provenance', 'watchdog_policy': {'enabled': True}}
        atomic_json(root / 'run.json', header)
        sdk.create_run(header['run_id'], command_id='create', input=header, definition=header['definition'])
        snapshot = sdk.get_run(header['run_id'])
        app = owner._new_application()
        operations = owner._schedule(snapshot, header, app, 'behavior_extract', dependencies=[])
        sdk.apply_operations(header['run_id'], command_id='dispatch', expected_revision=snapshot['revision'],
                             operations=operations, application_state=app)
        sdk.flush()
        # Stop the actual old driver after its real SDK process supervisor has
        # persisted cleanup, before the stage Effect/result can be settled.
        with patch.object(runtime.kernel, 'effect_ids_for_attempt', side_effect=lambda *a, **kw: os._exit(83)):
            runtime.run_once()
    raise AssertionError('driver crash injection was not consumed')


@unittest.skipUnless(os.name == 'posix' and Path('/proc/self/stat').exists(),
                     'Linux SDK driver birth observations required')
class WatchdogInterruptedSettlementTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        program = ('import sys; sys.path.insert(0, sys.argv[1]); '
                   'from test_watchdog_interrupted_settlement import crash_driver; crash_driver(sys.argv[2])')
        crashed = subprocess.run([sys.executable, '-c', program, str(Path(__file__).resolve().parent),
                                  str(self.root)], capture_output=True, timeout=20)
        self.assertEqual(83, crashed.returncode, crashed.stderr.decode())
        self.header = read_json(self.root / 'run.json')
        self.assertEqual(WORKFLOW_VERSION, self.header['definition']['workflow_version'])
        self.owner = MigrationOperations(handlers=handlers(), isolation_mode='process')
        self.runtime = self.enterContext(fixture_runtime(self.root))
        self.sdk = Orchestrator(self.root / 'orchestrator.sqlite3', self.runtime.kernel, runtime=self.runtime)
        self.addCleanup(self.sdk.close)
        self.sequence = 0
        self.runtime.reap()
        self.sdk.sync()
        report = reconcile_interrupted_executions(self.root, self.header, self.runtime, self.sdk)
        self.assertEqual(1, report['changed'], report)
        self.assertTrue(collect_runtime_notifications(self.sdk, self.header)['completed'])
        state = self.read()
        app = state['application_state']
        actions, pending = decide(self.owner, state, self.header, app, self.sdk)
        self.assertTrue(pending)
        self.apply(state, actions, app)
        self.assertIsNotNone(self.runtime.run_once(), repr([
            {key: row[key] for key in ('execution_id', 'state', 'last_error')}
            for row in self.sdk.delivery_messages()]))
        state = self.read()
        episode = state['application_state']['watchdog']['episodes'][
            state['application_state']['watchdog']['active']]
        self.supervisor_id = episode['supervisor_task_id']
        self.assertEqual('succeeded', state['tasks'][self.supervisor_id]['attempts'][-1]['state'])
        app = state['application_state']
        actions, pending = decide(self.owner, state, self.header, app, self.sdk)
        self.assertTrue(pending)
        self.assertEqual(['behavior_extract'], [row['task_id'] for row in actions if row['kind'] == 'cancel'])
        self.apply(state, actions, app)
        self.state = self.read()
        self.recovery = self.sdk.inspect_recoveries(self.header['run_id'])[0]
        self.operation = OperationInput.from_dict(self.recovery.execution.command.payload)
        self.execution_id = self.operation.command_id

    def read(self):
        self.sdk.sync()
        return hydrate_run_snapshot(self.root, self.sdk.get_run(self.header['run_id']))

    def apply(self, state, actions, app):
        self.sequence += 1
        self.sdk.apply_operations(self.header['run_id'], command_id='fixture-' + str(self.sequence),
            expected_revision=state['revision'], expected_generation=state['generation'],
            operations=actions, application_state=app)
        self.sdk.flush()

    def test_authorized_orphan_cleanup_settles_failed_receipt_and_dispatches_repair(self):
        before = self.sdk.inspect_cancellation(self.header['run_id'], execution_id=self.execution_id).executions[0]
        control = ('request_committed', 'command_delivered', 'execution_authority_revoked')
        self.assertEqual(['confirmed'] * 3, [getattr(before, field).status for field in control])
        self.assertEqual('unknown', before.local_process_tree_reaped.status)
        self.assertEqual('unknown', before.cleanup.status)
        self.assertTrue(settle(self.root, self.header, self.sdk, self.state))
        state = self.read()
        self.assertEqual('cancelled', state['tasks']['behavior_extract']['attempts'][-1]['state'])
        receipt = read_json(self.root / 'artifacts/executions' / self.execution_id / 'receipt.json')
        response = receipt['response']
        self.assertEqual('failed', response['status'])
        self.assertEqual('unknown', response['outputs']['external_outcome'])
        self.assertEqual('unverified', response['outputs']['acceptance_status'])
        note = read_json(self.root / response['outputs']['artifact_refs']['watchdog_cancelled_assignment']['path'])
        self.assertEqual('unknown', note['proof']['local_process_tree_reaped'])
        self.assertEqual('unknown', note['proof']['cleanup'])
        self.assertTrue(note['independent_cleanup']['confirmed'])
        after = self.sdk.inspect_cancellation(self.header['run_id'], execution_id=self.execution_id).executions[0]
        self.assertEqual('unknown', after.local_process_tree_reaped.status)
        self.assertEqual('unknown', after.cleanup.status)
        app = state['application_state']
        actions, _ = decide(self.owner, state, self.header, app, self.sdk)
        self.assertTrue(any(row['kind'] == 'new_attempt' for row in actions))
        self.apply(state, actions, app)
        self.assertIsNotNone(self.runtime.run_once())
        state = self.read()
        attempts = state['tasks']['behavior_extract']['attempts']
        self.assertEqual(2, len(attempts))
        self.assertEqual('succeeded', attempts[-1]['state'])
        repair = attempts[-1]['result']['value']['outputs']['consumed_repair']
        self.assertEqual(self.execution_id, repair['previous_execution_id'])
        self.assertEqual(3, state['application_state']['agent_assignments'])
        for task in state['tasks'].values():
            for attempt in task['attempts']:
                self.assertEqual(self.header['deadline_epoch'],
                                 attempt['command']['payload']['options']['deadline_epoch'])

    def test_live_unknown_stale_or_unauthorized_evidence_cannot_settle(self):
        def evidence(snapshot=None):
            return _evidence(self.root, self.header, self.sdk, snapshot or self.state,
                             self.recovery, self.operation)

        self.assertIsNotNone(evidence())
        for state in ('alive', 'unknown'):
            with self.subTest(driver_state=state), patch('modport.interrupted_execution._driver_state',
                return_value=(state, 'fixture_driver_' + state)):
                self.assertIsNone(evidence())
        with patch('modport.interrupted_execution._proc_mount_matches_namespace', return_value=False):
            self.assertIsNone(evidence())
        observation = self.runtime.observe(self.execution_id)
        stale = json_copy(observation)
        stale['identity']['fence'] -= 1
        with patch.object(self.runtime, 'observe', return_value=stale):
            self.assertIsNone(evidence())
        unauthorized = json_copy(self.state)
        unauthorized['tasks'][self.supervisor_id]['attempts'][-1]['result']['value']['outputs'][
            'watchdog_decision']['instruction'] = 'An unrelated instruction'
        self.assertIsNone(evidence(unauthorized))
        wrong_run = json_copy(self.state)
        wrong_run['tasks'][self.supervisor_id]['attempts'][-1]['result']['value']['run_id'] = 'another-logical-run'
        self.assertIsNone(evidence(wrong_run))
        self.assertEqual('recovery_required', self.runtime.kernel.get(self.execution_id).state)
        self.assertEqual('indeterminate', self.runtime.kernel.get_effect('modport:' + self.execution_id).state)
        self.assertFalse((self.root / 'artifacts/executions' / self.execution_id / 'receipt.json').exists())

    def test_note_only_crash_preserves_original_evidence_when_cleanup_proof_improves(self):
        original = _evidence(self.root, self.header, self.sdk, self.state,
                             self.recovery, self.operation)
        self.assertIsNotNone(original)
        note = self.root / 'artifacts/executions' / self.execution_id / 'watchdog-cancelled-assignment.json'
        atomic_json(note, original)
        improved = json_copy(original)
        improved['proof']['local_process_tree_reaped'] = 'confirmed'
        improved['proof']['cleanup'] = 'confirmed'
        improved.pop('independent_cleanup')
        # This receipt-level regression models the fresh, separately validated
        # caller evidence. It does not alter or pretend to alter SDK receipts.
        different = json_copy(improved)
        different['effect_revision'] += 1
        with self.assertRaisesRegex(ValueError, 'differs from the proved cancellation'):
            _receipt(self.root, self.recovery.execution.command.to_dict(), self.recovery.effect,
                     self.operation, different)
        response = _receipt(self.root, self.recovery.execution.command.to_dict(), self.recovery.effect,
                            self.operation, improved)
        self.assertEqual('failed', response['status'])
        self.assertEqual(original, read_json(note))
        actual = self.sdk.inspect_cancellation(self.header['run_id'], execution_id=self.execution_id).executions[0]
        self.assertEqual('unknown', actual.local_process_tree_reaped.status)
        self.assertEqual('unknown', actual.cleanup.status)


if __name__ == '__main__':
    if len(sys.argv) == 3 and sys.argv[1] == '--crash-driver':
        crash_driver(sys.argv[2])
    else:
        unittest.main()
