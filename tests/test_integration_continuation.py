"""Explicit successors rebind retained merges without replaying SDK task state."""

from copy import deepcopy
from dataclasses import replace
from pathlib import Path
import tempfile
import time
import unittest

from dispatcher_sdk.orchestrator import Orchestrator, Operations

from modport import integration_repair
from modport.application_state_storage import hydrate_run_snapshot
from modport.continuation import continue_from_planner, prepare_application
from modport.contracts import OperationInput, OperationResult
from modport.evidence import atomic_json
from modport.kernel_runtime import open_runtime
from modport.memory_admission import MemorySnapshot
from modport.models import Budget, MigrationRequest
from modport.operations import MigrationOperations
from modport.payload_storage import unpack_input
from modport.sdk_compat import SDK_VERSION
from modport.workflow import WORKFLOW_VERSION, WorkflowDefinition


class PrepareOnlyHandler:
    __execution_kernel_revision__ = 'integration-continuation-test'

    def __call__(self, _command):
        raise AssertionError('continuation preparation must not execute assignments')


class IntegrationContinuationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        request = MigrationRequest('fixture', 'https://example.invalid/fixture.git',
            '1.20.1', '26.1.2', budget=Budget(max_seconds=3600,
                                             max_agent_assignments=10)).to_dict()
        definition = WorkflowDefinition(request).to_dict()
        self.handlers = {row['handler_id']: PrepareOnlyHandler() for row in definition['stages']}
        self.owner = MigrationOperations(handlers=self.handlers, isolation_mode='thread',
            memory_probe=lambda: MemorySnapshot(64 * 1024**3, 64 * 1024**3, 'test capacity'))
        self.header = {'format_version': 2, 'run_id': 'previous', 'logical_run_id': 'logical',
            'run_dir': str(self.root), 'request': request, 'definition': definition,
            'sdk_version': SDK_VERSION, 'sdk_identity': {}, 'initial_refs': {},
            'prior_findings': [], 'rubric_sha256': 'host-provenance',
            'started_at': time.time(), 'deadline_epoch': time.time() + 3600,
            'registry_revision': 'fixture', 'watchdog_policy': {'enabled': False}}
        self.app = self.owner._new_application()
        self.app['agent_assignments'] = 3
        atomic_json(self.root / 'original-plan.json', {})
        scheduled = self.owner._schedule({'run_id': 'previous', 'tasks': {}},
            self.header, self.app, 'development_integrate', dependencies=[],
            artifact_overrides={'development_plan': {'path': 'original-plan.json'}},
            payload={'development_generation': 1, 'development_base': 'a' * 40,
                     'development_results': [], 'goal_scope': 'migration'},
            extra_options={'workspace': 'worktree'})
        self.original_sdk = scheduled[0]['command']
        self.original = OperationInput.from_dict(unpack_input(self.root, self.original_sdk['payload']))
        atomic_json(self.root / 'original-command.json', self.original.to_dict())
        self.merge_ref = {'path': 'merge.json'}
        atomic_json(self.root / 'merge.json', {
            'source_workspace': 'worktree', 'workspace': 'workspaces/integration-merges/original',
            'start': 'b' * 40, 'merge_head': 'c' * 40,
            'conflicts': [{'task_id': 'feature', 'paths': ['feature.txt'],
                           'patch_path': 'feature.patch', 'detail': 'CONFLICT: overlapping edits'}],
            'original_task_ids': ['feature'], 'patch_refs': [{'path': 'feature.patch'}],
            'plan_ref': self.original.artifact_refs['development_plan'],
            'command_ref': {'path': 'original-command.json'}})
        self.outcome = OperationResult('failed', self.original.run_id, self.original.task_id,
            self.original.stage_id, self.original.command_id, error_code='integration_merge_required',
            outputs={'integration_merge_ref': self.merge_ref})
        self.app['effective']['development_integrate'] = self.outcome.to_dict()
        self.app['history'].append({'execution_id': self.original.command_id})
        self.snapshot = {'run_id': 'previous', 'state': 'failed', 'revision': 4,
            'input': self.header, 'waits': {}, 'tasks': {self.original.task_id: {'attempts': [{
                'state': 'succeeded', 'command': self.original_sdk,
                'result': {'value': self.outcome.to_dict()}}]}}}
        repair_operations = integration_repair.start_or_resume(self.owner, self.snapshot,
            self.header, self.app, outcome=self.outcome)
        self.coder_sdk = next(op['command'] for op in repair_operations if op['kind'] == 'add_task')
        self.coder = OperationInput.from_dict(unpack_input(self.root, self.coder_sdk['payload']))
        self.snapshot['tasks'][self.coder.task_id] = {'attempts': [{
            'state': 'cancelled', 'command': self.coder_sdk}]}
        self.app['terminal_reason'] = 'user_cancelled'
        self.snapshot['application_state'] = self.app

    def prepare_and_schedule(self, *, start_stage=None, expected_assignments=4):
        before = deepcopy(self.snapshot)
        app = prepare_application(self.root, self.snapshot, start_stage=start_stage,
                                  target_workflow_version=WORKFLOW_VERSION)
        header = {**self.header, 'run_id': 'successor'}
        operations = self.owner._advance_without_business_gates(
            {'run_id': 'successor', 'tasks': {}, 'waits': {}}, header, app)
        self.assertEqual(before, self.snapshot)
        scheduled = [op for op in operations if op['kind'] in {'add_task', 'new_attempt'}]
        self.assertEqual(1, len(scheduled))
        command = OperationInput.from_dict(unpack_input(self.root, scheduled[0]['command']['payload']))
        self.assertEqual(self.header['deadline_epoch'], command.options['deadline_epoch'])
        self.assertEqual(expected_assignments, app['agent_assignments'])
        self.assertEqual('logical', command.run_id)
        self.assertNotEqual(self.original.command_id, command.command_id)
        return app, command

    def test_cancelled_running_repair_replays_integration_in_fresh_successor(self):
        app, command = self.prepare_and_schedule()
        self.assertEqual('development_integrate', command.stage_id)
        self.assertNotIn('integration_repair', app)
        self.assertEqual('running', app['integration_repair_history'][-1]['status'])
        self.assertNotIn('integration_resolution', command.payload)

    def test_failed_repair_replays_integration_before_explicit_unrelated_stage(self):
        self.app['integration_repair'].update(status='failed',
            error_code='sdk_execution_identity_invalid', detail='exact execution identity rejected')
        app, command = self.prepare_and_schedule(start_stage='code_cleanup')
        self.assertEqual('development_integrate', command.stage_id)
        self.assertEqual('sdk_execution_identity_invalid',
                         app['integration_repair_history'][-1]['error_code'])

    def later_contract_failure(self):
        later = replace(self.original, task_id='target_contract_freeze',
                        stage_id='target_contract_freeze',
                        command_id='previous:target_contract_freeze:1', payload={})
        failure = OperationResult('failed', later.run_id, later.task_id, later.stage_id,
                                  later.command_id, error_code='target_contract_invalid',
                                  detail='worktree/.modport/functional-contract.json is missing')
        self.snapshot['tasks'][later.task_id] = {'attempts': [{'state': 'succeeded',
            'command': {'execution_id': later.command_id, 'payload': later.to_dict()},
            'result': {'value': failure.to_dict()}}]}
        self.app['effective'][later.stage_id] = failure.to_dict()
        self.app['history'].append({'execution_id': later.command_id})

    def test_downstream_continuation_revisits_unrepaired_failed_integration(self):
        self.app.pop('integration_repair')
        self.later_contract_failure()
        for stage in ('code_cleanup', 'target_contract_freeze', 'target_build'):
            with self.subTest(start_stage=stage):
                app, command = self.prepare_and_schedule(start_stage=stage)
                self.assertEqual('development_integrate', command.stage_id)
                self.assertEqual(self.original.payload['development_results'],
                                 command.payload['development_results'])
                self.assertEqual(4, app['agent_assignments'])

    def test_default_continuation_revisits_integration_before_failed_consumer(self):
        self.app.pop('integration_repair')
        self.later_contract_failure()
        app, command = self.prepare_and_schedule()
        self.assertEqual('development_integrate', command.stage_id)
        self.assertEqual(self.original.payload['development_results'],
                         command.payload['development_results'])
        self.assertEqual(4, app['agent_assignments'])

    def test_blocked_repair_integration_is_revisited_before_its_consumer(self):
        self.app.pop('integration_repair')
        repair = replace(self.original, task_id='target_repair_integrate',
                         stage_id='target_repair_integrate',
                         command_id='previous:target_repair_integrate:1')
        blocked = OperationResult('blocked', repair.run_id, repair.task_id, repair.stage_id,
                                  repair.command_id, error_code='repair_integration_invalid',
                                  detail='selected repair workspace is missing')
        self.snapshot['tasks'][repair.task_id] = {'attempts': [{'state': 'succeeded',
            'command': {'execution_id': repair.command_id, 'payload': repair.to_dict()},
            'result': {'value': blocked.to_dict()}}]}
        self.app['effective'][repair.stage_id] = blocked.to_dict()
        self.app['history'].append({'execution_id': repair.command_id})
        self.later_contract_failure()
        for stage in (None, 'target_contract_freeze'):
            with self.subTest(start_stage=stage):
                app, command = self.prepare_and_schedule(start_stage=stage)
                self.assertEqual('target_repair_integrate', command.stage_id)
                self.assertEqual(blocked.to_dict(), self.app['effective'][repair.stage_id])
                self.assertEqual(4, app['agent_assignments'])

    def test_later_completed_candidate_prevents_replay_of_old_failed_integration(self):
        self.app.pop('integration_repair')
        completed = replace(self.original, task_id='target_repair_integrate',
                            stage_id='target_repair_integrate',
                            command_id='previous:target_repair_integrate:1')
        result = OperationResult('completed', completed.run_id, completed.task_id,
            completed.stage_id, completed.command_id, outputs={'head': 'd' * 40})
        self.app['effective'][completed.stage_id] = result.to_dict()
        self.app['history'].append({'execution_id': completed.command_id})
        self.later_contract_failure()
        app, command = self.prepare_and_schedule(start_stage='target_contract_freeze')
        self.assertEqual('target_contract_freeze', command.stage_id)
        self.assertNotIn('integration_replay', app)

    def successful_coder(self):
        patch = {'path': 'resolved.patch'}
        (self.root / patch['path']).write_text('retained successful coder delta\n')
        result = OperationResult('completed', self.coder.run_id, self.coder.task_id,
            'coder', self.coder.command_id, outputs={'artifact_refs': {'coder_patch': patch}})
        self.snapshot['tasks'][self.coder.task_id]['attempts'][-1].update(
            state='succeeded', result={'value': result.to_dict()})
        return patch, result

    def test_unconsumed_successful_coder_patch_is_reused_without_new_assignment(self):
        patch, _ = self.successful_coder()
        app, command = self.prepare_and_schedule()
        self.assertEqual({'merge_ref': self.merge_ref, 'coder_patch': patch,
                          'coder_execution_id': self.coder.command_id},
                         command.payload['integration_resolution'])
        self.assertEqual('running', app['integration_repair_history'][-1]['status'])

    def test_resolved_patch_is_reused_without_new_assignment(self):
        patch, result = self.successful_coder()
        self.app['integration_repair'].update(status='resolved', coder_patch=patch,
            coder_execution_id=self.coder.command_id, coder_result=result.to_dict())
        app, command = self.prepare_and_schedule(start_stage='development_integrate')
        self.assertEqual(patch, command.payload['integration_resolution']['coder_patch'])
        self.assertEqual('resolved', app['integration_repair_history'][-1]['status'])

    def test_completed_integration_archives_stale_state_and_resumes_later_failure(self):
        patch, result = self.successful_coder()
        resolved = replace(self.original, command_id='previous:development_integrate:2',
            payload={**self.original.payload, 'integration_resolution': {
                'merge_ref': self.merge_ref, 'coder_patch': patch,
                'coder_execution_id': self.coder.command_id}})
        completed = OperationResult('completed', resolved.run_id, resolved.task_id,
            resolved.stage_id, resolved.command_id, outputs={'head': 'd' * 40})
        self.snapshot['tasks'][resolved.task_id]['attempts'].append({
            'state': 'succeeded', 'command': {'execution_id': resolved.command_id,
                                            'payload': resolved.to_dict()},
            'result': {'value': completed.to_dict()}})
        self.app['effective'][resolved.stage_id] = completed.to_dict()
        self.app['integration_repair'].update(status='integrating', coder_patch=patch,
            coder_execution_id=self.coder.command_id, coder_result=result.to_dict())
        later = replace(self.original, task_id='target_build', stage_id='target_build',
                        command_id='previous:target_build:1', payload={})
        failure = OperationResult('failed', later.run_id, later.task_id, later.stage_id,
                                  later.command_id, error_code='target_build_failed')
        self.snapshot['tasks'][later.task_id] = {'attempts': [{'state': 'succeeded',
            'command': {'execution_id': later.command_id, 'payload': later.to_dict()},
            'result': {'value': failure.to_dict()}}]}
        self.app['effective'][later.stage_id] = failure.to_dict()
        self.app['history'].append({'execution_id': later.command_id})
        app, command = self.prepare_and_schedule(expected_assignments=5)
        self.assertNotEqual('development_integrate', command.stage_id)
        self.assertNotIn('integration_repair', app)
        self.assertNotIn('integration_replay', app)

    def test_same_run_restart_keeps_exact_running_task_and_assignment_usage(self):
        self.snapshot['tasks'][self.coder.task_id]['attempts'][-1]['state'] = 'running'
        before = deepcopy(self.app)
        self.assertEqual([], integration_repair.start_or_resume(self.owner, self.snapshot,
            self.header, self.app))
        self.assertEqual(before, self.app)

    def test_public_successor_has_fresh_sdk_authority_and_preserves_predecessor(self):
        with open_runtime(self.root, handlers=self.handlers, isolation_mode='thread') as runtime:
            self.header['registry_revision'] = runtime.registry_revision
            atomic_json(self.root / 'run.json', self.header)
            sdk = Orchestrator(self.root / 'orchestrator.sqlite3', runtime.kernel, runtime=runtime)
            try:
                sdk.create_run('previous', command_id='create', input=self.header,
                               definition=self.header['definition'])
                commands = (self.original_sdk, self.coder_sdk)
                sdk.apply_operations('previous', command_id='add', expected_revision=0,
                    operations=[Operations.add_task(unpack_input(self.root, command['payload'])['task_id'],
                        {**command, 'registry_revision': runtime.registry_revision}) for command in commands],
                    application_state=self.app)
                sdk.apply_operations('previous', command_id='settle', expected_revision=1,
                    operations=[Operations.cancel(self.original.task_id, reason='Fixture cancellation'),
                                Operations.cancel(self.coder.task_id, reason='Fixture cancellation'),
                                Operations.finish('cancelled')], application_state=self.app)
                predecessor = deepcopy(hydrate_run_snapshot(self.root, sdk.get_run('previous')))
            finally:
                sdk.close()
        successor = continue_from_planner(self.owner, self.root, 'previous', next_run_id='successor',
                                         reason='Explicit repair continuation')
        current = successor.snapshot['input']
        self.assertEqual(self.header['deadline_epoch'], current['deadline_epoch'])
        self.assertEqual(self.header['request']['budget'], current['request']['budget'])
        self.assertEqual('logical', current['logical_run_id'])
        self.assertEqual(4, successor.snapshot['application_state']['agent_assignments'])
        self.assertEqual(['development_integrate'], list(successor.snapshot['tasks']))
        attempt = successor.snapshot['tasks']['development_integrate']['attempts'][-1]
        self.assertEqual('pending_dispatch', attempt['state'])
        self.assertNotEqual(self.original.command_id, attempt['command']['execution_id'])
        operation = OperationInput.from_dict(unpack_input(self.root, attempt['command']['payload']))
        self.assertEqual('logical', operation.run_id)
        self.assertEqual('successor', current['run_id'])
        with open_runtime(self.root, handlers=self.handlers, isolation_mode='thread') as runtime:
            sdk = Orchestrator(self.root / 'orchestrator.sqlite3', runtime.kernel, runtime=runtime)
            try:
                previous = hydrate_run_snapshot(self.root, sdk.get_run('previous'))
                for field in ('input', 'definition', 'tasks', 'application_state'):
                    self.assertEqual(predecessor[field], previous[field])
            finally:
                sdk.close()


if __name__ == '__main__':
    unittest.main()
