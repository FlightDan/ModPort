"""Current supervisor routing through actual public SDK dispatch/cancellation."""
from dataclasses import dataclass
import json
from pathlib import Path
import tempfile
import time
import unittest

from dispatcher_sdk.orchestrator import Orchestrator, OrchestratorHost
from modport.application_state_storage import hydrate_run_snapshot
from modport.contracts import OperationResult
from modport.evidence import atomic_json
from modport.kernel_runtime import open_runtime
from modport.models import Budget, MigrationRequest
from modport.operations import MigrationOperations
from modport.workflow import compile_migration_workflow, WORKFLOW_VERSION


@dataclass
class WorkingAuthor:
    __execution_kernel_revision__ = 'progress-working-author-v1'

    def __call__(self, command):
        root = Path(command.run_dir)
        (root / 'author-started').write_text(command.command_id)
        while not (root / 'release-author').exists():
            time.sleep(0.02)
        return OperationResult('completed', command.run_id, command.task_id,
                               command.stage_id, command.command_id)


@dataclass
class ReviewingSupervisor:
    __execution_kernel_revision__ = 'progress-reviewing-supervisor-v1'

    def __call__(self, command):
        root = Path(command.run_dir)
        request = command.payload['progress_supervision']
        (root / 'supervisor-entered.json').write_text(json.dumps(request))
        decision = (root / 'supervisor-choice').read_text()
        return OperationResult('completed', command.run_id, command.task_id,
            command.stage_id, command.command_id,
            outputs={'progress_supervisor_decision': {
                **{key: request[key] for key in ('review_id', 'target_task_id', 'target_execution_id')},
                'decision': decision, 'reason': 'Inspected fixture source and execution records'}})


