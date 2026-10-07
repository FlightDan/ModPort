"""Isolated public SDK controls with persisted, identity-bound observations."""
import copy
from contextlib import contextmanager
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
from types import SimpleNamespace

from dispatcher_sdk.execution_kernel import Kernel
from dispatcher_sdk import ObservationOptions
from dispatcher_sdk.observability import ActivityRecorder, ObservationIdentity
from dispatcher_sdk.orchestrator import Orchestrator

from modport.interrupted_execution import reconcile_interrupted_executions, stopped_execution_evidence
from modport.application_state_storage import pack_application_state
from modport.run_monitor import pid_namespace


def handler(payload, context):
    return {'completed': True}


handler.__execution_kernel_revision__ = 'interrupted-execution-isolated-v1'


def crash_handler(payload, context):
    # A host-owned fixture crashes after claiming its external stage Effect.
    # Its actual SDK supervisor must reap the worker tree before driver death.
    def perform():
        time.sleep(.1)
        os._exit(77)
    return context.effects.execute_once('stage:crash', 'stage', {}, perform)


crash_handler.__execution_kernel_revision__ = 'interrupted-execution-process-crash-v1'


def crash_driver(root):
    root = Path(root)
    runtime = Kernel.open_sqlite(root / 'kernel.sqlite3', {'crash': crash_handler},
        isolation_mode='process', lease_seconds=3600,
        observation_options=ObservationOptions(flush_interval=.01))
    sdk = Orchestrator(root / 'orchestrator.sqlite3', runtime.kernel, runtime=runtime)
    sdk.create_run('crash', command_id='create')
    command = runtime.command('crash', execution_id='crashed-author', idempotency_key='crashed-author',
        correlation_id='crash', payload={'run_id': 'crash', 'task_id': 'author',
                                        'command_id': 'crashed-author'}, timeout_seconds=3600)
    sdk.apply_operations('crash', command_id='dispatch', expected_revision=0,
        operations=[{'kind': 'add_task', 'task_id': 'author', 'command': command.to_dict()},
                    {'kind': 'dispatch', 'task_id': 'author'}])
    sdk.flush()
    # Inject the driver loss at its public settlement effect lookup. This runs
    # after the real worker invocation and durable supervisor cleanup receipt,
    # before a result/recovery fact can settle the Kernel execution.
    with patch.object(runtime.kernel, 'effect_ids_for_attempt', side_effect=lambda *a, **kw: os._exit(83)):
        runtime.run_once()
    raise AssertionError('driver crash injection was not consumed')


def reopened_crash_driver(root):
    """Current producer/worker after a real public same-Run generation reopen."""
    from test_watchdog_interrupted_settlement import fixture_runtime, handlers
    from modport.operations import MigrationOperations
    from modport.models import Budget, MigrationRequest
    from modport.workflow import compile_migration_workflow
    from modport.evidence import atomic_json
    root = Path(root)
    (root / 'baseline/.modport').mkdir(parents=True)
    owner = MigrationOperations(handlers=handlers(), isolation_mode='process')
    request = MigrationRequest('reopened-fixture', 'https://example.invalid/mod', '1.20.1', '26.1.2',
        workflow_mode='artifact_verification', budget=Budget(max_seconds=600, max_agent_assignments=8))
    with fixture_runtime(root) as runtime:
        sdk = Orchestrator(root / 'orchestrator.sqlite3', runtime.kernel, runtime=runtime)
        header = {'run_id': 'physical-reopened', 'logical_run_id': 'logical-original',
            'run_dir': str(root), 'deadline_epoch': time.time() + 600, 'request': request.to_dict(),
            'definition': compile_migration_workflow(request).to_dict(),
            'registry_revision': runtime.registry_revision, 'initial_refs': {}, 'prior_findings': [],
            'rubric_sha256': 'host-fixture-provenance', 'watchdog_policy': {'enabled': True}}
        atomic_json(root / 'run.json', header)
        sdk.create_run(header['run_id'], command_id='create', input=header, definition=header['definition'])
        failed = sdk.apply_operations(header['run_id'], command_id='fail', expected_revision=0,
            operations=[{'kind': 'finish', 'state': 'failed'}], application_state=owner._new_application())
        app = owner._new_application()
        operations = owner._schedule(failed, header, app, 'behavior_extract', dependencies=[])
        sdk.reopen_run(header['run_id'], command_id='reopen', expected_revision=failed['revision'],
            expected_generation=0, actor='isolated-fixture', authorization_source='isolated-crash-regression',
            reason='Diagnose a failed temporary Run under its original deadline',
            target_deployment={'registry_revision': runtime.registry_revision},
            decision={'start_stage': 'behavior_extract', 'reused_artifacts': [], 'invalidated_artifacts': []},
            application_state=app, operations=operations)
        sdk.flush()
        with patch.object(runtime.kernel, 'effect_ids_for_attempt', side_effect=lambda *a, **kw: os._exit(83)):
            runtime.run_once()
    raise AssertionError('reopened driver crash injection was not consumed')


