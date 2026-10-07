"""Watchdog intake and decisions through current ModPort commands and SDK tasks."""
from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
import time
import unittest

from dispatcher_sdk.execution_kernel import Kernel
from dispatcher_sdk.orchestrator import Orchestrator

from modport.application_state_storage import hydrate_run_snapshot
from modport.contracts import OperationResult
from modport.kernel_runtime import sdk_handlers
from modport.memory_admission import MemorySnapshot
from modport.models import Budget, MigrationRequest
from modport.operations import MigrationOperations
from modport.watchdog_events import accept_notification, notifications
from modport.watchdog_routing import begin, decide, intercept_failure
from modport.workflow import compile_migration_workflow


class Assignment:
    __execution_kernel_revision__ = 'watchdog-routing-current-fixture'

    def __init__(self, status='completed', invalid=False):
        self.status, self.invalid = status, invalid

    def __call__(self, command):
        outputs = {}
        if command.stage_id == 'supervisor':
            incident = command.payload['watchdog_incident']
            outputs['watchdog_decision'] = {'incident_id': incident['incident_id'],
                'action': 'repair_resume', 'reason': 'The raw provider failure identifies the missing configuration.',
                'instruction': 'Restore the provider configuration, then rerun the original assignment.',
                'wait_for': [], 'stop_category': None}
            if self.invalid:
                outputs['watchdog_decision']['action'] = 'invented'
        return OperationResult(self.status, command.run_id, command.task_id, command.stage_id,
            command.command_id, outputs=outputs,
            detail='Raw provider authentication failure' if self.status == 'failed' else 'Fixture execution',
            error_code='provider_authentication_failed' if self.status == 'failed' else None)


class WatchdogRoutingTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.now = time.time()
        self.source = Assignment('failed')
        self.supervisor = Assignment()
        handlers = {'modport.source': self.source, 'modport.supervisor': self.supervisor,
                    'modport.mod_analysis': self.source}
        self.runtime = Kernel.open_sqlite(self.root / 'kernel.sqlite3', sdk_handlers(handlers), isolation_mode='thread')
        self.addCleanup(self.runtime.close)
        self.sdk = Orchestrator(self.root / 'orchestrator.sqlite3', self.runtime.kernel,
                                runtime=self.runtime)
        self.addCleanup(self.sdk.close)
        request = MigrationRequest('watchdog-routing', 'https://example.invalid/mod.git', '1.20.1', '26.1.2',
                                   budget=Budget(max_seconds=3600, max_agent_assignments=20))
        self.owner = MigrationOperations(clock=lambda: self.now,
            memory_probe=lambda: MemorySnapshot(64 * 1024 ** 3, 64 * 1024 ** 3, 'fixture'))
        self.header = {'run_id': 'run', 'run_dir': str(self.root), 'request': request.to_dict(),
            'definition': compile_migration_workflow(request).to_dict(),
            'deadline_epoch': self.now + 3600, 'registry_revision': self.runtime.registry_revision,
            'initial_refs': {}, 'prior_findings': [], 'rubric_sha256': 'host-rubric-provenance',
            'watchdog_policy': {'enabled': True, 'inactivity_seconds': 600}}
        self.sdk.create_run('run', command_id='create-test', input=self.header,
                            definition=self.header['definition'])
        self.app = self.owner._new_application()
        snapshot = self.snapshot()
        operations = self.owner._schedule(snapshot, self.header, self.app, 'source', dependencies=[])
        self.apply(operations, 'source')

    def snapshot(self):
        return hydrate_run_snapshot(self.root, self.sdk.get_run('run'))

    def apply(self, operations, command_id):
        snapshot = self.snapshot()
        self.sdk.apply_operations('run', command_id=command_id,
            expected_revision=snapshot['revision'], expected_generation=snapshot['generation'],
            operations=operations, application_state=self.app)

    def run_one(self):
        self.sdk.flush()
        self.assertIsNotNone(self.runtime.run_once())
        self.sdk.sync()

    def deliver(self):
        self.sdk.collect_notifications()
        self.sdk.deliver_notifications(lambda notice: accept_notification(self.root, notice),
                                       owner='watchdog-routing-test', limit=10)

    def consume(self):
        return decide(self.owner, self.snapshot(), self.header, self.app, self.sdk)

    def test_actual_sdk_business_failure_notice_schedules_supervisor_once(self):
        self.run_one()
        failed = self.snapshot()['tasks']['source']['attempts'][-1]
        self.assertEqual('succeeded', failed['state'])
        self.assertEqual('failed', failed['result']['value']['status'])
        self.deliver()
        self.assertEqual(1, len(list(notifications(self.root, 'run'))))
        operations, owns = self.consume()
        self.assertTrue(owns)
        additions = [row for row in operations if row['kind'] == 'add_task']
        self.assertEqual(1, len(additions))
        self.apply(operations, 'supervisor')
        self.assertEqual(([], True), self.consume())
        request = next(iter(self.app['watchdog']['episodes'].values()))['request']
        self.assertEqual('provider_authentication_failed', request['target_result']['error_code'])
        self.run_one()
        operations, owns = self.consume()
        self.assertTrue(owns)
        fresh = next(row for row in operations if row['kind'] == 'new_attempt')
        from modport.payload_storage import unpack_input
        payload = unpack_input(self.root, fresh['command']['payload'])
        self.assertEqual('source', fresh['task_id'])
        self.assertEqual('run:source:2', fresh['command']['execution_id'])
        self.assertEqual('Restore the provider configuration, then rerun the original assignment.',
                         payload['payload']['watchdog_recovery']['instruction'])
        self.assertEqual(self.header['deadline_epoch'], payload['options']['deadline_epoch'])

    def test_success_and_user_cancel_do_not_dispatch_repair_supervisor(self):
        self.source.status = 'completed'
        self.run_one()
        self.deliver()
        self.assertEqual(([], False), self.consume())
        self.app['user_cancelled'] = True
        accept_notification(self.root, {'notification_id': 'cancelled-check', 'run_id': 'run',
            'kind': 'terminal', 'task_id': 'source', 'execution_id': 'run:source:1'})
        self.assertEqual(([], False), self.consume())

    def test_duplicate_and_stale_notification_cannot_create_another_episode(self):
        event = {'notification_id': 'old-attempt', 'run_id': 'run', 'generation': 0,
                 'kind': 'terminal', 'task_id': 'source', 'execution_id': 'run:source:0'}
        first = accept_notification(self.root, event)
        self.assertEqual(first, accept_notification(self.root, event))
        self.assertEqual(1, len(list(notifications(self.root, 'run'))))
        self.assertEqual(([], False), self.consume())
        self.assertEqual(([], False), self.consume())
        self.assertEqual({}, self.app['watchdog']['episodes'])

    def test_failed_sdk_attempt_raw_error_is_supplied_to_supervisor(self):
        snapshot = self.snapshot()
        attempt = snapshot['tasks']['source']['attempts'][-1]
        attempt.update(state='failed', error={'code': 'handler_process_exit', 'message': 'Raw worker crash'},
                       result={'error': {'code': 'handler_process_exit', 'details': {'exitcode': -9}}})
        operations = begin(self.owner, snapshot, self.header, self.app, {
            'incident_id': 'raw-crash', 'kind': 'terminal', 'reason': 'worker failed',
            'target_task_id': 'source', 'target_execution_id': 'run:source:1'})
        self.assertTrue(operations)
        request = self.app['watchdog']['episodes']['raw-crash']['request']
        import json
        reference = next(ref for ref in request['evidence_refs']
                         if ref['path'].endswith('/target-attempt.json'))
        retained = json.loads((self.root / reference['path']).read_text())
        self.assertEqual(attempt['error'], retained['error'])
        self.assertEqual(attempt['result'], retained['result'])
        self.assertEqual('failed', retained['state'])

    def test_failed_business_coder_is_the_failure_intercept_target(self):
        self.run_one()
        snapshot = self.snapshot()
        old = deepcopy(self.app)
        old['active_stage'] = None
        old['active_group'] = {'kind': 'development', 'members': ['source']}
        snapshot['application_state'] = old
        app = deepcopy(old)
        app.update(active_group=None, terminal_reason='provider_authentication_failed')
        operations = intercept_failure(self.owner, snapshot, self.header,
            [{'kind': 'finish', 'state': 'failed'}], app)
        self.assertTrue(any(row['kind'] == 'dispatch' for row in operations))
        request = next(iter(app['watchdog']['episodes'].values()))['request']
        self.assertEqual('source', request['target_task_id'])

    def test_invalid_supervisor_has_no_repair_authority_and_bounded_retry(self):
        self.run_one()
        snapshot = self.snapshot()
        operations = begin(self.owner, snapshot, self.header, self.app, {
            'incident_id': 'invalid-decision', 'kind': 'terminal', 'reason': 'provider failed',
            'target_task_id': 'source', 'target_execution_id': 'run:source:1'})
        self.apply(operations, 'supervisor')
        self.supervisor.invalid = True
        self.run_one()
        self.assertEqual(([], True), self.consume())
        self.assertEqual(([], True), self.consume())
        self.now += 61
        operations, owns = self.consume()
        self.assertTrue(owns)
        self.assertEqual(['watchdog.g0.1'], [row['task_id'] for row in operations if row['kind'] == 'new_attempt'])
        self.assertFalse(any(row.get('task_id') == 'source' for row in operations))

    def test_delayed_supervisor_decision_cannot_repair_superseding_sdk_attempt(self):
        self.run_one()
        operations = begin(self.owner, self.snapshot(), self.header, self.app, {
            'incident_id': 'delayed-repair', 'kind': 'terminal', 'reason': 'provider failed',
            'target_task_id': 'source', 'target_execution_id': 'run:source:1'})
        self.apply(operations, 'supervisor')
        self.run_one()
        fresh = self.owner._schedule(self.snapshot(), self.header, self.app, 'source', dependencies=[])
        self.apply(fresh, 'independent-new-attempt')
        operations, owns = self.consume()
        self.assertEqual([], operations)
        self.assertFalse(owns)
        self.assertEqual('superseded', self.app['watchdog']['episodes']['delayed-repair']['status'])
        self.assertEqual('run:source:2', self.snapshot()['tasks']['source']['attempts'][-1]['command']['execution_id'])

    def test_active_target_incident_persists_readable_progress_baseline(self):
        from modport.evidence import read_json
        operations = begin(self.owner, self.snapshot(), self.header, self.app, {
            'incident_id': 'active-stall', 'kind': 'stalled', 'reason': 'No model responses',
            'target_task_id': 'source', 'target_execution_id': 'run:source:1'})
        self.assertTrue(any(row['kind'] == 'dispatch' for row in operations))
        episode = self.app['watchdog']['episodes']['active-stall']
        baseline = read_json(self.root / episode['baseline'])
        self.assertIsNotNone(baseline)
        self.assertEqual('run:source:1', baseline['identity']['command_id'])

    def test_failed_author_is_exposed_in_actual_supervisor_rework_transport(self):
        from modport.contracts import OperationInput
        from modport.payload_storage import unpack_input
        from modport.rework_tools import is_interactive_review, prepare_session
        self.run_one()
        author_operations = self.owner._schedule(self.snapshot(), self.header, self.app,
                                                 'mod_analysis', dependencies=[])
        self.apply(author_operations, 'author')
        self.run_one()
        operations = begin(self.owner, self.snapshot(), self.header, self.app, {
            'incident_id': 'author-failed', 'kind': 'terminal', 'reason': 'Author provider failed',
            'target_task_id': 'mod_analysis', 'target_execution_id': 'run:mod_analysis:1'})
        added = next(row for row in operations if row['kind'] == 'add_task')
        operation = OperationInput.from_dict(unpack_input(self.root, added['command']['payload']))
        self.assertEqual('failed', operation.upstream_results['mod_analysis']['status'])
        self.assertEqual('run:mod_analysis:1', operation.upstream_results['mod_analysis']['command_id'])
        targets = operation.payload['review_rework_targets']
        self.assertEqual(['mod_analysis'], [target['target_agent'] for target in targets])
        self.assertTrue(is_interactive_review(operation))
        session = prepare_session(operation, self.root / operation.options['workspace'], 120)
        from modport.evidence import read_json
        self.assertEqual(targets, read_json(session)['targets'])

        self.apply(operations, 'diagnose-author')
        self.supervisor.invalid = True
        self.run_one()
        self.assertEqual(([], True), self.consume())
        self.now += 61
        retried, owns = self.consume()
        self.assertTrue(owns)
        fresh = next(row for row in retried if row['kind'] == 'new_attempt')
        retry = OperationInput.from_dict(unpack_input(self.root, fresh['command']['payload']))
        self.assertEqual(targets, retry.payload.get('review_rework_targets'))

    def test_authorized_cancellation_keeps_nonterminal_recovery_owned(self):
        self.run_one()
        operations = begin(self.owner, self.snapshot(), self.header, self.app, {
            'incident_id': 'exact-cancellation', 'kind': 'stalled', 'reason': 'No model responses',
            'notification': {'kind': 'stalled', 'disposition': {}},
            'target_task_id': 'source', 'target_execution_id': 'run:source:1'})
        self.apply(operations, 'supervisor')
        self.run_one()
        snapshot = self.snapshot()
        snapshot['tasks']['source']['attempts'][-1]['state'] = 'recovery_required'
        episode = self.app['watchdog']['episodes']['exact-cancellation']
        episode['status'] = 'cancellation_authorized'
        operations, owns = decide(self.owner, snapshot, self.header, self.app, self.sdk)
        self.assertEqual([], operations)
        self.assertTrue(owns)
        self.assertEqual('cancelling', episode['status'])
        self.assertEqual('repair_resume', episode['decision']['action'])
        snapshot['application_state'] = self.app
        snapshot['waits'] = {'uncertain-effect': {'state': 'open'}}
        self.assertFalse(self.owner._may_leave_waiting_run(snapshot))


if __name__ == '__main__':
    unittest.main()
