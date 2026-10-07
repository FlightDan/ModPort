"""Watchdog decision authority, producer handoff and eligible rework targets."""
from dataclasses import replace
import json
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from dispatcher_sdk.execution_kernel import BudgetEnvelope, ClockCheckpoint, DeadlineConstraint

from modport.contracts import OperationInput, OperationResult
from modport.execution_budget import execution_budget
from modport.rework_tools import is_interactive_review, prepare_session, rework_targets, tool_prompt
from modport.watchdog_supervisor import (WATCHDOG_SUPERVISOR_PROMPT,
    invoke_watchdog_supervisor, validate_watchdog_decision, watchdog_supervisor_schema)
from modport.workflow import WORKFLOW_VERSION, WorkflowDefinition


class WatchdogSupervisorTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.request = {'incident_id': 'incident-1', 'target_task_id': 'coder.api',
            'target_execution_id': 'run:coder.api:1', 'kind': 'execution_failed',
            'reason': 'handler_process_exit', 'known_task_ids': ['coder.api', 'prepare'],
            'budget_context': {'deadline_epoch': 10_000_000_000, 'agent_assignments': 4},
            'evidence_refs': ['artifacts/raw-error.txt'],
            'target_result': {'error_code': 'handler_process_exit'}, 'prior_recovery': None}
        self.command = OperationInput('run', 'watchdog.incident-1', 'supervisor',
            'run:watchdog.incident-1:1', str(self.root),
            payload={'watchdog_incident': self.request},
            options={'workflow_version': WORKFLOW_VERSION, 'business_gates_disabled': True,
                     'agent_assignment': 5, 'deadline_epoch': 10_000_000_000,
                     'agent_dialogue_policy': WorkflowDefinition({'budget': {}}).to_dict()['agent_dialogue_policy'],
                     'workspace': 'workspaces/watchdog/incident-1'})
        self.decision = {'incident_id': 'incident-1', 'action': 'repair_resume',
            'reason': 'The retained compiler error identifies an unavailable method.',
            'instruction': 'Replace the obsolete invocation in the supplied isolated copy, then compile.',
            'wait_for': [], 'stop_category': None}

    def handler(self, text, *, status='completed', edit=None):
        def create(prompt):
            self.assertIn(json.dumps(self.request, sort_keys=True), prompt)
            self.assertIn('host cancels and settles', prompt)
            self.assertIn('Do not run project code', prompt)
            def execute(command):
                self.assertEqual(self.command.command_id, command.command_id)
                self.assertEqual(self.command.options['agent_assignment'], command.options['agent_assignment'])
                self.assertEqual(self.command.options['deadline_epoch'], command.options['deadline_epoch'])
                if edit is not None:
                    edit(command)
                output = self.root / 'logs' / 'watchdog-report.txt'
                output.parent.mkdir(parents=True, exist_ok=True)
                output.write_text(text)
                return OperationResult(status, command.run_id, command.task_id, command.stage_id,
                    command.command_id, outputs={'last_message': output.relative_to(self.root).as_posix()},
                    error_code='agent_failed' if status == 'failed' else None)
            return execute
        return create

    def test_producer_schema_and_consumer_share_machine_fields(self):
        from modport.report_schemas import report_contract
        contract = report_contract(self.command)
        self.assertEqual(watchdog_supervisor_schema(), contract['schema'])
        self.assertEqual(set(self.decision), set(contract['schema']['required']))
        self.assertIsNone(contract['output_path'])
        self.assertIn('request_rework', WATCHDOG_SUPERVISOR_PROMPT)
        self.assertIn('conclusively unrecoverable', WATCHDOG_SUPERVISOR_PROMPT)
        result = invoke_watchdog_supervisor(self.command, self.handler(json.dumps(self.decision)))
        self.assertEqual(self.decision, result.outputs['watchdog_decision'])
        self.assertEqual(self.request, json.loads((self.root / result.outputs['watchdog_supervision_input']).read_text())['watchdog_incident'])

    def test_invalid_and_failed_reports_have_no_authority_and_keep_raw_evidence(self):
        cases = [('not JSON', 'completed'),
            (json.dumps({**self.decision, 'incident_id': 'another'}), 'completed'),
            (json.dumps({**self.decision, 'run_id': 'invented'}), 'completed'),
            (json.dumps({**self.decision, 'instruction': None}), 'completed'),
            (json.dumps(self.decision), 'failed')]
        for text, status in cases:
            with self.subTest(text=text, status=status):
                result = invoke_watchdog_supervisor(self.command, self.handler(text, status=status))
                self.assertIsNone(result.outputs['watchdog_decision'])
                self.assertEqual(status, result.status)
                self.assertEqual(text, (self.root / result.outputs['watchdog_supervisor_raw_report']).read_text())
                self.assertTrue(result.outputs['watchdog_supervisor_diagnostics'])

    def test_wait_and_stop_are_bound_to_known_tasks_and_explicit_actions(self):
        wait = {**self.decision, 'action': 'wait', 'wait_for': ['prepare'], 'instruction': None}
        self.assertEqual(wait, validate_watchdog_decision(wait, self.request))
        external = {**wait, 'wait_for': [], 'instruction': 'Wait for the external provider to restore authentication.'}
        self.assertEqual(external, validate_watchdog_decision(external, self.request))
        stop = {**self.decision, 'action': 'stop', 'instruction': None, 'stop_category': 'unrecoverable'}
        self.assertEqual(stop, validate_watchdog_decision(stop, self.request))
        for document in ({**wait, 'wait_for': ['invented']},
                         {**wait, 'wait_for': []},
                         {**wait, 'wait_for': ['prepare', 'prepare']},
                         {**stop, 'stop_category': 'timeout'},
                         {**self.decision, 'stop_category': 'unrecoverable'},
                         {**self.decision, 'wait_for': ['prepare']}):
            with self.subTest(document=document), self.assertRaises(ValueError):
                validate_watchdog_decision(document, self.request)

    def test_report_path_cannot_escape_or_traverse_symlink(self):
        outside = self.root.parent / 'outside-watchdog-report.txt'
        for path in (str(outside), 'logs/link.txt'):
            with self.subTest(path=path):
                if path == 'logs/link.txt':
                    (self.root / 'logs').mkdir(exist_ok=True)
                    (self.root / path).symlink_to(outside)
                def create(prompt):
                    return lambda command: OperationResult('completed', command.run_id,
                        command.task_id, command.stage_id, command.command_id,
                        outputs={'last_message': path})
                result = invoke_watchdog_supervisor(self.command, create)
                self.assertIsNone(result.outputs['watchdog_decision'])
                self.assertTrue(result.outputs['watchdog_supervisor_diagnostics'])

    def test_actual_isolated_source_repair_preserves_running_workspace(self):
        source = self.root / 'workspaces/development/author/src/A.java'
        source.parent.mkdir(parents=True)
        source.write_text('class A { obsolete(); }\n')
        target = {'task_id': 'api', 'plan_ref': {'path': 'artifacts/plan.json'},
                  'source_workspace': 'workspaces/development/author',
                  'source_execution_id': 'run:coder.api:1'}
        command = replace(self.command, payload={**self.command.payload,
                                                'diagnostic_repair_targets': [target]})
        def edit(prepared):
            path = self.root / prepared.options['workspace'] / 'source/task-0/src/A.java'
            path.write_text('class A { current(); }\n')
        result = invoke_watchdog_supervisor(command, self.handler(json.dumps(self.decision), edit=edit))
        self.assertEqual(self.decision, result.outputs['watchdog_decision'])
        self.assertTrue(result.outputs['diagnostic_repairs'])
        self.assertEqual('api', result.outputs['diagnostic_repairs'][0]['metadata']['task_id'])
        self.assertEqual('class A { obsolete(); }\n', source.read_text())
        self.assertNotIn('sha256', result.outputs['diagnostic_repairs'][0])

    def test_actual_rework_targets_are_exposed_only_to_watchdog_supervisor(self):
        source = OperationInput('run', 'coder.api', 'coder', 'run:coder.api:1', str(self.root),
            payload={'development_task': {'id': 'api', 'objective': 'Repair the API call',
                                         'owned_paths': ['src/A.java']}, 'goal_scope': 'migration'},
            options={'workflow_version': WORKFLOW_VERSION})
        outcome = OperationResult('failed', source.run_id, source.task_id, source.stage_id,
            source.command_id, error_code='agent_failed', detail='obsolete API')
        snapshot = {'tasks': {source.task_id: {'attempts': [{'state': 'failed',
            'command': {'execution_id': source.command_id, 'payload': source.to_dict()},
            'result': {'value': outcome.to_dict()}}]}}}
        watchdog = replace(self.command, upstream_results={source.task_id: outcome.to_dict()})
        targets = rework_targets(snapshot, watchdog)
        self.assertEqual([source.task_id], [target['target_agent'] for target in targets])
        interactive = replace(watchdog, payload={**watchdog.payload, 'review_rework_targets': targets})
        self.assertTrue(is_interactive_review(interactive))
        workspace = self.root / interactive.options['workspace']
        workspace.mkdir(parents=True)
        with patch('modport.repair_context.observe_candidate', side_effect=AssertionError('extra candidate check')):
            session = prepare_session(interactive, workspace, 120)
        descriptor = json.loads(session.read_text())
        self.assertEqual(targets, descriptor['targets'])
        self.assertNotIn('repair_context', descriptor)
        self.assertIn('request_rework', tool_prompt(interactive))
        for payload in ({'progress_supervision': {}}, {'desktop_chat': {}}, {}):
            ordinary = replace(watchdog, payload=payload)
            self.assertEqual([], rework_targets(snapshot, ordinary))
            self.assertFalse(is_interactive_review(replace(ordinary, payload={**payload, 'review_rework_targets': targets})))
        revival = replace(watchdog, stage_id='coder_revival_plan')
        self.assertEqual([], rework_targets(snapshot, revival))

    def test_real_agent_transport_consumes_watchdog_prompt_schema_and_budget(self):
        from modport.handlers import SupervisorHandler
        calls = []
        def execute(**kwargs):
            calls.append(kwargs)
            self.assertTrue(kwargs['read_only'])
            self.assertEqual(self.root / self.command.options['workspace'], kwargs['cwd'])
            self.assertEqual(self.command.command_id, kwargs['command_id'])
            self.assertGreater(kwargs['timeout'], 0)
            self.assertLessEqual(kwargs['timeout'], 1200)
            self.assertEqual(watchdog_supervisor_schema(), json.loads(kwargs['schema_path'].read_text()))
            directory = self.root / 'artifacts/executions' / self.command.command_id
            plan = json.loads((directory / 'task-instructions.plan.json').read_text())
            task = json.loads((directory / 'task-instructions.execute.json').read_text())
            self.assertIn('root-cause investigation', plan['task'])
            self.assertNotIn('necessary edits to the supplied goal', plan['task'])
            self.assertIn(self.request['incident_id'], task['task'])
            self.assertIn('host cancels and settles', task['task'])
            self.assertIn('unchanged', task['task'])
            result = subprocess.CompletedProcess(['opencode'], 0, json.dumps({
                'type': 'item.completed', 'item': {'type': 'agent_message',
                                                  'text': json.dumps(self.decision)}}))
            result.dialogue_metadata = {}
            return result
        compressor = SimpleNamespace(compress=lambda text, **kwargs: SimpleNamespace(
            text=text, metadata={'compressed': False, 'context_window': 272000,
                'input_token_budget': 200000, 'output_token_reserve': 10000,
                'tool_token_reserve': 10000}))
        class BudgetContext:
            def __init__(self, command):
                self.command = SimpleNamespace(execution_id=command.command_id,
                    timeout_seconds=1200, payload=command.to_dict())
                self.envelope = BudgetEnvelope((DeadlineConstraint(
                    'watchdog-test', 'execution', time.time() + 1200, 0),), self.sample())
            @staticmethod
            def sample():
                return ClockCheckpoint(time.time(), time.monotonic(), 'watchdog-test', 'boot')
            @property
            def budget(self):
                self.envelope = self.envelope.recheckpoint(sample=self.sample())
                return self.envelope.view(sample=self.envelope.checkpoint)
        resolved_rule = ('Shared execution rules.', {'path': 'artifacts/rules.md',
                                                    'sha256': 'host-supplied-provenance'})
        with patch('modport.handlers._acceptance_rubric_for', return_value={}), \
             patch('modport.handlers._read_artifact_ref', return_value=resolved_rule), \
             patch('modport.handlers._reuse_cached_compressed_prompt', return_value=None), \
             patch('modport.handlers.PromptCompressor.from_environment', return_value=compressor), \
             patch('modport.opencode_agent.run_agent', side_effect=execute), \
             execution_budget(BudgetContext(self.command)):
            result = SupervisorHandler()(self.command)
        self.assertEqual('completed', result.status, result.detail)
        self.assertEqual(self.decision, result.outputs['watchdog_decision'])
        self.assertEqual(1, len(calls))
        self.assertEqual(5, result.outputs['agent_assignment'])
        self.assertEqual(self.request, self.command.payload['watchdog_incident'])
        self.assertEqual(self.decision,
            json.loads((self.root / result.outputs['watchdog_supervisor_raw_report']).read_text()))


if __name__ == '__main__':
    unittest.main()
