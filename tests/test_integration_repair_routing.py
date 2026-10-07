"""Conflict repair uses the production scheduler without model calls or Runs."""

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from modport import integration_repair
from modport.contracts import OperationInput, OperationResult, json_copy
from modport.operations import MigrationOperations
from modport.workflow import WORKFLOW_VERSION, WorkflowDefinition


class IntegrationRepairRoutingTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.owner = MigrationOperations(clock=lambda: 100.0)
        self.app = self.owner._new_application()
        self.app['agent_assignments'] = 3
        self.header = {'run_id': 'run', 'run_dir': str(self.root),
                       'definition': {'workflow_version': WORKFLOW_VERSION},
                       'deadline_epoch': 700.0, 'registry_revision': 'fixture-current',
                       'rubric_sha256': 'host-owned-provenance', 'prior_findings': [],
                       'initial_refs': {},
                       'request': {'workflow_mode': 'migration', 'budget': {
                           'max_agent_assignments': 5, 'max_tokens': None,
                           'max_rework_rounds': 0, 'execution_max_attempts': 1}}}
        self.header['definition'] = WorkflowDefinition(self.header['request']).to_dict()
        self.original = OperationInput(
            'run', 'development_integrate', 'development_integrate',
            'run:development_integrate:1', str(self.root),
            payload={'development_generation': 1, 'development_base': 'a' * 40,
                     'development_results': [], 'goal_scope': 'migration'},
            options={'workflow_version': WORKFLOW_VERSION, 'workspace': 'worktree',
                     'deadline_epoch': 700.0},
            artifact_refs={'development_plan': {'path': 'original-plan.json'}})
        (self.root / 'original-plan.json').write_text('{}\n')
        (self.root / 'original-command.json').write_text(json.dumps(self.original.to_dict()))
        self.record = {
            'source_workspace': 'worktree', 'workspace': 'workspaces/integration-merge/original',
            'start': 'b' * 40, 'merge_head': 'c' * 40,
            'conflicts': [{'task_id': 'A', 'paths': ['src/A.java'],
                           'patch_path': 'original.patch', 'detail': 'CONFLICT: overlapping edits'}],
            'original_task_ids': ['A'], 'command_ref': {'path': 'original-command.json'},
            'plan_ref': {'path': 'original-plan.json'}, 'patch_refs': [{'path': 'original.patch'}]}
        (self.root / 'merge.json').write_text(json.dumps(self.record))
        self.merge_ref = {'path': 'merge.json'}
        self.outcome = OperationResult(
            'failed', self.original.run_id, self.original.task_id,
            self.original.stage_id, self.original.command_id,
            error_code='integration_merge_required', outputs={'integration_merge_ref': self.merge_ref})
        self.snapshot = {'run_id': 'run', 'tasks': {}}
        # Keep operation payloads directly observable. Scheduling, budget
        # accounting and command construction remain the production methods.
        packing = patch('modport.operations.pack_input', side_effect=lambda root, value: value)
        packing.start()
        self.addCleanup(packing.stop)

    def start(self):
        return integration_repair.start_or_resume(
            self.owner, self.snapshot, self.header, self.app, self.original, self.outcome)

    def settle_coder(self, operations, *, status='completed', patch_data='resolved delta',
                     error_code=None, detail=''):
        dispatched = next(op for op in operations if op['kind'] == 'add_task')
        command = OperationInput.from_dict(dispatched['command']['payload'])
        refs = {}
        if patch_data is not None:
            (self.root / 'resolution.patch').write_text(patch_data)
            refs['coder_patch'] = {'path': 'resolution.patch'}
        result = OperationResult(status, command.run_id, command.task_id,
                                 command.stage_id, command.command_id,
                                 outputs={'artifact_refs': refs}, error_code=error_code, detail=detail)
        self.snapshot['tasks'][command.task_id] = {'attempts': [{
            'state': 'succeeded', 'command': dispatched['command'],
            'result': {'value': result.to_dict()}}]}
        return command

    def test_real_scheduler_charges_once_and_resume_integrates_original_boundary(self):
        operations = self.start()
        dispatched = next(op for op in operations if op['kind'] == 'add_task')
        command = OperationInput.from_dict(dispatched['command']['payload'])
        self.assertEqual(4, self.app['agent_assignments'])
        self.assertEqual(700.0, command.options['deadline_epoch'])
        self.assertEqual(600.0, dispatched['command']['timeout_seconds'])
        self.assertEqual('coder', command.stage_id)
        self.assertEqual(self.record['merge_head'], command.payload['development_base'])
        self.assertEqual(self.record['workspace'], command.payload['development_source_workspace'])
        self.assertIn('CONFLICT: overlapping edits', command.payload['development_task']['objective'])
        self.assertIn('original.patch', command.payload['development_task']['objective'])
        self.assertNotIn('coder_revival', command.payload)
        self.assertNotIn('sha256', command.artifact_refs['development_plan'])
        self.assertNotEqual(self.record['source_workspace'], command.options['workspace'])
        self.snapshot['tasks'][command.task_id] = {'attempts': [{
            'state': 'running', 'command': dispatched['command']}]}
        self.app = json_copy(self.app)
        self.assertEqual([], self.start())
        self.assertEqual(4, self.app['agent_assignments'])
        self.settle_coder(operations)
        resumed = integration_repair.start_or_resume(
            self.owner, self.snapshot, self.header, self.app)
        operation = next(op for op in resumed if op['kind'] == 'add_task')
        integrated = OperationInput.from_dict(operation['command']['payload'])
        self.assertEqual(self.original.stage_id, integrated.stage_id)
        self.assertEqual(self.original.task_id, integrated.task_id)
        self.assertEqual(self.original.payload['development_base'], integrated.payload['development_base'])
        self.assertEqual(self.merge_ref, integrated.payload['integration_resolution']['merge_ref'])
        self.assertEqual(command.command_id, integrated.payload['integration_resolution']['coder_execution_id'])
        self.assertEqual(4, self.app['agent_assignments'])
        self.assertEqual(700.0, integrated.options['deadline_epoch'])
        self.assertIsNone(integration_repair.start_or_resume(
            self.owner, self.snapshot, self.header, self.app))
        self.assertIsNone(self.start())

    def test_failed_coder_keeps_raw_error_and_never_forwards_or_retries(self):
        operations = self.start()
        self.settle_coder(operations, status='failed', error_code='sdk_execution_identity_invalid',
                          detail='exact SDK execution budget identity was rejected')
        result = integration_repair.start_or_resume(self.owner, self.snapshot, self.header, self.app)
        self.assertEqual([{'kind': 'finish', 'state': 'failed'}], result)
        self.assertEqual('sdk_execution_identity_invalid', self.app['terminal_reason'])
        self.assertIn('execution budget identity', self.app['integration_repair']['detail'])
        self.assertEqual(4, self.app['agent_assignments'])
        self.assertEqual(result, self.start())

    def test_missing_or_empty_patch_cannot_advance_to_cleanup(self):
        for patch_data in (None, ''):
            with self.subTest(patch_data=patch_data):
                self.app = self.owner._new_application()
                self.snapshot['tasks'] = {}
                operations = self.start()
                self.settle_coder(operations, patch_data=patch_data)
                result = integration_repair.start_or_resume(
                    self.owner, self.snapshot, self.header, self.app)
                self.assertEqual([{'kind': 'finish', 'state': 'failed'}], result)
                self.assertEqual('integration_repair_patch_missing', self.app['terminal_reason'])

    def test_assignment_limit_does_not_create_repair_or_extend_deadline(self):
        self.app['agent_assignments'] = self.header['request']['budget']['max_agent_assignments']
        result = self.start()
        self.assertEqual([{'kind': 'finish', 'state': 'failed'}], result)
        self.assertEqual('agent_assignment_budget_exhausted', self.app['terminal_reason'])
        self.assertEqual(5, self.app['agent_assignments'])
        self.assertEqual(700.0, self.header['deadline_epoch'])

    def test_unrelated_failure_does_not_create_repair(self):
        failure = OperationResult('failed', error_code='integration_patch_invalid')
        self.assertIsNone(integration_repair.start_or_resume(
            self.owner, self.snapshot, self.header, self.app, self.original, failure))
        self.assertNotIn('integration_repair', self.app)

    def test_explicit_continuation_reads_original_command_from_host_record(self):
        operations = integration_repair.start_or_resume(
            self.owner, self.snapshot, self.header, self.app, outcome=self.outcome.to_dict())
        self.assertTrue(any(op['kind'] == 'add_task' for op in operations))
        self.assertEqual(self.record['command_ref'], self.app['integration_repair']['command_ref'])
        self.assertNotIn('source_command', self.app['integration_repair'])

    def test_sdk_transport_failure_keeps_exact_error_evidence(self):
        operations = self.start()
        dispatched = next(op for op in operations if op['kind'] == 'add_task')
        self.snapshot['tasks'][dispatched['task_id']] = {'attempts': [{
            'state': 'failed', 'command': dispatched['command'],
            'error': {'code': 'sdk_cleanup_unconfirmed', 'message': 'Child cleanup is uncertain'}}]}
        result = integration_repair.start_or_resume(self.owner, self.snapshot, self.header, self.app)
        self.assertEqual([{'kind': 'finish', 'state': 'failed'}], result)
        self.assertEqual('sdk_cleanup_unconfirmed',
                         self.app['integration_repair']['coder_execution']['error']['code'])
        self.assertNotEqual('completed', self.app['integration_repair']['status'])

    def test_actual_flowthrough_failure_coder_and_reintegration_reaches_cleanup(self):
        original_sdk = {'execution_id': self.original.command_id,
                        'payload': self.original.to_dict()}
        self.snapshot['tasks'][self.original.task_id] = {'attempts': [{
            'state': 'succeeded', 'command': original_sdk,
            'result': {'value': self.outcome.to_dict()}}]}
        self.app['active_stage'] = self.original.task_id
        coder_operations = self.owner._advance_without_business_gates(
            self.snapshot, self.header, self.app)
        coder = self.settle_coder(coder_operations)
        reintegration = self.owner._advance_without_business_gates(
            self.snapshot, self.header, self.app)
        retry = next(op for op in reintegration if op['kind'] == 'new_attempt')
        integrated_command = OperationInput.from_dict(retry['command']['payload'])
        self.assertEqual(coder.command_id, integrated_command.payload[
            'integration_resolution']['coder_execution_id'])
        completed = OperationResult('completed', integrated_command.run_id,
                                    integrated_command.task_id, integrated_command.stage_id,
                                    integrated_command.command_id, outputs={'head': 'd' * 40})
        self.snapshot['tasks'][self.original.task_id]['attempts'].append({
            'state': 'succeeded', 'command': retry['command'],
            'result': {'value': completed.to_dict()}})
        cleanup = self.owner._advance_without_business_gates(
            self.snapshot, self.header, self.app)
        scheduled = [OperationInput.from_dict(op['command']['payload'])
                     for op in cleanup if op['kind'] in {'add_task', 'new_attempt'}]
        self.assertEqual(['code_cleanup'], [command.stage_id for command in scheduled])

    def test_explicit_next_stage_cannot_bypass_conflict_repair(self):
        self.app['effective'][self.original.stage_id] = self.outcome.to_dict()
        self.app['flowthrough_resume'] = {'stage': self.original.stage_id, 'location': 'main',
                                         'task_id': self.original.task_id,
                                         'command_id': self.original.command_id,
                                         'next_stage': 'code_cleanup'}
        operations = self.owner._advance_without_business_gates(
            self.snapshot, self.header, self.app)
        scheduled = [OperationInput.from_dict(op['command']['payload'])
                     for op in operations if op['kind'] in {'add_task', 'new_attempt'}]
        self.assertEqual(['coder'], [command.stage_id for command in scheduled])
        self.assertNotIn('flowthrough_resume', self.app)

    def test_all_integration_failures_stop_with_raw_error_before_successor(self):
        for stage in integration_repair.INTEGRATION_STAGES:
            with self.subTest(stage=stage):
                self.app = self.owner._new_application()
                outcome = OperationResult('failed', stage_id=stage,
                                          error_code='integration_workspace_invalid',
                                          detail='Current candidate contains an unsafe symlink')
                self.app['effective'][stage] = outcome.to_dict()
                operations = self.owner._flowthrough_schedule_successor(
                    self.snapshot, self.header, self.app, stage, 'original')
                self.assertEqual([{'kind': 'finish', 'state': 'failed'}], operations)
                self.assertEqual('integration_workspace_invalid', self.app['terminal_reason'])
                self.assertIn('unsafe symlink', self.app['business_diagnostics'][-1]['detail'])
                self.assertNotIn('integration_repair', self.app)

    def test_explicit_watchdog_stop_cannot_dispatch_pending_repair(self):
        self.start()
        self.app['stop_reason'] = 'supervisor_terminated_author'
        self.app['stop_state'] = 'failed'
        assignments = self.app['agent_assignments']
        operations = self.owner._advance_without_business_gates(
            self.snapshot, self.header, self.app)
        self.assertEqual([], operations)
        self.assertEqual('supervisor_terminated_author', self.app['stop_reason'])
        self.assertEqual(assignments, self.app['agent_assignments'])


if __name__ == '__main__':
    unittest.main()
