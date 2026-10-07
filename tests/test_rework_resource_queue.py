"""Capacity waits retain one SDK task and one original rework request."""
from copy import deepcopy
from dataclasses import replace
import json
import os
from pathlib import Path
import unittest

from dispatcher_sdk.orchestrator import Orchestrator
from modport.contracts import OperationInput, OperationResult
from modport.evidence import file_digest
from modport.memory_admission import MemorySnapshot
from modport.payload_storage import unpack_input
from modport.rework_orchestration import project_rework_responses
from modport.rework_mcp import Session
from modport.rework_tools import interactive_review_timeout_cap, prepare_session
from modport.supervised_goals import target_key
from modport.workflow import WorkflowDefinition
import test_planning_operations


GIB = 1024 ** 3


class QueueAuthor:
    __execution_kernel_revision__ = 'resource-queue-author-v1'

    def __call__(self, payload, context):
        operation = OperationInput.from_dict(unpack_input(Path(payload['run_dir']), payload))
        marker = Path(operation.run_dir) / 'author-invocations.txt'
        with marker.open('a') as stream:
            stream.write(operation.command_id + '\n')
        return OperationResult('completed', operation.run_id, operation.task_id,
            operation.stage_id, operation.command_id).to_dict()


class ResourceQueueTests(unittest.TestCase):
    def setUp(self):
        self.fixture = test_planning_operations.PlanningPolicyTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        f = self.fixture
        self.root = Path(f.header['run_dir'])
        (self.root / 'worktree').mkdir()
        f.header['definition'] = WorkflowDefinition(f.header['request'], version=21).to_dict()
        f.header['request']['max_parallel_coders'] = 1
        options = {'workflow_version': 21, 'gate_policy': 'downstream_toolcall'}
        source = OperationInput('policy', 'coder.g1.a', 'coder', 'coder-1', str(self.root), options=options)
        result = OperationResult('completed', source.run_id, source.task_id, source.stage_id, source.command_id)
        caller = OperationInput('policy', 'code_review', 'code_review', 'review-1', str(self.root),
            options=options, payload={'review_rework_targets': [{'target_agent': source.task_id}]},
            upstream_results={source.task_id: result.to_dict()})
        f.snapshot['tasks'] = {
            source.task_id: {'attempts': [{'state': 'succeeded',
                'command': {'execution_id': source.command_id, 'payload': source.to_dict()},
                'result': {'value': result.to_dict()}}]},
            caller.task_id: {'attempts': [{'state': 'running',
                'command': {'execution_id': caller.command_id, 'payload': caller.to_dict()}}]}}
        self.session = prepare_session(caller, self.root / 'worktree', 60)
        self.request_id = 'capacity-request'
        self.record_key = caller.command_id + '/' + self.request_id
        self.request = self.session.parent / 'requests' / (self.request_id + '.json')
        self.request.parent.mkdir(exist_ok=True)
        self.request.write_text(json.dumps({'request_id': self.request_id, 'run_id': 'policy',
            'reviewer_execution_id': caller.command_id, 'target_agent': source.task_id,
            'instructions': 'Fix the concrete defect.'}))
        self.memory = MemorySnapshot(GIB, 8 * GIB, 'fixture')
        f.operations.memory_probe = lambda: self.memory

    def decide(self):
        f = self.fixture
        return f.operations._review_rework_decision(f.snapshot, f.header, f.app)

    def enqueue(self):
        changes = self.decide()
        self.assertEqual(['add_task'], [op['kind'] for op in changes])
        addition = changes[0]
        self.fixture.snapshot['tasks'][addition['task_id']] = {'attempts': [{
            'state': 'planned', 'command': addition['command']}]}
        return addition

    def test_same_request_waits_without_response_or_repeated_budget_charge(self):
        addition = self.enqueue()
        initial = deepcopy(self.fixture.app)
        for _ in range(3):
            self.assertEqual([], self.decide())
            self.assertEqual(initial, self.fixture.app)
        project_rework_responses(self.root, self.fixture.app)
        self.assertFalse((self.session.parent / 'responses' / self.request.name).exists())
        self.memory = MemorySnapshot(8 * GIB, 8 * GIB, 'fixture')
        self.assertEqual([{'kind': 'dispatch', 'task_id': addition['task_id']}], self.decide())
        self.assertEqual(1, self.fixture.app['agent_assignments'])
        self.assertEqual(1, self.fixture.app['rounds']['review_rework:coder.g1.a'])
        self.assertEqual(1, len(self.fixture.app['review_rework']['requests']))
        self.assertEqual(1, len(self.fixture.snapshot['tasks'][addition['task_id']]['attempts']))

    def test_v26_author_deadline_uses_tool_response_window(self):
        now = self.fixture.operations.clock()
        descriptor = json.loads(self.session.read_text())
        descriptor['deadline_epoch'] = now + 3600
        self.session.write_text(json.dumps(descriptor))
        request = json.loads(self.request.read_text())
        request['response_deadline_epoch'] = now + 3000
        self.request.write_text(json.dumps(request))
        self.fixture.header['definition'] = WorkflowDefinition(
            self.fixture.header['request'], version=26).to_dict()
        addition = self.enqueue()
        self.assertGreater(addition['command']['payload']['options']['deadline_epoch'],
                           now + 1200)
        self.assertLessEqual(addition['command']['payload']['options']['deadline_epoch'],
                             now + 3000)

    def test_v26_session_producer_passes_version_to_mcp_consumer(self):
        caller = OperationInput('policy', 'contract_review', 'contract_review',
            'review-v26', str(self.root), options={'workflow_version': 26,
                'business_gates_disabled': True}, payload={'review_rework_targets': [
                    {'target_agent': 'contract_restore', 'stage': 'contract_restore',
                     'description': 'Repair the restored harness'}]})
        path = prepare_session(caller, self.root / 'worktree', 3600)
        self.assertEqual(26, json.loads(path.read_text())['workflow_version'])
        self.assertEqual(26, Session.load(path).workflow_version)

    def test_v26_interactive_reviewer_keeps_shared_run_window_for_rework(self):
        f = self.fixture
        f.header['definition'] = WorkflowDefinition(f.header['request'], version=26).to_dict()
        now = f.operations.clock()
        f.header['deadline_epoch'] = now + 4 * 3600
        f.app['effective']['coder'] = f.snapshot['tasks']['coder.g1.a']['attempts'][0]['result']['value']
        changes = f.operations._schedule(f.snapshot, f.header, f.app,
            'code_review', dependencies=['coder.g1.a'])
        self.assertIn(changes[0]['kind'], {'add_task', 'new_attempt'})
        sdk_command = changes[0]['command']
        reviewer = OperationInput.from_dict(unpack_input(self.root, sdk_command['payload']))
        self.assertTrue(reviewer.payload['review_rework_targets'])
        self.assertGreater(sdk_command['timeout_seconds'], 7200)
        self.assertLessEqual(sdk_command['timeout_seconds'], 4 * 3600)
        self.assertGreater(interactive_review_timeout_cap(reviewer, now=now), 7200)

        ordinary = replace(reviewer, payload={**reviewer.payload,
            'review_rework_targets': []})
        historical = replace(reviewer, options={**reviewer.options, 'workflow_version': 25})
        self.assertEqual(7200, interactive_review_timeout_cap(ordinary, now=now))
        self.assertEqual(7200, interactive_review_timeout_cap(historical, now=now))

    def test_v26_contract_followup_excludes_old_lock_and_uses_response_deadline(self):
        f = self.fixture
        f.header['definition'] = WorkflowDefinition(f.header['request'], version=26).to_dict()
        old_lock = self.root / 'artifacts/old-contract-lock.json'
        old_lock.parent.mkdir(parents=True, exist_ok=True)
        old_lock.write_text('{}')
        old_ref = {'path': 'artifacts/old-contract-lock.json',
                   'sha256': file_digest(old_lock), 'media_type': 'application/json'}
        f.app['effective']['contract_freeze'] = {'status': 'completed',
            'command_id': 'old-freeze', 'outputs': {'artifact_refs': {
                'functional_contract_lock': old_ref}}}
        f.app['effective']['contract_review'] = {'status': 'completed',
            'command_id': 'old-review', 'outputs': {}}
        f.app['locked_artifacts']['contract_lock_sha256'] = 'old-lock'
        deadline = f.operations.clock() + 30
        operations = f.operations._schedule(f.snapshot, f.header, f.app,
            'contract_review', task_id='agent-rework.late.verify.review',
            dependencies=['agent-rework.late.verify'],
            payload={'reviewer_rework': {'request_id': 'late'}},
            extra_options={'deadline_epoch': deadline})
        self.assertEqual('add_task', operations[0]['kind'])
        command = operations[0]['command']
        scheduled = OperationInput.from_dict(unpack_input(self.root, command['payload']))
        self.assertNotIn('contract_freeze', scheduled.upstream_results)
        self.assertNotIn('contract_review', scheduled.upstream_results)
        self.assertNotIn('functional_contract_lock', scheduled.artifact_refs)
        self.assertNotIn('contract_lock_sha256', scheduled.payload['locked_artifacts'])
        self.assertEqual(deadline, scheduled.options['deadline_epoch'])
        self.assertLessEqual(command['timeout_seconds'], 30)

    def test_v26_rework_dispatch_binds_revision_published_after_original_coder(self):
        self.fixture.header['definition'] = WorkflowDefinition(
            self.fixture.header['request'], version=26).to_dict()
        task = {'id': 'a', 'objective': 'Preserve the source behavior.'}
        plan = self.root / 'artifacts' / 'source-plan.json'
        plan.parent.mkdir(parents=True, exist_ok=True)
        plan.write_text('{"tasks": []}', encoding='utf-8')
        plan_ref = {'path': plan.relative_to(self.root).as_posix(),
                    'sha256': file_digest(plan)}
        revision = self.root / 'artifacts' / 'late-revision.json'
        revision.write_text('{"revision": "late"}', encoding='utf-8')
        revision_ref = {'path': revision.relative_to(self.root).as_posix(),
                        'sha256': file_digest(revision)}
        attempt = self.fixture.snapshot['tasks']['coder.g1.a']['attempts'][0]
        original = OperationInput.from_dict(attempt['command']['payload'])
        original = replace(original, payload={**original.payload, 'development_task': task},
                           artifact_refs={**original.artifact_refs,
                                          'development_plan': plan_ref})
        attempt['command']['payload'] = original.to_dict()
        key = target_key(task, plan_ref)
        self.fixture.app.setdefault('supervision', {})['goal_revisions'] = {
            key: {'revision_ref': revision_ref, 'window_end': 5}}
        changes = self.decide()
        addition = next(change for change in changes if change['kind'] == 'add_task')
        child = OperationInput.from_dict(unpack_input(
            str(self.root), addition['command']['payload']))
        self.assertEqual('agent_rework', child.stage_id)
        self.assertNotIn('supervised_goal_revision', original.artifact_refs)
        self.assertEqual(revision_ref, child.artifact_refs['supervised_goal_revision'])

    def test_queue_expiry_cancels_planned_task_without_running_author(self):
        addition = self.enqueue()
        deadline = self.fixture.app['review_rework']['requests'][self.record_key]['queue_deadline_epoch']
        self.fixture.operations.clock = lambda: deadline + 1
        changes = self.decide()
        self.assertEqual(['cancel'], [op['kind'] for op in changes])
        self.assertEqual(addition['task_id'], changes[0]['task_id'])
        self.assertIn('deadline', changes[0]['reason'])
        self.assertEqual('failed', self.fixture.app['review_rework']['requests'][self.record_key]['state'])
        self.assertFalse((self.root / 'author-invocations.txt').exists())

    def test_already_expired_response_is_rejected_before_budget_or_task_creation(self):
        request = json.loads(self.request.read_text())
        request['response_deadline_epoch'] = self.fixture.operations.clock() - 1
        self.request.write_text(json.dumps(request))
        self.assertEqual([], self.decide())
        self.assertEqual(0, self.fixture.app['agent_assignments'])
        self.assertEqual({}, self.fixture.app['rounds'])
        record = self.fixture.app['review_rework']['requests'][self.record_key]
        self.assertEqual('failed', record['state'])
        self.assertIn('deadline', record['error'])

    def test_advisory_caller_completion_does_not_revoke_queued_request(self):
        addition = self.enqueue()
        self.fixture.snapshot['tasks']['code_review']['attempts'][0]['state'] = 'succeeded'
        self.memory = MemorySnapshot(8 * GIB, 8 * GIB, 'fixture')
        self.assertEqual([{'kind': 'dispatch', 'task_id': addition['task_id']}], self.decide())

    def test_deadline_crossed_during_probe_rejects_before_schedule_or_charge(self):
        now = self.fixture.operations.clock()
        self.fixture.operations.clock = lambda: now
        request = json.loads(self.request.read_text())
        request['response_deadline_epoch'] = now + 1
        self.request.write_text(json.dumps(request))

        def slow_probe():
            self.fixture.operations.clock = lambda: now + 2
            return MemorySnapshot(8 * GIB, 8 * GIB, 'fixture')

        self.fixture.operations.memory_probe = slow_probe
        self.assertEqual([], self.decide())
        self.assertEqual(0, self.fixture.app['agent_assignments'])
        self.assertEqual({}, self.fixture.app['rounds'])
        record = self.fixture.app['review_rework']['requests'][self.record_key]
        self.assertEqual('failed', record['state'])
        self.assertIn('deadline', record['error'])

    def test_deadline_crossed_during_recovery_probe_cancels_same_planned_task(self):
        addition = self.enqueue()
        record = self.fixture.app['review_rework']['requests'][self.record_key]
        deadline = record['queue_deadline_epoch']
        self.fixture.operations.clock = lambda: deadline - 1

        def slow_probe():
            self.fixture.operations.clock = lambda: deadline + 1
            return MemorySnapshot(8 * GIB, 8 * GIB, 'fixture')

        self.fixture.operations.memory_probe = slow_probe
        changes = self.decide()
        self.assertEqual(['cancel'], [op['kind'] for op in changes])
        self.assertEqual(addition['task_id'], changes[0]['task_id'])
        self.assertEqual('failed', record['state'])
        self.assertFalse(record['waiting_resources'])
        self.assertIn('deadline', record['error'])
        self.assertEqual(1, self.fixture.app['agent_assignments'])

    def test_actual_reviewer_stages_wait_for_memory_with_free_coder_slot(self):
        for stage in ('contract_review', 'code_review', 'test_review', 'gap_review'):
            with self.subTest(stage=stage):
                snapshot = deepcopy(self.fixture.snapshot)
                snapshot['tasks']['code_review']['attempts'][0]['command']['payload']['stage_id'] = stage
                self.memory = MemorySnapshot(int(2.9 * GIB), 8 * GIB, 'fixture')
                app = deepcopy(self.fixture.app)
                self.assertEqual(0, self.fixture.operations._memory_capacity(
                    snapshot, self.fixture.header, app, 'rework',
                    exclude_execution_ids=('review-1',), retained_memory_execution_ids=('review-1',)))
                self.assertEqual('memory_pressure', app['memory_waits']['rework']['reason'])
                self.memory = MemorySnapshot(4 * GIB, 8 * GIB, 'fixture')
                self.assertEqual(1, self.fixture.operations._memory_capacity(
                    snapshot, self.fixture.header, app, 'rework',
                    exclude_execution_ids=('review-1',), retained_memory_execution_ids=('review-1',)))

    def test_explicit_cancellation_retains_original_request_and_error(self):
        self.enqueue()
        self.request.with_name(self.request_id + '.cancel.json').write_text('{}')
        self.assertEqual(['cancel'], [op['kind'] for op in self.decide()])
        project_rework_responses(self.root, self.fixture.app)
        response = json.loads((self.session.parent / 'responses' / self.request.name).read_text())
        self.assertEqual('failed', response['status'])
        self.assertTrue(self.request.exists())

    def test_run_cancellation_settles_queued_ledger_and_sdk_task(self):
        addition = self.enqueue()
        changes, app = self.fixture.operations._decision(
            self.fixture.snapshot, self.fixture.header, stop_reason='user_cancelled')
        self.assertTrue(any(op['kind'] == 'cancel' and op['task_id'] == addition['task_id']
                            for op in changes))
        record = app['review_rework']['requests'][self.record_key]
        self.assertEqual('failed', record['state'])
        self.assertEqual('user_cancelled', record['error'])

    def test_arrival_order_precedes_uuid_order(self):
        earlier = self.request.with_name('z-earlier.json')
        request = json.loads(self.request.read_text())
        request['request_id'] = 'z-earlier'
        earlier.write_text(json.dumps(request))
        arrived = self.request.stat().st_mtime_ns
        os.utime(earlier, ns=(arrived - 1000000, arrived - 1000000))
        addition = self.enqueue()
        self.assertEqual('agent-rework.z-earlier', addition['task_id'])

    def test_waiting_coder_retains_memory_but_yields_single_concurrency_slot(self):
        f = self.fixture
        caller = f.snapshot['tasks']['code_review']['attempts'][0]['command']['payload']
        caller['stage_id'] = 'coder'
        self.enqueue()
        self.memory = MemorySnapshot(3 * GIB, 8 * GIB, 'fixture')
        self.assertEqual([], self.decide())  # parent + child still need real memory
        self.memory = MemorySnapshot(8 * GIB, 8 * GIB, 'fixture')
        self.assertEqual(['dispatch'], [op['kind'] for op in self.decide()])

    def test_real_sdk_queue_survives_reopen_and_executes_once(self):
        database = self.root / 'queue-sdk.sqlite3'
        handlers = {'modport.agent_rework': QueueAuthor()}
        with Orchestrator.open_sqlite(database, handlers, isolation_mode='thread') as sdk:
            self.fixture.header['registry_revision'] = sdk.runtime.registry_revision
            sdk.create_run('policy', command_id='create')
            addition = self.enqueue()
            state = sdk.apply_operations('policy', command_id='queue', expected_revision=0,
                operations=[addition], application_state=self.fixture.app)
            self.assertEqual('planned', state['tasks'][addition['task_id']]['attempts'][0]['state'])
            self.assertEqual(0, sdk.flush())
            self.assertFalse((self.root / 'author-invocations.txt').exists())
        with Orchestrator.open_sqlite(database, handlers, isolation_mode='thread') as sdk:
            state = sdk.get_run('policy')
            self.fixture.snapshot['application_state'] = state['application_state']
            self.fixture.snapshot['tasks'][addition['task_id']] = state['tasks'][addition['task_id']]
            self.memory = MemorySnapshot(8 * GIB, 8 * GIB, 'fixture')
            changes = self.decide()
            self.assertEqual([{'kind': 'dispatch', 'task_id': addition['task_id']}], changes)
            sdk.apply_operations('policy', command_id='release', expected_revision=state['revision'],
                operations=changes, application_state=self.fixture.app)
            sdk.flush()
            sdk.runtime.run_once()
            sdk.sync()
            state = sdk.get_run('policy')
            attempts = state['tasks'][addition['task_id']]['attempts']
            self.assertEqual(1, len(attempts))
            self.assertEqual('succeeded', attempts[0]['state'])
            self.assertEqual([addition['command']['execution_id']],
                             (self.root / 'author-invocations.txt').read_text().splitlines())


if __name__ == '__main__':
    unittest.main()
