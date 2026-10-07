"""The progress-supervisor producer and consumer share one execution-bound protocol."""
from dataclasses import replace
from copy import deepcopy
import json
from pathlib import Path
import subprocess
import time
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from dispatcher_sdk.execution_kernel import BudgetEnvelope, ClockCheckpoint, DeadlineConstraint

from modport.contracts import OperationInput, OperationResult
from modport.handlers import SupervisorHandler
from modport.kernel_runtime import cancel_incomplete_agent_stage, cancel_incomplete_progress_assignment
from modport.execution_budget import execution_budget
from modport.progress_supervisor import (PROGRESS_SUPERVISOR_PROMPT,
    invoke_progress_supervisor, validate_progress_supervisor_decision, recorded_progress_termination)
from modport.report_dialogue import prepare_dialogue
from modport.workflow import WorkflowDefinition, WORKFLOW_VERSION


class SDKBudgetContext:
    """Sample the pinned SDK's public budget instead of deriving it from a lease."""

    def __init__(self, command):
        self.command = SimpleNamespace(execution_id=command.command_id,
                                       timeout_seconds=1200, payload=command.to_dict())
        self.envelope = BudgetEnvelope((DeadlineConstraint(
            'progress-supervisor', 'execution', time.time() + 1200, 0),), self.sample())

    @staticmethod
    def sample():
        return ClockCheckpoint(time.time(), time.monotonic(), 'progress-supervisor-test', 'boot')

    @property
    def budget(self):
        self.envelope = self.envelope.recheckpoint(sample=self.sample())
        return self.envelope.view(sample=self.envelope.checkpoint)


class ProgressSupervisorTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.request = {'review_id': 'idle-review-1', 'target_task_id': 'coder.api',
            'target_execution_id': 'run:coder.api:1', 'idle_windows': 3,
            'interval_seconds': 600, 'observation': {
                'snapshot_paths': ['artifacts/progress/before.json', 'artifacts/progress/after.json'],
                'evidence_paths': ['artifacts/target-tool-response.txt'],
                'before': {'useful_tool_results': 0}, 'after': {'useful_tool_results': 0}}}
        definition = WorkflowDefinition({'budget': {}}).to_dict()
        self.command = OperationInput('run', 'progress.idle-review-1', 'supervisor',
            'run:progress.idle-review-1:1', str(self.root),
            payload={'progress_supervision': self.request},
            options={'workflow_version': WORKFLOW_VERSION,
                'agent_assignment': 4, 'deadline_epoch': 10_000_000_000,
                'workspace': 'workspaces/progress-supervision/idle-review-1',
                'progress_supervision_policy': definition['progress_supervision_policy'],
                'agent_dialogue_policy': definition['agent_dialogue_policy']})
        self.decision = {key: self.request[key] for key in (
            'review_id', 'target_task_id', 'target_execution_id')}
        self.decision.update(decision='terminate', reason='The tool response confirms a circular dependency.')

    def result_handler(self, text, *, status='completed'):
        def create(prompt):
            self.assertIn(json.dumps(self.request, sort_keys=True), prompt)
            def execute(command):
                output = self.root / 'logs' / 'report.txt'
                output.parent.mkdir(parents=True, exist_ok=True)
                output.write_text(text)
                return OperationResult(status, command.run_id, command.task_id,
                    command.stage_id, command.command_id,
                    outputs={'last_message': output.relative_to(self.root).as_posix()},
                    error_code='agent_failed' if status == 'failed' else None)
            return execute
        return create

    def test_schema_and_prompt_supply_exact_consumer_fields_without_goal_editing(self):
        dialogue = prepare_dialogue(self.command, self.root, PROGRESS_SUPERVISOR_PROMPT)
        schema = json.loads(dialogue['schema_path'].read_text())
        self.assertEqual(set(self.decision), set(schema['required']))
        self.assertEqual(['continue', 'terminate'], schema['properties']['decision']['enum'])
        self.assertIn('read-only investigation', dialogue['planning_task'])
        self.assertNotIn('publish those edits', dialogue['planning_task'])
        self.assertNotIn('Edit the supplied goal', dialogue['execution_task'])
        self.assertEqual(self.decision,
            validate_progress_supervisor_decision(self.decision, self.request))

    def test_invalid_or_failed_reports_cannot_terminate_and_raw_report_is_archived(self):
        cases = [('not JSON', 'completed'),
                 (json.dumps({**self.decision, 'target_execution_id': 'other:execution:1'}), 'completed'),
                 (json.dumps({**self.decision, 'reason': ''}), 'completed'),
                 (json.dumps(self.decision), 'failed')]
        for text, status in cases:
            with self.subTest(text=text, status=status):
                result = invoke_progress_supervisor(self.command, self.result_handler(text, status=status))
                self.assertIsNone(result.outputs['progress_supervisor_decision'])
                self.assertEqual(status, result.status)
                self.assertEqual(text,
                    (self.root / result.outputs['progress_supervisor_raw_report']).read_text())
                self.assertTrue(result.outputs['progress_supervisor_diagnostics'])

    def test_report_path_cannot_read_outside_run(self):
        external = self.root.parent / 'outside-progress-report.txt'
        def create(prompt):
            return lambda command: OperationResult('completed', command.run_id,
                command.task_id, command.stage_id, command.command_id,
                outputs={'last_message': str(external)})
        result = invoke_progress_supervisor(self.command, create)
        self.assertIsNone(result.outputs['progress_supervisor_decision'])
        self.assertIn('leaves the Run', result.outputs['progress_supervisor_diagnostics'][0])

    def test_interrupted_kernel_recovery_archives_progress_report_without_goal_manifest(self):
        directory = self.root / 'artifacts' / 'executions' / self.command.command_id
        report = directory / 'dialogue' / 'final-report.txt'
        report.parent.mkdir(parents=True)
        raw = json.dumps(self.decision)
        report.write_text(raw)
        expected = {'input_sha256': 'host-effect-input', 'run_dir': str(self.root), 'stage': 'supervisor'}
        effect = SimpleNamespace(execution_id=self.command.command_id,
            effect_id='modport:' + self.command.command_id, name='modport.stage',
            revision=1, attempt=1, fence=1, request=expected)
        command = {'payload': self.command.to_dict(), 'handler_id': 'modport.supervisor',
                   'execution_id': self.command.command_id}
        evidence = {'schema': 'modport.interrupted-agent-cancellation.v1',
            'run_id': self.command.run_id, 'task_id': self.command.task_id,
            'stage_id': 'supervisor', 'execution_id': self.command.command_id,
            'effect_id': effect.effect_id, 'effect_revision': 1, 'kernel_attempt': 1,
            'fence': 1, 'recovery_reason': 'user_cancelled', 'external_outcome': 'unknown',
            'artifacts_complete': False, 'acceptance_status': 'unverified',
            'proof': {key: 'confirmed' for key in ('request_committed', 'command_delivered',
                'execution_authority_revoked', 'local_process_tree_reaped', 'cleanup')},
            'issues': [], 'effects_truncated': False, 'receipts_truncated': False}
        # The existing frozen Effect custody comparison uses host-supplied
        # identity here; no new digest calculation or verification is needed.
        with patch('modport.kernel_runtime.digest', return_value='host-effect-input'), \
             patch('modport.supervised_goals.prepare', side_effect=AssertionError('old goal path')), \
             patch('modport.supervised_goals.collect', side_effect=AssertionError('old goal path')):
            result = cancel_incomplete_agent_stage(self.root, command, effect, evidence)
        self.assertEqual('failed', result['status'])
        self.assertEqual('supervisor_interrupted', result['error_code'])
        self.assertIsNone(result['outputs']['progress_supervisor_decision'])
        self.assertNotIn('supervised_goal_revisions', result['outputs'])
        refs = result['outputs']['artifact_refs']
        self.assertEqual(raw,
            (self.root / refs['progress_supervision_interrupted_report']['path']).read_text())
        packet = json.loads((self.root / refs['progress_supervision_interrupted_request']['path']).read_text())
        self.assertEqual(self.request, packet['progress_supervision'])
        self.assertEqual(raw, report.read_text())
        self.assertFalse((self.root / 'artifacts' / 'supervised-goals').exists())

    def test_interrupted_progress_recovery_preserves_cleanup_proof_requirement(self):
        expected = {'input_sha256': 'host-effect-input', 'run_dir': str(self.root), 'stage': 'supervisor'}
        effect = SimpleNamespace(execution_id=self.command.command_id,
            effect_id='modport:' + self.command.command_id, name='modport.stage',
            revision=1, attempt=1, fence=1, request=expected)
        command = {'payload': self.command.to_dict(), 'handler_id': 'modport.supervisor',
                   'execution_id': self.command.command_id}
        with patch('modport.kernel_runtime.digest', return_value='host-effect-input'):
            with self.assertRaisesRegex(ValueError, 'cancellation evidence identity'):
                cancel_incomplete_agent_stage(self.root, command, effect, {})
        self.assertFalse((self.root / 'artifacts').exists())

    def test_progress_settlement_authority_requires_exact_completed_supervisor_result(self):
        target = replace(self.command, task_id=self.request['target_task_id'],
                         stage_id='coder', command_id=self.request['target_execution_id'])
        reason = 'progress_supervisor: ' + self.decision['reason']
        outcome = OperationResult('completed', self.command.run_id, self.command.task_id,
            'supervisor', self.command.command_id, outputs={'progress_supervisor_decision': self.decision})
        review = {'status': 'termination_requested', 'target_task_id': target.task_id,
            'target_execution_id': target.command_id, 'supervisor_task_id': self.command.task_id,
            'supervisor_execution_id': self.command.command_id,
            'request': self.request, 'decision': self.decision}
        snapshot = {'application_state': {'progress_supervision': {
            'terminated_executions': {target.command_id: {'task_id': target.task_id,
                'review_id': self.request['review_id'], 'reason': self.decision['reason']}},
            'reviews': {self.request['review_id']: review}}}, 'tasks': {
                target.task_id: {'attempts': [{'state': 'recovery_required',
                    'command': {'execution_id': target.command_id, 'payload': target.to_dict()}}]},
                self.command.task_id: {'attempts': [{'state': 'succeeded',
                    'command': {'execution_id': self.command.command_id, 'payload': self.command.to_dict()},
                    'result': {'value': outcome.to_dict()}}]}}}
        authority = recorded_progress_termination(snapshot, target, reason)
        self.assertEqual(self.command.command_id, authority['supervisor_execution_id'])
        invalid = deepcopy(snapshot)
        invalid['tasks'][self.command.task_id]['attempts'][-1]['state'] = 'failed'
        self.assertIsNone(recorded_progress_termination(invalid, target, reason))
        invalid = deepcopy(snapshot)
        invalid['application_state']['progress_supervision']['terminated_executions'][target.command_id]['task_id'] = 'other'
        self.assertIsNone(recorded_progress_termination(invalid, target, reason))
        self.assertIsNone(recorded_progress_termination(snapshot, target, 'progress_supervisor: different reason'))
        expected = {'input_sha256': 'host-effect-input', 'run_dir': str(self.root), 'stage': target.stage_id}
        effect = SimpleNamespace(execution_id=target.command_id, effect_id='modport:' + target.command_id,
                                 name='modport.stage', revision=1, attempt=1, fence=1, request=expected)
        evidence = {'schema': 'modport.progress-assignment-cancellation.v1', 'run_id': target.run_id,
            'task_id': target.task_id, 'stage_id': target.stage_id, 'execution_id': target.command_id,
            'effect_id': effect.effect_id, 'effect_revision': 1, 'kernel_attempt': 1, 'fence': 1,
            'external_outcome': 'unknown', 'artifacts_complete': False, 'acceptance_status': 'unverified',
            'recovery_reason': reason, 'supervisor_authorization': authority,
            'proof': {key: 'confirmed' for key in ('request_committed', 'command_delivered',
                'execution_authority_revoked', 'local_process_tree_reaped', 'cleanup')},
            'issues': [], 'effects_truncated': False, 'receipts_truncated': False}
        evidence['proof']['cleanup'] = 'unknown'
        command = {'execution_id': target.command_id, 'handler_id': 'modport.coder', 'payload': target.to_dict()}
        with patch('modport.kernel_runtime.digest', return_value='host-effect-input'):
            with self.assertRaisesRegex(ValueError, 'cleanup proof is incomplete'):
                cancel_incomplete_progress_assignment(self.root, command, effect, evidence)
        self.assertFalse((self.root / 'artifacts').exists())

    def test_real_stage_handoff_keeps_budget_identity_read_only_and_uses_custom_schema(self):
        calls = []
        def execute(**kwargs):
            calls.append(kwargs)
            self.assertTrue(kwargs['read_only'])
            self.assertEqual(self.root / self.command.options['workspace'], kwargs['cwd'])
            self.assertTrue(kwargs['cwd'].is_dir())
            self.assertEqual(self.command.command_id, kwargs['command_id'])
            self.assertGreater(kwargs['timeout'], 0)
            self.assertLessEqual(kwargs['timeout'], 1200)
            schema = json.loads(kwargs['schema_path'].read_text())
            self.assertEqual(set(self.decision), set(schema['properties']))
            instructions = self.root / 'artifacts' / 'executions' / self.command.command_id
            planning = json.loads((instructions / 'task-instructions.plan.json').read_text())
            executing = json.loads((instructions / 'task-instructions.execute.json').read_text())
            self.assertIn('read-only investigation', planning['task'])
            self.assertIn(self.request['target_execution_id'], executing['task'])
            self.assertIn('modport_sandbox_read_run_artifact', executing['task'])
            self.assertIn('continue or terminate', executing['task'])
            self.assertIn('budget.max_rework_rounds is a legacy count', executing['protected_context'])
            self.assertIn('Do not self-terminate', executing['protected_context'])
            self.assertNotIn('Edit the supplied goal', executing['task'])
            result = subprocess.CompletedProcess(['opencode'], 0, json.dumps({
                'type': 'item.completed', 'item': {'type': 'agent_message',
                                                  'text': json.dumps(self.decision)}}))
            result.dialogue_metadata = {}
            return result
        compressor = SimpleNamespace(compress=lambda text, **kwargs: SimpleNamespace(
            text=text, metadata={'compressed': False, 'context_window': 272000,
                'input_token_budget': 200000, 'output_token_reserve': 10000,
                'tool_token_reserve': 10000}))
        # Host authentication is outside this focused handoff test. Supply its
        # already-resolved rule references without introducing digest checks.
        resolved_rule = ('Shared execution rules.', {'path': 'artifacts/rules.md',
                                                    'sha256': 'host-supplied-provenance'})
        context = SDKBudgetContext(self.command)
        with patch('modport.handlers._acceptance_rubric_for', return_value={}), \
             patch('modport.handlers._read_artifact_ref', return_value=resolved_rule), \
             patch('modport.handlers._reuse_cached_compressed_prompt', return_value=None), \
             patch('modport.handlers.PromptCompressor.from_environment', return_value=compressor), \
             patch('modport.opencode_agent.run_agent', side_effect=execute), \
             execution_budget(context):
            result = SupervisorHandler()(self.command)
        self.assertEqual('completed', result.status, result.detail)
        self.assertEqual(self.decision, result.outputs['progress_supervisor_decision'])
        self.assertEqual(4, result.outputs['agent_assignment'])
        self.assertEqual(1, len(calls))
        self.assertEqual(10_000_000_000, self.command.options['deadline_epoch'])
        self.assertEqual(self.request, self.command.payload['progress_supervision'])
        self.assertEqual(json.dumps(self.decision),
            (self.root / result.outputs['progress_supervisor_raw_report']).read_text().strip())


if __name__ == '__main__':
    unittest.main()
