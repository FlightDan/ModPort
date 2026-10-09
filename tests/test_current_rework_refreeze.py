"""Current reviewer repairs refreeze before verification under the SDK budget."""
from dataclasses import replace
import json
import time
import unittest

from modport.contracts import OperationInput, OperationResult
from modport.execution_budget import current_deadline_budget
from modport.kernel_runtime import open_runtime
from modport.memory_admission import MemorySnapshot
from modport.models import Budget, MigrationRequest
from modport.operations import MigrationOperations
from modport.payload_storage import unpack_input
from modport.rework_orchestration import project_rework_responses
from modport.target_contract import TargetContractFreezeHandler
from modport.test_selection_execution import build_selected_test_execution
from modport.workflow import WORKFLOW_VERSION, WorkflowDefinition
import test_independent_source_reading as freeze_fixture


class CurrentReworkRefreezeTests(unittest.TestCase):
    def setUp(self):
        fixture = freeze_fixture.IndependentSourceReadingTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.root = fixture.root
        self.deadline = time.time() + 1200
        request = MigrationRequest('fixture', 'https://example.invalid/fixture.git',
            '1.21', '26.1', source_loader='neoforge',
            budget=Budget(max_seconds=1200, max_agent_assignments=20)).to_dict()
        self.header = {'run_dir': str(self.root), 'request': request,
            'definition': WorkflowDefinition(request).to_dict(),
            'initial_refs': fixture.refs, 'prior_findings': [],
            'deadline_epoch': self.deadline, 'rubric_sha256': 'host-rubric',
            'registry_revision': 'host-registry'}
        self.memory = MemorySnapshot(64 * 1024**3, 64 * 1024**3, 'test capacity')
        self.host = MigrationOperations(memory_probe=lambda: self.memory)
        self.app = self.host._new_application()
        self.app['agent_assignments'] = 1
        self.old_lock = fixture.refs['functional_contract_lock']
        self.app['effective']['target_contract_freeze'] = OperationResult(
            'completed', 'run', 'prior-freeze', 'target_contract_freeze', 'prior-freeze',
            outputs={'artifact_refs': {'functional_contract_lock': self.old_lock}}).to_dict()
        candidate_path = self.root / 'worktree/.modport/functional-contract.json'
        candidate = json.loads(candidate_path.read_text())
        candidate['test_evidence']['target.damage']['result_identity']['name'] = 'target/new_damage'
        candidate_path.write_text(json.dumps(candidate))
        options = {'workflow_version': WORKFLOW_VERSION, 'deadline_epoch': self.deadline}
        reviewer = OperationInput('run', 'review', 'code_review', 'review-exec',
            str(self.root), options=options)
        author = replace(reviewer, task_id='author', stage_id='agent_rework', command_id='author-exec')
        outcome = OperationResult('completed', 'run', 'author', 'agent_rework', 'author-exec',
            outputs={'integration_status': 'integrated', 'changed_paths': ['.modport/functional-contract.json']})
        self.snapshot = {'run_id': 'run', 'tasks': {
            'review': {'attempts': [{'state': 'running', 'command': {
                'execution_id': reviewer.command_id, 'payload': reviewer.to_dict()}}]},
            'author': {'attempts': [{'state': 'succeeded', 'command': {
                'execution_id': author.command_id, 'payload': author.to_dict()},
                'result': {'value': outcome.to_dict()}}]}}}
        self.record = {'state': 'running', 'task_id': 'author',
            'reviewer_execution_id': 'review-exec', 'target_stage': 'coder',
            'target_agent': 'coder-A', 'reviewer_stage': 'code_review',
            'request_id': 'repair', 'queue_deadline_epoch': self.deadline, 'updates': []}
        self.app['review_rework'] = {'requests': {'repair': self.record},
                                    'latest_targets': {}, 'sequence': 1}

    def next_command(self):
        operations = self.host._review_rework_decision(self.snapshot, self.header, self.app)
        added = next(row for row in operations if row['kind'] == 'add_task')
        self.assertTrue(any(row['kind'] == 'dispatch' for row in operations))
        command = OperationInput.from_dict(unpack_input(self.root, added['command']['payload']))
        self.assertEqual(self.deadline, command.options['deadline_epoch'])
        return command

    def deliver(self, command, result):
        self.snapshot['tasks'][command.task_id] = {'attempts': [{'state': 'succeeded',
            'command': {'execution_id': command.command_id, 'payload': command.to_dict()},
            'result': {'value': result.to_dict()}}]}

    def test_public_kernel_cleanup_freeze_build_consumes_fresh_lock_and_returns_failure(self):
        outer = self
        observed = []

        class Cleanup:
            __execution_kernel_revision__ = 'current-rework-cleanup-boundary'

            def __call__(self, command):
                outer.assertLessEqual(current_deadline_budget(command).effective_deadline,
                                      outer.deadline)
                observed.append(command.stage_id)
                return OperationResult('completed', command.run_id, command.task_id,
                    command.stage_id, command.command_id)

        class Verify:
            __execution_kernel_revision__ = 'current-rework-verify-boundary'

            def __call__(self, command):
                outer.assertLessEqual(current_deadline_budget(command).effective_deadline,
                                      outer.deadline)
                observed.append(command.stage_id)
                outer.assertNotEqual(outer.old_lock['path'],
                                     command.artifact_refs['functional_contract_lock']['path'])
                lock = json.loads((outer.root / command.artifact_refs['functional_contract_lock']['path']).read_text())
                outer.assertEqual('target/new_damage', lock['test_evidence']['target.damage']['result_identity']['name'])
                selected = build_selected_test_execution(lock['contract'], ['target.damage'],
                                                         workflow_version=WORKFLOW_VERSION)
                outer.assertEqual((':runGameTestServer',), selected.gradle_tasks)
                return OperationResult('failed', command.run_id, command.task_id,
                    command.stage_id, command.command_id,
                    detail='raw native runtime failure after fresh lock consumption',
                    error_code='target_behavior_failed')

        class Freeze(TargetContractFreezeHandler):
            __execution_kernel_revision__ = 'current-rework-real-target-freeze'

        handlers = {'modport.code_cleanup': Cleanup(),
                    'modport.target_contract_freeze': Freeze(),
                    'modport.target_build': Verify()}
        with open_runtime(self.root, handlers=handlers, isolation_mode='thread') as runtime:
            self.header['registry_revision'] = runtime.registry_revision
            for expected in ['code_cleanup', 'target_contract_freeze', 'target_build']:
                command = self.next_command()
                self.assertEqual(expected, command.stage_id)
                sdk_command = runtime.command('modport.' + expected,
                    execution_id=command.command_id, idempotency_key=command.command_id,
                    correlation_id=command.run_id, timeout_seconds=600, payload=command.to_dict())
                runtime.submit(sdk_command)
                result = runtime.run_once()
                self.assertEqual('succeeded', result.state)
                self.deliver(command, OperationResult.from_dict(result.result.value))
            self.assertEqual([], self.host._review_rework_decision(
                self.snapshot, self.header, self.app))
        self.assertEqual(['code_cleanup', 'target_build'], observed)
        self.assertEqual(2, self.app['agent_assignments'])
        self.assertEqual('failed', self.record['state'])
        self.assertFalse(self.record.get('lock_handoff_error'))
        project_rework_responses(self.root, self.app)
        response = json.loads((self.root / 'artifacts/rework-tools/review-exec/responses/repair.json').read_text())
        self.assertEqual('failed', response['status'])
        self.assertIn('raw native runtime failure', response['error'])
        self.assertEqual(1, self.app['effective']['target_contract_freeze']['outputs']['behavior_count'])

    def test_cleanup_capacity_wait_keeps_one_task_charge_and_original_deadline(self):
        self.memory = MemorySnapshot(128 * 1024**2, 64 * 1024**3, 'temporary pressure')
        operations = self.host._review_rework_decision(self.snapshot, self.header, self.app)
        addition = next(row for row in operations if row['kind'] == 'add_task')
        self.assertFalse(any(row['kind'] == 'dispatch' for row in operations))
        command = OperationInput.from_dict(unpack_input(self.root, addition['command']['payload']))
        self.assertEqual('code_cleanup', command.stage_id)
        self.assertEqual(self.deadline, command.options['deadline_epoch'])
        self.assertTrue(self.record['waiting_resources'])
        self.snapshot['tasks'][command.task_id] = {'attempts': [{'state': 'planned',
            'command': {'execution_id': command.command_id, 'payload': command.to_dict()}}]}
        self.assertEqual([], self.host._review_rework_decision(self.snapshot, self.header, self.app))
        self.assertEqual('code_cleanup', self.record['resource_wait']['requested_stage'])
        self.memory = MemorySnapshot(64 * 1024**3, 64 * 1024**3, 'capacity returned')
        self.assertEqual([{'kind': 'dispatch', 'task_id': command.task_id}],
                         self.host._review_rework_decision(self.snapshot, self.header, self.app))
        self.assertEqual(2, self.app['agent_assignments'])
        self.assertFalse(self.record['waiting_resources'])

    def test_compile_package_repairs_cleanup_without_runtime_refreeze(self):
        self.header['definition']['validation_policy'] = {'scope': 'compile_package'}
        cleanup = self.next_command()
        self.assertEqual('code_cleanup', cleanup.stage_id)
        self.deliver(cleanup, OperationResult('completed', cleanup.run_id, cleanup.task_id,
                                             cleanup.stage_id, cleanup.command_id))
        self.assertEqual('target_build', self.next_command().stage_id)
        self.assertEqual(2, self.app['agent_assignments'])


if __name__ == '__main__':
    unittest.main()