class ProgressSupervisionSDKTests(unittest.TestCase):
    def test_live_work_idle_review_continue_and_sdk_cancel(self):
        self.check_live_work_idle_review_and_settlement('tick')

    def test_supervisor_termination_explicit_recovery_settles_effect_and_task(self):
        self.check_live_work_idle_review_and_settlement('recover')

    def check_live_work_idle_review_and_settlement(self, settlement):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / 'baseline/.modport/tests').mkdir(parents=True)
            source = root / 'baseline/.modport/tests/Test.java'
            source.write_text('class Test { int value = 1; }')
            (root / 'worktree').mkdir()
            (root / 'supervisor-choice').write_text('continue')
            request = MigrationRequest('probe', 'https://example.invalid/probe.git', '1.20.1', '1.21.1',
                workflow_mode='artifact_verification',
                budget=Budget(max_seconds=100_000, max_agent_assignments=8, max_rework_rounds=0))
            definition = compile_migration_workflow(request).to_dict()
            self.assertEqual(WORKFLOW_VERSION, definition['workflow_version'])
            now = [time.time()]
            handlers = {'modport.contract_draft': WorkingAuthor(), 'modport.supervisor': ReviewingSupervisor()}
            owner = MigrationOperations(handlers=handlers, isolation_mode='process', clock=lambda: now[0])
            with open_runtime(root, handlers=handlers, isolation_mode='process',
                              now=time.time, memory_policy=owner.memory_policy) as runtime:
                sdk = Orchestrator(root / 'orchestrator.sqlite3', runtime.kernel, runtime=runtime)
                host = OrchestratorHost(sdk, worker_count=3)
                try:
                    header = {'format_version': 2, 'run_id': 'progress-probe', 'run_dir': str(root),
                        'request': request.to_dict(), 'definition': definition,
                        'registry_revision': runtime.registry_revision, 'prior_findings': [],
                        'initial_refs': {}, 'rubric_sha256': 'host-provided',
                        'started_at': now[0], 'deadline_epoch': now[0] + 100_000}
                    atomic_json(root / 'run.json', header)
                    sdk.create_run(header['run_id'], command_id='create', input=header, definition=definition)
                    sequence = [0]

                    def read():
                        sdk.sync()
                        return hydrate_run_snapshot(root, sdk.get_run(header['run_id']))

                    def apply(snapshot, actions, app):
                        sequence[0] += 1
                        sdk.apply_operations(header['run_id'], command_id='decision-' + str(sequence[0]),
                            expected_revision=snapshot['revision'], expected_generation=snapshot['generation'],
                            operations=actions, application_state=app)
                        host.wake(header['run_id'])

                    state = read()
                    app = owner._new_application()
                    actions = owner._schedule(state, header, app, 'contract_draft', dependencies=[])
                    self.assertGreater(actions[0]['command']['timeout_seconds'], 7200)
                    self.assertEqual(100_000, actions[0]['command']['timeout_seconds'])
                    host.start()
                    apply(state, actions, app)

                    def wait_until(predicate, seconds=15):
                        end = time.monotonic() + seconds
                        while time.monotonic() < end:
                            state = read()
                            if predicate(state):
                                return state
                            host.wake(header['run_id'])
                            time.sleep(0.02)
                        self.fail('SDK probe did not reach expected state: ' + repr(state['tasks']))

                    state = wait_until(lambda _: (root / 'author-started').exists())
                    execution_id = state['tasks']['contract_draft']['attempts'][-1]['command']['execution_id']

                    def observe(advance=600):
                        now[0] += advance
                        state = read()
                        app = state['application_state']
                        actions = owner._progress_supervision_decision(state, header, app)
                        apply(state, actions, app)
                        return actions, app

                    observe(0)  # baseline
                    observe()
                    source.write_text('class Test { int value = 2; }')
                    actions, app = observe()
                    self.assertFalse(actions)
                    self.assertEqual(0, app['progress_supervision']['executions'][execution_id]['idle_windows'])
                    observe()
                    observe()
                    actions, app = observe()
                    self.assertTrue(any(a['kind'] == 'add_task' for a in actions))
                    self.assertFalse(any(a['kind'] == 'cancel' for a in actions))
                    self.assertEqual('contract_draft', app['active_stage'])
                    first_review = next(iter(app['progress_supervision']['reviews'].values()))
                    self.assertEqual(execution_id, first_review['request']['target_execution_id'])
                    self.assertEqual(3, first_review['request']['idle_windows'])
                    supervisor_id = first_review['supervisor_task_id']
                    wait_until(lambda s: s['tasks'][supervisor_id]['attempts'][-1]['state'] == 'succeeded')
                    actions, app = observe(0)
                    self.assertFalse(any(a['kind'] == 'cancel' for a in actions))
                    self.assertEqual('continue', first_review['status'] if first_review.get('status') == 'continue'
                                     else next(iter(app['progress_supervision']['reviews'].values()))['status'])

                    # Reconstruct host between observations: durable SDK state,
                    # not an in-memory timer, owns the next escalation.
                    owner = MigrationOperations(handlers=handlers, isolation_mode='process', clock=lambda: now[0])
                    (root / 'supervisor-choice').write_text('terminate')
                    observe()
                    observe()
                    actions, app = observe()
                    second = list(app['progress_supervision']['reviews'].values())[-1]
                    supervisor_id = second['supervisor_task_id']
                    wait_until(lambda s: s['tasks'][supervisor_id]['attempts'][-1]['state'] == 'succeeded')
                    # Work resumed during supervisor deliberation: reject its stale decision.
                    source.write_text('class Test { int value = 3; }')
                    actions, app = observe(0)
                    self.assertFalse(any(a['kind'] == 'cancel' for a in actions))
                    self.assertEqual('superseded_by_progress', list(app['progress_supervision']['reviews'].values())[-1]['status'])
                    observe()
                    observe()
                    actions, app = observe()
                    third = list(app['progress_supervision']['reviews'].values())[-1]
                    supervisor_id = third['supervisor_task_id']
                    wait_until(lambda s: s['tasks'][supervisor_id]['attempts'][-1]['state'] == 'succeeded')
                    actions, app = observe(0)
                    self.assertEqual(['contract_draft'], [a['task_id'] for a in actions if a['kind'] == 'cancel'])
                    end = time.monotonic() + 15
                    evidence = None
                    while time.monotonic() < end:
                        sdk.sync()
                        report = sdk.inspect_cancellation(header['run_id'], execution_id=execution_id)
                        if report.executions:
                            evidence = report.executions[0]
                            if (evidence.local_process_tree_reaped.status == 'confirmed'
                                    and evidence.command_delivered.status == 'confirmed'):
                                break
                        time.sleep(0.02)
                    self.assertIsNotNone(evidence)
                    self.assertEqual('confirmed', evidence.command_delivered.status)
                    self.assertEqual('confirmed', evidence.local_process_tree_reaped.status)
                    self.assertEqual(4, app['agent_assignments'])
                    for task in read()['tasks'].values():
                        command = task['attempts'][-1]['command']
                        self.assertEqual(header['deadline_epoch'], command['payload']['options']['deadline_epoch'])
                        self.assertLessEqual(command['timeout_seconds'], 100_000)
                    self.assertEqual('indeterminate', runtime.kernel.get_effect('modport:' + execution_id).state)
                    self.assertTrue(sdk.inspect_recoveries(header['run_id']))
                    if settlement == 'tick':
                        state = owner.tick(sdk, header)
                        state = wait_until(lambda s: s['tasks']['contract_draft']['attempts'][-1]['state'] == 'cancelled')
                        effect_state = runtime.kernel.get_effect('modport:' + execution_id).state
                        remaining_recoveries = sdk.inspect_recoveries(header['run_id'])
                    else:
                        host.stop()
                        recovery_owner = MigrationOperations(handlers=handlers, isolation_mode='process',
                                                             memory_policy=owner.memory_policy)
                        state = recovery_owner.recover(root, header['run_id']).snapshot
                        # host.stop closes its owned Runtime. Inspect the new
                        # recovery owner rather than that closed Kernel handle.
                        with recovery_owner.session(root, header['run_id'], allow_terminal_deployment=True) as (_, _, recovered_runtime, recovered_sdk):
                            effect_state = recovered_runtime.kernel.get_effect('modport:' + execution_id).state
                            remaining_recoveries = recovered_sdk.inspect_recoveries(header['run_id'])
                    self.assertEqual('cancelled', state['tasks']['contract_draft']['attempts'][-1]['state'])
                    self.assertEqual('committed', effect_state)
                    self.assertEqual([], remaining_recoveries)
                    receipt = json.loads((root / 'artifacts' / 'executions' / execution_id / 'receipt.json').read_text())
                    outcome = receipt['response']
                    self.assertEqual('failed', outcome['status'])
                    self.assertEqual('progress_supervisor_terminated', outcome['error_code'])
                    self.assertTrue(outcome['outputs']['partial_outputs_unaccepted'])
                    self.assertEqual('unknown', outcome['outputs']['external_outcome'])
                    self.assertEqual('unverified', outcome['outputs']['acceptance_status'])
                    self.assertEqual(1, len(state['tasks']['contract_draft']['attempts']))
                    self.assertEqual('class Test { int value = 3; }', source.read_text())
                finally:
                    (root / 'release-author').touch()
                    host.stop()
                    sdk.close()


if __name__ == '__main__':
    unittest.main()