@unittest.skipUnless(os.name == 'posix' and Path('/proc/self/stat').exists(),
                     'SDK Linux process-birth observations required')
class InterruptedExecutionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.runtime = Kernel.open_sqlite(self.root / 'kernel.sqlite3', {'handler': handler},
                                         isolation_mode='thread', lease_seconds=3600)
        self.addCleanup(self.runtime.close)
        self.sdk = Orchestrator(self.root / 'orchestrator.sqlite3', self.runtime.kernel,
                                runtime=self.runtime)
        self.sdk.create_run('isolated', command_id='create')
        command = self.runtime.command('handler', execution_id='author', idempotency_key='author',
            correlation_id='isolated', payload={'run_id': 'isolated', 'task_id': 'author',
                                                'command_id': 'author'}, timeout_seconds=3600)
        self.sdk.apply_operations('isolated', command_id='dispatch', expected_revision=0,
            operations=[{'kind': 'add_task', 'task_id': 'author', 'command': command.to_dict()},
                        {'kind': 'dispatch', 'task_id': 'author'},
                        {'kind': 'watch_task', 'task_id': 'author', 'watch_id': 'author-watch',
                         'target': {'run_id': 'isolated', 'task_id': 'author'}}])
        self.sdk.flush()
        self.lease = self.runtime.kernel.claim_and_start('old-driver', lease_seconds=3600,
                                                         registry_revision=command.registry_revision)
        self.sdk.sync()
        self.identity = ObservationIdentity('author', self.lease.attempt, self.lease.fence,
                                             run_id='isolated', task_id='author')
        self.runtime.observation_journal.bind_current(self.identity)
        self.recorder = ActivityRecorder(self.runtime.observation_journal, self.identity)
        self.addCleanup(self.recorder.close)
        self.header = {'run_id': 'isolated', 'run_dir': str(self.root)}
        self.driver = subprocess.Popen([sys.executable, '-c', 'import sys; sys.stdin.read()'],
                                       stdin=subprocess.PIPE)
        self.addCleanup(self.stop_driver)
        ticks = int(Path(f'/proc/{self.driver.pid}/stat').read_bytes().rsplit(b')', 1)[1].split()[19])
        self.registration = {'pid': self.driver.pid,
            'birth_identity': {'pid': self.driver.pid, 'start_ticks': ticks, 'namespace': pid_namespace()},
            'namespace': pid_namespace()}

    def stop_driver(self):
        if self.driver.poll() is None:
            self.driver.stdin.close()
            self.driver.wait(timeout=5)

    def evidence(self, *, cleanup=True, namespace=None, driver_pid=None, driver_birth=None):
        values = dict(self.registration)
        if namespace is not None:
            values['namespace'] = namespace
        if driver_pid is not None:
            values['pid'] = driver_pid
        if driver_birth is not None:
            values['birth_identity'] = driver_birth
        self.runtime.observation_journal.register_process(self.identity, 'driver', role='driver', **values)
        if cleanup:
            self.recorder.phase('process_cleanup', details={
                'state': 'confirmed', 'source': 'runtime_supervisor_reaped'})
        self.recorder.flush()
        observed = self.runtime.observe('author', attempt=self.lease.attempt, fence=self.lease.fence)
        self.assertTrue(observed['complete'], observed)
        self.assertTrue(observed['current'], observed)
        return observed

    def effect(self):
        effect = self.runtime.kernel.prepare_effect(self.lease, effect_id='stage:author',
            name='stage', request={'task_id': 'author'})
        return self.runtime.kernel.claim_effect(self.lease, effect.effect_id)

    def reconcile(self):
        return reconcile_interrupted_executions(self.root, self.header, self.runtime, self.sdk)

    def assert_unchanged(self, report, reason):
        self.assertEqual(report['changed'], 0, report)
        self.assertEqual(report['executions'][0]['reason'], reason, report)
        self.assertEqual(self.runtime.kernel.get('author').state, 'running')

    def test_dead_driver_cleanup_parks_performing_effect_and_sdk_notifies_without_replay(self):
        original = self.effect()
        self.evidence()
        self.stop_driver()
        report = self.reconcile()
        self.assertEqual(report['changed'], 1, report)
        execution = self.runtime.kernel.get('author')
        self.assertEqual(execution.state, 'recovery_required')
        effect = self.runtime.kernel.get_effect(original.effect_id)
        self.assertEqual(effect.state, 'indeterminate')
        self.assertEqual(effect.response['code'], 'effect_outcome_uncertain')
        self.assertIsNone(effect.recovery_decision)
        self.assertEqual((effect.attempt, effect.fence), (original.attempt, original.fence))
        self.assertEqual(self.sdk.get_run('isolated')['tasks']['author']['attempts'][-1]['state'],
                         'recovery_required')
        self.sdk.collect_notifications()
        notices = []
        self.sdk.deliver_notifications(lambda notice: notices.append(notice), owner='isolated-test')
        recoveries = [notice for notice in notices if notice['kind'] == 'recovery_required']
        self.assertEqual(len(recoveries), 1, notices)
        self.assertEqual(recoveries[0]['execution_id'], 'author')
        self.assertIsNone(self.runtime.run_once())
        self.assertEqual(self.reconcile()['changed'], 0)
        saved = json.loads((self.root / 'artifacts/monitor/interrupted-executions.json').read_text())
        self.assertEqual(saved['run_id'], 'isolated')

    def test_dead_driver_without_effects_is_failed_not_replayed(self):
        self.evidence()
        self.stop_driver()
        report = self.reconcile()
        self.assertEqual(report['changed'], 1, report)
        execution = self.runtime.kernel.get('author')
        self.assertEqual(execution.state, 'dead')
        self.assertEqual(execution.result.error.code, 'worker_interrupted')
        self.assertFalse(execution.result.error.retryable)
        self.assertIsNone(self.runtime.run_once())

    def test_live_driver_cannot_be_changed_even_with_cleanup_receipt(self):
        self.evidence()
        self.assert_unchanged(self.reconcile(), 'registered_driver_alive')

    def test_current_process_cannot_be_treated_as_interrupted_worker(self):
        ticks = int(Path('/proc/self/stat').read_bytes().rsplit(b')', 1)[1].split()[19])
        self.evidence(driver_pid=os.getpid(), driver_birth={
            'pid': os.getpid(), 'start_ticks': ticks, 'namespace': pid_namespace()})
        self.assert_unchanged(self.reconcile(), 'current_driver_process')

    def test_other_namespace_is_unknown_even_after_pid_disappears(self):
        self.evidence(namespace='pid:[another-namespace]')
        self.stop_driver()
        self.assert_unchanged(self.reconcile(), 'driver_pid_namespace_unverified')

    def test_birth_identity_is_required_even_with_cleanup_and_dead_pid(self):
        self.evidence(driver_birth={'pid': self.driver.pid})
        self.stop_driver()
        self.assert_unchanged(self.reconcile(), 'driver_birth_identity_unavailable')

    def test_worker_exit_or_supervisor_exit_does_not_confirm_descendant_cleanup(self):
        self.evidence(cleanup=False)
        self.stop_driver()
        self.runtime.observation_journal.register_process(self.identity, 'worker', role='worker',
            pid=self.driver.pid, birth_identity=self.registration['birth_identity'],
            namespace=pid_namespace())
        self.runtime.observation_journal.observe_process(self.identity, 'worker', 'exited',
            evidence={'source': 'multiprocessing_wait_status', 'returncode': 0, 'cleanup': 'unknown'})
        self.assert_unchanged(self.reconcile(), 'worker_tree_cleanup_unknown')

    def test_missing_driver_registration_remains_unknown(self):
        self.recorder.phase('process_cleanup', details={
            'state': 'confirmed', 'source': 'runtime_supervisor_reaped'})
        self.recorder.flush()
        self.assert_unchanged(self.reconcile(), 'registered_driver_unavailable')

    def test_stale_attempt_or_fence_observation_cannot_control_current_lease(self):
        observed = self.evidence()
        self.stop_driver()
        for field in ('attempt', 'fence', 'execution_id', 'run_id', 'task_id', 'generation'):
            with self.subTest(field=field):
                changed = copy.deepcopy(observed)
                changed['identity'][field] = 'other' if field.endswith('_id') else 99
                with patch.object(self.runtime, 'observe', return_value=changed):
                    self.assert_unchanged(self.reconcile(), 'execution_observation_incomplete_or_stale')

    def test_incomplete_observation_retains_raw_error_without_mutation(self):
        observed = self.evidence()
        self.stop_driver()
        observed.update(complete=False, unknown_reason='control_observation_unavailable',
                        error='OperationalError: database is locked')
        observed.pop('execution')
        with patch.object(self.runtime, 'observe', return_value=observed):
            report = self.reconcile()
        self.assert_unchanged(report, 'execution_observation_incomplete_or_stale')
        self.assertEqual(report['executions'][0]['observation_error']['error'],
                         'OperationalError: database is locked')

    def test_unrelated_telemetry_gap_does_not_gate_exact_cleanup_reconciliation(self):
        observed = self.evidence()
        self.stop_driver()
        observed.update(complete=False, unknown_reason='telemetry_collection_incomplete')
        with patch.object(self.runtime, 'observe', return_value=observed):
            report = self.reconcile()
        self.assertEqual(report['changed'], 1, report)
        self.assertEqual(report['executions'][0]['observation_diagnostic']['unknown_reason'],
                         'telemetry_collection_incomplete')

    def test_proc_mount_mismatch_is_unknown_even_with_same_pid_namespace(self):
        self.evidence()
        self.stop_driver()
        with patch('modport.interrupted_execution._proc_mount_matches_namespace', return_value=False):
            self.assert_unchanged(self.reconcile(), 'driver_pid_namespace_unverified')

    def test_cancelled_run_preserves_unknown_sdk_cleanup_and_effect(self):
        self.effect()
        self.evidence()
        self.stop_driver()
        current = self.sdk.get_run('isolated')
        self.sdk.apply_operations('isolated', command_id='user-stop',
            expected_revision=current['revision'], operations=[],
            application_state=pack_application_state(self.root,
                {'user_cancelled': True, 'stop_reason': 'user_cancelled',
                 **{f'context_{index}': 'retained cancellation evidence ' * 4
                    for index in range(1000)}}))
        self.assertNotIn('user_cancelled', self.sdk.get_run('isolated')['application_state'])
        before = self.sdk.inspect_cancellation('isolated').to_dict()
        report = self.reconcile()
        self.assert_unchanged(report, 'cancellation_requires_existing_sdk_settlement')
        after = self.sdk.inspect_cancellation('isolated').to_dict()
        self.assertEqual(before['executions'], after['executions'])
        self.assertEqual(self.runtime.kernel.get_effect('stage:author').state, 'performing')

    def test_actual_process_supervisor_cleanup_and_dead_driver_are_reconciled(self):
        root = self.root / 'actual-process-crash'
        root.mkdir()
        child = subprocess.run([sys.executable, str(Path(__file__).resolve()), '--crash-driver', str(root)],
            capture_output=True, timeout=15)
        self.assertEqual(child.returncode, 83, child.stderr.decode())
        with Kernel.open_sqlite(root / 'kernel.sqlite3', {'crash': crash_handler},
                isolation_mode='process', lease_seconds=3600) as runtime:
            sdk = Orchestrator(root / 'orchestrator.sqlite3', runtime.kernel, runtime=runtime)
            sdk.sync()
            execution = runtime.kernel.get('crashed-author')
            self.assertEqual(execution.state, 'running')
            self.assertEqual(runtime.kernel.get_effect('stage:crash').state, 'performing')
            observed = runtime.observe('crashed-author', attempt=execution.attempt, fence=execution.fence)
            receipts = [note for note in observed['diagnostics']['notes']
                        if note['phase'] == 'process_cleanup']
            self.assertTrue(any(note['evidence'].get('source') == 'runtime_supervisor_reaped'
                                and note['evidence'].get('state') == 'confirmed' for note in receipts), observed)
            report = reconcile_interrupted_executions(root, {'run_id': 'crash'}, runtime, sdk)
            self.assertEqual(report['changed'], 1, report)
            self.assertEqual(runtime.kernel.get('crashed-author').state, 'recovery_required')
            self.assertEqual(runtime.kernel.get_effect('stage:crash').state, 'indeterminate')
            self.assertIsNone(runtime.run_once())

    def test_driver_startup_parks_then_notifies_and_dispatches_existing_supervisor(self):
        # Reuse the current command-producing fixture, then enter the actual
        # driver startup with a temporary SDK session. Stop at host entry so
        # this witness dispatches diagnosis without executing more authors.
        from test_watchdog_routing import WatchdogRoutingTests
        from modport.operations import MigrationRun
        from modport.payload_storage import unpack_input
        fixture = WatchdogRoutingTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.owner.handlers = {}
        fixture.sdk.flush()
        command = fixture.snapshot()['tasks']['source']['attempts'][-1]['command']
        lease = fixture.runtime.kernel.claim_and_start('crashed-driver', lease_seconds=3600,
                                                      registry_revision=fixture.runtime.registry_revision)
        fixture.sdk.sync()
        execution_id = command['execution_id']
        fixture.runtime.kernel.prepare_effect(lease, effect_id='stage:startup', name='stage', request={})
        fixture.runtime.kernel.claim_effect(lease, 'stage:startup')
        identity = ObservationIdentity(execution_id, lease.attempt, lease.fence,
                                       run_id='run', task_id='source')
        fixture.runtime.observation_journal.bind_current(identity)
        fixture.runtime.observation_journal.register_process(identity, 'driver', role='driver',
                                                             **self.registration)
        recorder = ActivityRecorder(fixture.runtime.observation_journal, identity)
        self.addCleanup(recorder.close)
        recorder.phase('process_cleanup', details={'state': 'confirmed', 'source': 'runtime_supervisor_reaped'})
        recorder.flush()
        self.stop_driver()

        @contextmanager
        def session(*args, **kwargs):
            yield fixture.root, fixture.header, fixture.runtime, fixture.sdk

        class ReachedHost(Exception):
            pass

        with patch.object(fixture.owner, 'session', session), patch(
                'modport.operations.OrchestratorHost', side_effect=ReachedHost):
            with self.assertRaises(ReachedHost):
                fixture.owner._execute_owned(MigrationRun('run', fixture.root, fixture.snapshot()),
                    poll_interval=.01, driver=SimpleNamespace(check_health=lambda: None))
        state = fixture.snapshot()
        self.assertEqual(fixture.runtime.kernel.get(execution_id).state, 'recovery_required')
        self.assertEqual(fixture.runtime.kernel.get_effect('stage:startup').state, 'indeterminate')
        self.assertEqual(len(state['tasks']['source']['attempts']), 1)
        supervisors = [task for task_id, task in state['tasks'].items() if task_id.startswith('watchdog.')]
        self.assertEqual(len(supervisors), 1, state['tasks'])
        attempt = supervisors[0]['attempts'][-1]
        self.assertTrue(attempt['dispatched'])
        payload = unpack_input(fixture.root, attempt['command']['payload'])
        self.assertEqual(payload['stage_id'], 'supervisor')
        incident = payload['payload']['watchdog_incident']
        self.assertEqual(incident['target_execution_id'], execution_id)
        self.assertEqual(payload['options']['deadline_epoch'], fixture.header['deadline_epoch'])

    def test_current_producer_continuation_binds_physical_sdk_membership_and_logical_payload(self):
        from test_watchdog_routing import WatchdogRoutingTests
        fixture = WatchdogRoutingTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        root = self.root / 'physical-continuation'
        root.mkdir()
        with Kernel.open_sqlite(root / 'kernel.sqlite3', fixture.runtime.handlers,
                               isolation_mode='thread') as runtime:
            sdk = Orchestrator(root / 'orchestrator.sqlite3', runtime.kernel, runtime=runtime)
            header = {**fixture.header, 'run_id': 'physical-continuation', 'logical_run_id': 'logical-original',
                      'run_dir': str(root), 'registry_revision': runtime.registry_revision}
            sdk.create_run(header['run_id'], command_id='create-continuation')
            app = fixture.owner._new_application()
            operations = fixture.owner._schedule(sdk.get_run(header['run_id']), header, app,
                                                   'source', dependencies=[])
            sdk.apply_operations(header['run_id'], command_id='schedule-continuation',
                expected_revision=0, operations=operations, application_state=app)
            sdk.flush()
            lease = runtime.kernel.claim_and_start('old-continuation-driver', lease_seconds=3600,
                                                     registry_revision=runtime.registry_revision)
            sdk.sync()
            snapshot = sdk.get_run(header['run_id'])
            current = snapshot['tasks']['source']['attempts'][-1]
            execution = runtime.kernel.get(current['command']['execution_id'])
            payload = execution.command.payload
            from modport.payload_storage import unpack_input
            payload = unpack_input(root, payload)
            self.assertEqual(payload['run_id'], 'logical-original')
            self.assertNotIn('execution_run_id', payload['options'])
            runtime.kernel.prepare_effect(lease, effect_id='stage:continuation', name='stage', request={})
            runtime.kernel.claim_effect(lease, 'stage:continuation')
            identity = ObservationIdentity(execution.execution_id, lease.attempt, lease.fence)
            runtime.observation_journal.bind_current(identity)
            runtime.observation_journal.register_process(identity, 'driver', role='driver', **self.registration)
            recorder = ActivityRecorder(runtime.observation_journal, identity)
            self.addCleanup(recorder.close)
            recorder.phase('process_cleanup', details={
                'state': 'confirmed', 'source': 'runtime_supervisor_reaped'})
            recorder.flush()
            self.stop_driver()
            for mutation in ('run_id', 'execution_id', 'payload', 'task_attempt'):
                altered = copy.deepcopy(snapshot)
                if mutation == 'run_id':
                    altered['run_id'] = 'foreign-run'
                elif mutation == 'execution_id':
                    altered['tasks']['source']['attempts'][-1]['command']['execution_id'] = 'foreign-author'
                elif mutation == 'payload':
                    altered['tasks']['source']['attempts'][-1]['command']['payload'] = {'task_id': 'foreign-task'}
                else:
                    altered['tasks']['source']['attempts'].append(copy.deepcopy(current))
                    altered['tasks']['source']['attempts'][-1]['command']['execution_id'] = 'new-current-author'
                with self.subTest(mutation=mutation):
                    proof = stopped_execution_evidence(root, header, runtime, execution,
                                                       task_id='source', snapshot=altered)
                    self.assertFalse(proof['confirmed'], proof)
            report = reconcile_interrupted_executions(root, header, runtime, sdk)
            self.assertEqual(report['changed'], 1, report)
            self.assertEqual(runtime.kernel.get(execution.execution_id).state, 'recovery_required')
            self.assertEqual(len(sdk.get_run(header['run_id'])['tasks']['source']['attempts']), 1)
            recorder.close()

    def test_public_reopen_generation_uses_sdk_membership_for_actual_unbound_worker_observation(self):
        from test_watchdog_interrupted_settlement import fixture_runtime
        from modport.evidence import read_json
        root = self.root / 'actual-reopened-crash'
        root.mkdir()
        child = subprocess.run([sys.executable, str(Path(__file__).resolve()), '--reopened-crash-driver', str(root)],
            capture_output=True, timeout=20)
        self.assertEqual(child.returncode, 83, child.stderr.decode())
        header = read_json(root / 'run.json')
        with fixture_runtime(root) as runtime:
            sdk = Orchestrator(root / 'orchestrator.sqlite3', runtime.kernel, runtime=runtime)
            sdk.sync()
            snapshot = sdk.get_run(header['run_id'])
            self.assertEqual(snapshot['generation'], 1)
            current = snapshot['tasks']['behavior_extract']['attempts'][-1]
            self.assertEqual(current['generation'], snapshot['generation'])
            execution = runtime.kernel.get(current['command']['execution_id'])
            observed = runtime.observe(execution.execution_id, attempt=execution.attempt, fence=execution.fence)
            self.assertIsNone(observed['identity']['run_id'])
            self.assertIsNone(observed['identity']['task_id'])
            self.assertEqual(observed['identity']['generation'], 0)
            proof = stopped_execution_evidence(root, header, runtime, execution,
                task_id='behavior_extract', snapshot=snapshot)
            self.assertTrue(proof['confirmed'], proof)
            self.assertEqual(proof['generation'], current['generation'])
            bound_stale = copy.deepcopy(observed)
            bound_stale['identity'].update(run_id=header['run_id'], task_id='behavior_extract')
            with patch.object(runtime, 'observe', return_value=bound_stale):
                stale = stopped_execution_evidence(root, header, runtime, execution,
                    task_id='behavior_extract', snapshot=snapshot)
            self.assertFalse(stale['confirmed'], stale)
            foreign = copy.deepcopy(observed)
            foreign['identity'].update(run_id='foreign-run', task_id='behavior_extract', generation=1)
            with patch.object(runtime, 'observe', return_value=foreign):
                rejected = stopped_execution_evidence(root, header, runtime, execution,
                    task_id='behavior_extract', snapshot=snapshot)
            self.assertFalse(rejected['confirmed'], rejected)
            report = reconcile_interrupted_executions(root, header, runtime, sdk)
            self.assertEqual(report['changed'], 1, report)
            self.assertEqual(runtime.kernel.get(execution.execution_id).state, 'recovery_required')
            self.assertEqual(sdk.get_run(header['run_id'])['generation'], snapshot['generation'])


if __name__ == '__main__':
    if len(sys.argv) == 3 and sys.argv[1] == '--crash-driver':
        crash_driver(sys.argv[2])
    elif len(sys.argv) == 3 and sys.argv[1] == '--reopened-crash-driver':
        reopened_crash_driver(sys.argv[2])
    else:
        unittest.main()
