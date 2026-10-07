"""Current driver observation survives real SQLite contention without revival."""
import json
from contextlib import contextmanager
from pathlib import Path
import sqlite3
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from dispatcher_sdk.execution_kernel import Kernel
from dispatcher_sdk.observability import ObservationError, ObservationOptions
from dispatcher_sdk.orchestrator import Orchestrator
from modport.application_state_storage import hydrate_run_snapshot
from modport.contracts import OperationResult
from modport.kernel_runtime import sdk_handlers
from modport.models import Budget, MigrationRequest
from modport.operations import MigrationOperations
from modport.watchdog_events import (
    collect_runtime_notifications, enabled, install_runtime_watches, notifications,
    observe_optional,
)
from modport.workflow import WORKFLOW_VERSION, compile_migration_workflow


class Assignment:
    __execution_kernel_revision__ = 'optional-observation-contention-fixture'

    def __call__(self, operation):
        if hasattr(self, 'entered'):
            self.entered.set()
            if not self.release.wait(8):
                raise TimeoutError('bounded driver fixture handler was not released')
        return OperationResult('completed', operation.run_id, operation.task_id,
                               operation.stage_id, operation.command_id)


class ImmediateAdmissionOrchestrator(Orchestrator):
    """Use the real SDK transaction with a short test-only admission allowance."""
    def _connect(self, **options):
        connection = super()._connect(**options)
        connection.execute('PRAGMA busy_timeout=0')
        return connection


class WatchdogObservationContentionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.assignment = Assignment()
        self.runtime = Kernel.open_sqlite(self.root / 'kernel.sqlite3',
            sdk_handlers({'modport.migration_plan': self.assignment}), isolation_mode='thread',
            observation_options=ObservationOptions(write_timeout=.02, flush_interval=.1))
        self.addCleanup(self.runtime.close)
        self.sdk = ImmediateAdmissionOrchestrator(self.root / 'orchestrator.sqlite3',
                                                 self.runtime.kernel, runtime=self.runtime)
        self.addCleanup(self.sdk.close)
        request = MigrationRequest('contention', 'https://example.invalid/mod', '1.20.1', '26.1.2',
            budget=Budget(max_seconds=600, max_agent_assignments=10))
        self.header = {'run_id': 'run', 'run_dir': str(self.root), 'request': request.to_dict(),
            'definition': compile_migration_workflow(request).to_dict(),
            'registry_revision': self.runtime.registry_revision, 'deadline_epoch': time.time() + 600,
            'initial_refs': {}, 'prior_findings': [], 'rubric_sha256': 'host-provenance',
            'watchdog_policy': {'enabled': True, 'inactivity_seconds': 600}}
        self.assertEqual(WORKFLOW_VERSION, self.header['definition']['workflow_version'])
        self.sdk.create_run('run', command_id='create', input=self.header,
                            definition=self.header['definition'])
        self.owner = MigrationOperations()
        self.app = self.owner._new_application()
        self.now = 10.0

    def snapshot(self):
        self.sdk.sync()
        return hydrate_run_snapshot(self.root, self.sdk.get_run('run'))

    def add_running(self, identifier):
        snapshot = self.snapshot()
        operations = self.owner._schedule(snapshot, self.header, self.app,
                                          'migration_plan', task_id=identifier, dependencies=[])
        self.sdk.apply_operations('run', command_id='schedule-' + identifier,
            expected_revision=snapshot['revision'], operations=operations, application_state=self.app)
        self.sdk.flush()
        dispatch = next(operation for operation in operations if operation['kind'] == 'add_task')
        lease = self.runtime.kernel.claim_and_start('contention-test',
            execution_id=dispatch['command']['execution_id'], registry_revision=self.runtime.registry_revision,
            lease_seconds=600)
        self.assertIsNotNone(lease)
        return lease

    def lock(self, path):
        connection = sqlite3.connect(path, timeout=1)
        connection.execute('BEGIN IMMEDIATE')
        self.addCleanup(connection.close)
        return connection

    def install(self, snapshot):
        return install_runtime_watches(self.runtime, self.sdk, self.header, snapshot,
                                       monotonic=lambda: self.now)

    def test_real_sdk_watch_busy_retains_installed_identity_and_recovers_after_cooldown(self):
        first = self.add_running('first')
        installed = self.install(self.snapshot())
        first_identity = (first.execution_id, first.attempt, first.fence)
        self.assertEqual([first_identity], installed['installed'])
        second = self.add_running('second')
        snapshot = self.snapshot()
        lock = self.lock(self.runtime.observation_storage['path'])
        started = time.monotonic()
        deferred = self.install(snapshot)
        self.assertLess(time.monotonic() - started, 1)
        self.assertFalse(deferred['completed'])
        self.assertEqual({first_identity}, self.runtime._modport_watchdog_attempts)
        diagnostic = deferred['deferred'][0]
        self.assertEqual('OperationalError', diagnostic['error_type'])
        self.assertEqual(sqlite3.SQLITE_BUSY, diagnostic['sqlite_errorcode'] & 255)
        self.assertIn('locked', diagnostic['detail'])
        self.assertEqual(1, diagnostic['failures'])
        retained = [json.loads(path.read_text()) for path in
                    (self.root / 'artifacts/watchdog/run/observation-errors').glob('*.json')]
        self.assertIn(diagnostic, retained)
        lock.rollback()
        # Releasing the database does not bypass the monotonic retry bound.
        self.assertFalse(self.install(snapshot)['completed'])
        self.assertEqual(1, diagnostic['failures'])
        self.now += 1
        resumed = self.install(snapshot)
        second_identity = (second.execution_id, second.attempt, second.fence)
        self.assertEqual([second_identity], resumed['installed'])
        self.assertEqual({first_identity, second_identity}, self.runtime._modport_watchdog_attempts)
        self.assertEqual('resolved', self.runtime._modport_watchdog_observation[diagnostic['operation']]['status'])
        self.assertEqual([], self.install(snapshot)['installed'])
        self.assertEqual(self.header['deadline_epoch'], snapshot['input']['deadline_epoch'])
        self.assertEqual(2, self.app['agent_assignments'])

    def test_startup_collection_and_delivery_handle_real_sdk_writer_contention(self):
        self.add_running('watched')
        # Collection traverses this real SDK watch and its retained Kernel events.
        lock = self.lock(self.sdk.db_path)
        report = collect_runtime_notifications(self.sdk, self.header, monotonic=lambda: self.now)
        self.assertFalse(report['completed'])
        self.assertEqual('notification-collect', report['collect']['diagnostic']['operation'])
        lock.rollback()
        self.now += 1
        report = collect_runtime_notifications(self.sdk, self.header, monotonic=lambda: self.now)
        self.assertTrue(report['completed'])
        self.sdk.enqueue_stall_notification({'kind': 'stalled', 'notification_id': 'delivery-contention',
            'execution_id': 'observed-execution', 'target': {'run_id': 'run', 'task_id': 'watched'}})
        # Test delivery separately from collection so the observed busy writer
        # belongs to the claim/ack path, not the collection transaction.
        lock = self.lock(self.sdk.db_path)
        with patch.object(self.sdk, 'collect_notifications', return_value=0):
            report = collect_runtime_notifications(self.sdk, self.header, monotonic=lambda: self.now)
        self.assertFalse(report['completed'])
        self.assertEqual('notification-deliver', report['deliver']['diagnostic']['operation'])
        self.assertEqual([], list(notifications(self.root, 'run')))
        lock.rollback()
        self.now += 1
        report = collect_runtime_notifications(self.sdk, self.header, monotonic=lambda: self.now)
        self.assertTrue(report['completed'])
        self.assertEqual(['delivery-contention'], [notice['notification_id']
                                                  for notice, _ in notifications(self.root, 'run')])

    def test_identity_error_propagates_without_discarding_an_installed_watch(self):
        first = self.add_running('first')
        self.add_running('second')
        watch = self.runtime.watch_stall

        def bind(execution_id, policy, **options):
            if execution_id.endswith(':second:1'):
                raise ObservationError('subscription identity is stale')
            return watch(execution_id, policy, **options)

        with patch.object(self.runtime, 'watch_stall', side_effect=bind):
            with self.assertRaisesRegex(ObservationError, 'subscription identity is stale'):
                self.install(self.snapshot())
        self.assertEqual({(first.execution_id, first.attempt, first.fence)},
                         self.runtime._modport_watchdog_attempts)

    def test_stopped_watchdog_overrides_old_authorization_and_does_not_call_sdk(self):
        stopped = {**self.header, 'watchdog_policy': {'enabled': False}}
        app = {'watchdog': {'authorized': True}}
        self.assertFalse(enabled(stopped, app))
        with patch.object(self.sdk, 'collect_notifications', side_effect=AssertionError('revived stopped watchdog')):
            self.assertFalse(collect_runtime_notifications(self.sdk, stopped, app)['enabled'])
        with patch.object(self.runtime, 'set_stall_notification_bridge',
                          side_effect=AssertionError('revived stopped watchdog')):
            self.assertFalse(install_runtime_watches(self.runtime, self.sdk, stopped,
                {'tasks': {}, 'application_state': app})['enabled'])

    def test_noncontention_sqlite_and_authentication_errors_are_not_masked(self):
        permanent_code = sqlite3.OperationalError('database is locked')
        permanent_code.sqlite_errorcode = sqlite3.SQLITE_CORRUPT
        for error in (sqlite3.OperationalError('no such table: observation_binding'),
                      permanent_code,
                      PermissionError('SDK authentication denied'),
                      ValueError('SDK execution identity differs')):
            with self.subTest(error=error):
                def fail():
                    raise error
                with self.assertRaises(type(error)):
                    observe_optional(self.runtime, self.header, 'permanent-error', fail)

    def test_real_extended_sqlite_locked_error_has_bounded_cooldown(self):
        path = self.root / 'optional-reader.sqlite3'
        uri = path.as_uri() + '?cache=shared'
        writer = sqlite3.connect(uri, uri=True, timeout=0)
        reader = sqlite3.connect(uri, uri=True, timeout=0)
        self.addCleanup(reader.close)
        self.addCleanup(writer.close)
        writer.execute('CREATE TABLE optional_observation(value INTEGER)')
        writer.execute('INSERT INTO optional_observation VALUES(1)')
        writer.commit()
        writer.execute('BEGIN IMMEDIATE')
        writer.execute('UPDATE optional_observation SET value=2')
        for delay in (1, 2, 4, 8, 16, 30, 30):
            report = observe_optional(self.runtime, self.header, 'optional-reader',
                lambda: reader.execute('SELECT value FROM optional_observation').fetchone()[0],
                monotonic=lambda: self.now)
            diagnostic = report['diagnostic']
            self.assertEqual(sqlite3.SQLITE_LOCKED, diagnostic['sqlite_errorcode'] & 255)
            self.assertEqual(delay, diagnostic['retry_at'] - self.now)
            self.now = diagnostic['retry_at']
        writer.rollback()
        report = observe_optional(self.runtime, self.header, 'optional-reader',
            lambda: reader.execute('SELECT value FROM optional_observation').fetchone()[0],
            monotonic=lambda: self.now)
        self.assertTrue(report['completed'])
        self.assertEqual(1, report['value'])

    def test_actual_driver_loop_survives_busy_watch_then_completes_sdk_handler(self):
        from modport.operations import MigrationRun
        from modport.runner import DriverLease

        # The current compile/package route can finish after a pending fixture
        # assignment without inventing target behavior acceptance evidence.
        request = MigrationRequest('driver-contention', 'https://example.invalid/mod', '1.20.1', '26.1.2',
            validation_scope='compile_package', budget=Budget(max_seconds=600, max_agent_assignments=10))
        header = {**self.header, 'run_id': 'driver-run', 'request': request.to_dict(),
                  'definition': compile_migration_workflow(request).to_dict()}
        # Driver/host concurrency uses the SDK's normal admission allowance.
        # The zero-admission SDK subclass belongs only to the isolated startup
        # contention checks above; it would introduce unrelated writer races.
        sdk = Orchestrator(self.root / 'orchestrator.sqlite3', self.runtime.kernel, runtime=self.runtime)
        self.addCleanup(sdk.close)
        sdk.create_run('driver-run', command_id='create-driver', input=header,
                       definition=header['definition'])
        self.assignment.entered = threading.Event()
        self.assignment.release = threading.Event()
        self.addCleanup(self.assignment.release.set)
        root, runtime = self.root, self.runtime

        class BoundOperations(MigrationOperations):
            @contextmanager
            def session(self, run_dir, run_id, **options):
                if Path(run_dir) != root or run_id != header['run_id']:
                    raise ValueError('driver fixture session identifies another Run')
                yield root, header, runtime, sdk

        owner = BoundOperations(handlers={'modport.migration_plan': self.assignment}, isolation_mode='thread')
        snapshot = hydrate_run_snapshot(root, sdk.get_run('driver-run'))
        app = owner._new_application()
        app['flowthrough_finish_pending'] = True
        operations = owner._schedule(snapshot, header, app, 'migration_plan', dependencies=[])
        sdk.apply_operations('driver-run', command_id='schedule-driver', expected_revision=snapshot['revision'],
                             operations=operations, application_state=app)
        execution_id = next(operation['command']['execution_id'] for operation in operations
                            if operation['kind'] == 'add_task')
        locked = threading.Event()
        failures = []
        observations = []

        def hold_observation_writer():
            connection = sqlite3.connect(runtime.observation_storage['path'], timeout=1)
            try:
                connection.execute('BEGIN IMMEDIATE')
                locked.set()
                limit = time.monotonic() + 5
                while time.monotonic() < limit:
                    diagnostic = next((row for key, row in getattr(runtime,
                        '_modport_watchdog_observation', {}).items()
                        if key.startswith('watch:') and row['status'] == 'deferred'), None)
                    if diagnostic is not None:
                        observations.append(dict(diagnostic))
                        break
                    time.sleep(.01)
                if not observations:
                    raise TimeoutError('actual driver did not defer the busy observation watch')
                connection.rollback()
                limit = time.monotonic() + 3
                while time.monotonic() < limit:
                    if any(identity[0] == execution_id for identity in
                           getattr(runtime, '_modport_watchdog_attempts', set())):
                        return
                    time.sleep(.01)
                raise TimeoutError('actual driver did not reinstall the watch after contention')
            except BaseException as error:
                failures.append(error)
            finally:
                connection.rollback()
                connection.close()
                self.assignment.release.set()

        blocker = threading.Thread(target=hold_observation_writer, name='bounded-observation-writer')
        blocker.start()
        self.assertTrue(locked.wait(2), 'fixture did not obtain its observation SQLite writer')
        deadline = time.monotonic() + 10

        class BoundedLease(DriverLease):
            def check_health(self):
                super().check_health()
                if time.monotonic() >= deadline:
                    raise TimeoutError('bounded actual driver regression did not finish')

        try:
            with BoundedLease(root, 'driver-run') as driver:
                result = owner._execute_owned(MigrationRun('driver-run', root, snapshot),
                                              poll_interval=.02, driver=driver)
        finally:
            self.assignment.release.set()
            blocker.join(3)
        self.assertFalse(blocker.is_alive())
        self.assertEqual([], failures)
        self.assertTrue(self.assignment.entered.is_set())
        self.assertEqual(sqlite3.SQLITE_BUSY, observations[0]['sqlite_errorcode'] & 255)
        self.assertEqual('resolved', runtime._modport_watchdog_observation[observations[0]['operation']]['status'])
        self.assertEqual('succeeded', result.status)
        attempt = result.snapshot['tasks']['migration_plan']['attempts'][-1]
        self.assertEqual('succeeded', attempt['state'])
        self.assertEqual('completed', attempt['result']['value']['status'])
        self.assertEqual(execution_id, attempt['result']['value']['command_id'])
        self.assertEqual(1, result.snapshot['application_state']['agent_assignments'])
        self.assertEqual(header['deadline_epoch'], result.snapshot['input']['deadline_epoch'])

    def test_real_runtime_initialization_busy_remains_explicit_until_public_reopen(self):
        lease = self.add_running('initialization')
        snapshot = self.snapshot()
        sidecar = self.root / 'initialization-observations.sqlite3'
        connection = sqlite3.connect(sidecar, timeout=0)
        self.addCleanup(connection.close)
        connection.execute('PRAGMA journal_mode=WAL')
        connection.execute('BEGIN IMMEDIATE')
        kernel = Path(self.runtime.kernel.db_path)
        options = {'isolation_mode': 'thread', 'observation_path': str(sidecar),
                   'observation_options': ObservationOptions(write_timeout=.02)}
        runtime = Kernel.open_sqlite(kernel, sdk_handlers({'modport.migration_plan': Assignment()}), **options)
        self.addCleanup(runtime.close)
        self.assertFalse(runtime.observation_storage['available'])
        self.assertEqual('OperationalError: database is locked', runtime.observation_storage['unknown_reason'])
        report = install_runtime_watches(runtime, self.sdk, self.header, snapshot)
        self.assertFalse(report['completed'])
        self.assertEqual([], report['installed'])
        diagnostic = report['deferred'][0]
        self.assertEqual('runtime_reopen_required', diagnostic['status'])
        self.assertEqual('next_runtime_reopen', diagnostic['retry_mode'])
        self.assertEqual(runtime.observation_storage['unknown_reason'], diagnostic['unknown_reason'])
        connection.rollback()
        # Unlocking SQLite cannot create a supervisor in an already-open SDK
        # Runtime. Keep this failure unresolved instead of pretending to watch.
        self.assertFalse(install_runtime_watches(runtime, self.sdk, self.header, snapshot)['completed'])
        self.assertEqual(set(), runtime._modport_watchdog_attempts)
        runtime.close()
        reopened = Kernel.open_sqlite(kernel, sdk_handlers({'modport.migration_plan': Assignment()}), **options)
        self.addCleanup(reopened.close)
        self.assertTrue(reopened.observation_storage['available'])
        self.assertTrue(install_runtime_watches(reopened, self.sdk, self.header, snapshot)['completed'])
        self.assertIn((lease.execution_id, lease.attempt, lease.fence), reopened._modport_watchdog_attempts)

    def test_cooldown_starts_after_slow_failed_optional_call(self):
        clock = [10.0]

        def slow_contention():
            clock[0] += 30
            raise sqlite3.OperationalError('database is locked')

        report = observe_optional(self.runtime, self.header, 'slow-admission', slow_contention,
                                  monotonic=lambda: clock[0])
        self.assertEqual(41, report['diagnostic']['retry_at'])
        report = observe_optional(self.runtime, self.header, 'slow-admission',
                                  lambda: self.fail('admission latency consumed the cooldown'),
                                  monotonic=lambda: clock[0])
        self.assertFalse(report['completed'])

    def test_unavailable_runtime_permanent_reason_retains_sdk_error(self):
        class Unavailable:
            observation_storage = {'available': False, 'unknown_reason': 'ObservationError: source binding differs'}

            def set_stall_notification_bridge(self, callback):
                pass

            def watch_stall(self, *arguments, **options):
                raise RuntimeError('durable observation storage is unavailable')

        lease = self.add_running('unavailable')
        runtime = Unavailable()
        runtime.kernel = self.runtime.kernel
        with self.assertRaisesRegex(RuntimeError, 'durable observation storage is unavailable'):
            install_runtime_watches(runtime, self.sdk, self.header, self.snapshot())
        self.assertNotIn((lease.execution_id, lease.attempt, lease.fence), runtime._modport_watchdog_attempts)


if __name__ == '__main__':
    unittest.main()
