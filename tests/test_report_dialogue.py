"""Business handlers consume the second reply and retain both conversation turns."""
from dataclasses import replace
import json
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from modport.handlers import CodexStageHandler, ReviewHandler, SupervisorHandler
from modport.prompts import STAGE_PROMPTS
from modport.report_dialogue import dialogue_enabled, materialize_report, prepare_dialogue
import test_handlers


THREAD = '00000000-0000-0000-0000-000000000135'
POLICY = {'version': 1, 'turns': ['plan', 'execute']}


def transcript(text):
    return '\n'.join(json.dumps(event, ensure_ascii=False) for event in (
        {'type': 'thread.started', 'thread_id': THREAD},
        {'type': 'item.completed', 'item': {'type': 'agent_message', 'text': text}},
        {'type': 'turn.completed', 'usage': {'input_tokens': 10, 'output_tokens': 10}},
    ))


class ReportDialogueTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.work = self.root / 'worktree'
        self.work.mkdir()
        original = test_handlers.HandlerTests._command(self.root, 'code_review')
        self.command = replace(original, options={**original.options,
            'workflow_version': 17, 'agent_assignment': 135, 'agent_dialogue_policy': POLICY})
        self.calls = []
        self.answer = json.dumps({'verdict': 'rejected', 'findings': ['Unmerged adapter.'],
                                  'report': 'Candidate still has an unresolved integration conflict.'})

    def execute(self, args, **kwargs):
        self.calls.append((args, kwargs))
        is_resume = 'resume' in args
        text = self.answer if is_resume else '# 审阅计划\nInspect the integrated candidate and build evidence.'
        log = kwargs['log']
        log.parent.mkdir(parents=True, exist_ok=True)
        log.write_text(transcript(text))
        Path(str(log) + '.stdin.txt').write_text(kwargs['prompt'])
        return subprocess.CompletedProcess(args, 0, transcript(text))

    def test_code_review_has_two_messages_one_assignment_and_actual_json(self):
        with patch('modport.handlers._exec_agent', side_effect=self.execute):
            result = ReviewHandler(baseline=False)(self.command)
        self.assertEqual('failed', result.status)
        self.assertEqual('code_review_rejected', result.error_code)
        self.assertEqual('unverified', result.outputs['acceptance_status'])
        self.assertEqual(2, len(self.calls))
        first_args, first = self.calls[0]
        second_args, second = self.calls[1]
        self.assertNotIn('--ephemeral', first_args)
        self.assertNotIn('--output-schema', first_args)
        self.assertIn('resume', second_args)
        self.assertEqual(THREAD, second_args[-1])
        self.assertIn('--output-schema', second_args)
        self.assertLessEqual(second['timeout'], first['timeout'])
        self.assertEqual(135, result.outputs['agent_assignment'])
        refs = result.outputs['artifact_refs']
        self.assertIn('# 审阅计划', (self.root / refs['agent_plan']['path']).read_text())
        self.assertEqual(self.answer, (self.root / refs['agent_last_message']['path']).read_text().strip())
        self.assertIn('agent_planning_log', refs)
        self.assertIn('agent_execution_log', refs)
        schema = json.loads((self.root / refs['agent_output_schema']['path']).read_text())
        self.assertIn('verdict', schema['properties'])
        directory = self.root / 'artifacts/executions/command-1'
        planning = json.loads((directory / 'task-instructions.plan.json').read_text())['task']
        execution = json.loads((directory / 'task-instructions.execute.json').read_text())['task']
        self.assertIn('prepare only a task plan', planning)
        self.assertNotIn('MODPORT_DECISION', planning)
        self.assertNotIn('output-schema.json', planning)
        self.assertIn('Turn 2 of 2', execution)
        self.assertIn('output-schema.json', execution)
        self.assertNotEqual(first['prompt'], second['prompt'])

    def test_markdown_report_is_saved_from_second_reply(self):
        self.answer = '# Background\nThe mod changes mob difficulty.'
        command = replace(self.command, stage_id='background')
        with patch('modport.handlers._exec_agent', side_effect=self.execute):
            result = CodexStageHandler(STAGE_PROMPTS['background'],
                required_paths=('.modport/background.md',))(command)
        self.assertEqual('completed', result.status, result.detail)
        self.assertEqual(self.answer, (self.work / '.modport/background.md').read_text().strip())
        self.assertNotIn('--output-schema', self.calls[1][0])

    def test_review_final_before_rework_result_keeps_raw_report_and_observation(self):
        def execute(args, **kwargs):
            result = self.execute(args, **kwargs)
            if 'resume' in args:
                pending = {'type': 'item.started', 'item': {
                    'id': 'call-pending', 'type': 'mcp_tool_call',
                    'server': 'modport_rework', 'tool': 'request_rework',
                    'status': 'in_progress', 'arguments': {
                        'target_agent': 'coder-a', 'instructions': 'repair A'}}}
                result.stdout = json.dumps(pending) + '\n' + result.stdout
                kwargs['log'].write_text(result.stdout)
            return result

        with patch('modport.handlers._exec_agent', side_effect=execute):
            result = ReviewHandler(baseline=False)(self.command)
        self.assertEqual('code_review_rejected', result.error_code)
        self.assertEqual('unobserved', result.outputs['review_rework_observation']['status'])
        refs = result.outputs['artifact_refs']
        self.assertEqual(self.answer,
            (self.root / refs['agent_last_message']['path']).read_text().strip())
        self.assertTrue((self.root / refs['review_rework_observation']['path']).is_file())

    def test_frozen_command_without_policy_keeps_single_turn(self):
        command = replace(self.command, options={key: value for key, value in self.command.options.items()
                                               if key != 'agent_dialogue_policy'})
        with patch('modport.handlers._exec_agent', side_effect=self.execute):
            result = CodexStageHandler('Write a report.')(command)
        self.assertEqual('completed', result.status)
        self.assertEqual(1, len(self.calls))
        self.assertIn('--ephemeral', self.calls[0][0])
        self.assertNotIn('agent_dialogue', result.outputs)
        self.assertFalse(dialogue_enabled(command))

    def test_second_turn_timeout_keeps_first_plan_and_session(self):
        def execute(args, **kwargs):
            if 'resume' in args:
                raise subprocess.TimeoutExpired(args, kwargs['timeout'])
            return self.execute(args, **kwargs)
        with patch('modport.handlers._exec_agent', side_effect=execute):
            result = CodexStageHandler('Review the source.')(self.command)
        self.assertEqual('agent_timeout', result.error_code)
        refs = result.outputs['artifact_refs']
        self.assertIn('agent_plan', refs)
        session = json.loads((self.root / refs['agent_dialogue']['path']).read_text())
        self.assertEqual(THREAD, session['thread_id'])
        self.assertEqual(1, session['turns'])

    def test_malformed_second_reply_remains_raw_diagnostic(self):
        self.answer = 'Partial review {invalid JSON'
        with patch('modport.handlers._exec_agent', side_effect=self.execute):
            result = ReviewHandler(baseline=False)(self.command)
        self.assertEqual('completed', result.status, result.detail)
        self.assertEqual('unverified', result.outputs['acceptance_status'])
        self.assertEqual(self.answer, result.outputs['raw_report'].strip())
        self.assertEqual(2, len(self.calls))

    def test_multi_document_materialization_is_limited_to_host_paths(self):
        dialogue = prepare_dialogue(self.command, self.root, 'Read source.')
        dialogue['contract'] = {'output_paths': ['metadata.json', 'SKILL.md']}
        text = json.dumps({'outputs': {'metadata.json': {'schema_version': 1},
            'SKILL.md': '# Skill', '../unexpected': 'unsafe'}})
        self.assertEqual([], materialize_report(dialogue, self.work, text))
        self.assertEqual({'schema_version': 1}, json.loads((self.work / 'metadata.json').read_text()))
        self.assertEqual('# Skill', (self.work / 'SKILL.md').read_text())
        self.assertFalse((self.root / 'unexpected').exists())

    def test_characterization_array_maps_to_existing_consumer_document(self):
        command = replace(self.command, stage_id='contract_draft')
        dialogue = prepare_dialogue(command, self.root, 'Characterize the source.')
        declaration = {'path': '.modport/evidence/damage.json', 'evidence_kind': 'runtime',
                       'executor': 'junit', 'runtime_operations': ['damage'],
                       'test_source_files': ['.modport/DamageTest.java']}
        text = json.dumps({'schema_version': 1,
                          'test_evidence': [{'test_id': 'damage', 'declaration': declaration}]})
        self.assertEqual([], materialize_report(dialogue, self.work, text))
        document = json.loads((self.work / '.modport/functional-contract.json').read_text())
        self.assertEqual({'damage': declaration}, document['test_evidence'])
        self.assertEqual(text, (dialogue['directory'] / 'final-report.txt').read_text())

    def test_v31_contract_review_materialization_preserves_assertion_reviews_for_consumer(self):
        command = replace(self.command, stage_id='contract_review', options={
            **self.command.options, 'workflow_version': 31,
        })
        dialogue = prepare_dialogue(command, self.root, STAGE_PROMPTS['contract_review'],
            required_paths=('.modport/contract-review.json', '.modport/test-assessment.json'))
        schema = json.loads(dialogue['schema_path'].read_text(encoding='utf-8'))
        self.assertIn('assertion_reviews', schema['properties'])

        anchor = {'path': 'src/main/java/example/Item.java',
                  'start_line': 12, 'end_line': 14}
        assessment = {'assertion_id': 'item-use-result', 'source_anchor': anchor,
                      'status': 'supported',
                      'reasoning': 'The source returns success after the use action changes state.'}
        reply = json.dumps({'verdict': 'approved', 'findings': [],
                            'report': 'The assertion matches the source operation.',
                            'assertion_reviews': [assessment]})
        self.assertEqual([], materialize_report(dialogue, self.work, reply))

        from modport.agent_reports import review_decision
        from modport.handlers import _assertion_review_diagnostics
        materialized = (self.work / '.modport/contract-review.json').read_text(encoding='utf-8')
        consumed = review_decision(materialized)
        contract = {'behaviors': [{'assertion_contracts': [{
            'assertion_id': 'item-use-result', 'source_anchor': anchor,
        }]}]}
        self.assertEqual([assessment], consumed['assertion_reviews'])
        self.assertEqual([], _assertion_review_diagnostics(contract, consumed))

    def test_supervisor_nullable_slots_preserve_original_decision_semantics(self):
        ids = [f'execution-{index}' for index in range(5)]
        for decision, intervention in (
            ('continue', {'prompt': None, 'task_ids': None, 'stage': None, 'profile': None}),
            ('targeted_fix', {'prompt': 'Inspect the original conflict.', 'task_ids': None,
                              'stage': None, 'profile': None}),
        ):
            with self.subTest(decision=decision):
                self.calls.clear()
                self.answer = json.dumps({'schema_version': 1, 'decision': decision,
                    'reason': 'Inspect the original evidence.', 'evidence_execution_ids': ids,
                    'process_improvements': [], 'intervention': intervention})
                command = replace(self.command, stage_id='supervisor',
                    payload={'supervision_packet': {'evidence_execution_ids': ids}})
                with patch('modport.handlers._exec_agent', side_effect=self.execute):
                    result = SupervisorHandler()(command)
                self.assertEqual('completed', result.status, result.detail)
                self.assertEqual(decision, result.outputs['supervisor_decision']['decision'])
                self.assertEqual(self.answer, (self.root / result.outputs['last_message']).read_text().strip())
                if decision == 'continue':
                    self.assertNotIn('intervention', result.outputs['supervisor_decision'])

    def test_skill_generation_uses_same_two_turns_and_materializes_real_bundle(self):
        import test_skill_runtime
        from modport.skill_runtime import build_skill_registry
        fixture = test_skill_runtime.SkillRuntimeTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.java_only()
        source = fixture.candidate()
        outputs = {name: ((source / name).read_text() if name.endswith('.md')
                         else json.loads((source / name).read_text()))
                   for name in ('SKILL.md', 'metadata.json', 'rules.json', 'coverage.json', 'evidence.json')}
        self.answer = json.dumps({'outputs': outputs})
        command = fixture.command('java_diff', options={'workflow_version': 17,
                                  'agent_dialogue_policy': POLICY})
        with patch('modport.handlers._exec_agent', side_effect=self.execute):
            result = build_skill_registry()['java_diff'](command)
        self.assertEqual('completed', result.status, result.detail)
        self.assertEqual(2, len(self.calls))
        workspace = fixture.run / result.outputs['workspace']
        self.assertEqual(outputs['rules.json'], json.loads((workspace / 'rules.json').read_text()))
        self.assertTrue((workspace / 'manifest.json').is_file())
        self.assertIn('agent_plan', result.outputs['artifact_refs'])

    def test_scheduler_freezes_policy_without_a_second_assignment_charge(self):
        from modport.contracts import OperationInput
        from modport.models import Budget, MigrationRequest
        from modport.operations import MigrationOperations
        from modport.workflow import AGENT_STAGES, compile_migration_workflow
        request = MigrationRequest('fixture', 'https://example.invalid/mod.git',
            '1.20.1', '26.1.2', budget=Budget(max_agent_assignments=210))
        header = {'request': request.to_dict(), 'definition': compile_migration_workflow(request).to_dict(),
            'deadline_epoch': None, 'run_dir': str(self.root), 'initial_refs': {},
            'prior_findings': [], 'registry_revision': 'a' * 64, 'rubric_sha256': 'b' * 64}
        operations = MigrationOperations()
        for stage in ('code_review', 'contract_revise', 'target_revise', 'development_prepare'):
            with self.subTest(stage=stage):
                app = operations._new_application()
                app['agent_assignments'] = 135
                snapshot = {'run_id': 'fixture', 'state': 'running', 'waits': {}, 'tasks': {}}
                changes = operations._schedule(snapshot, header, app, stage)
                addition = next(row for row in changes if row['kind'] == 'add_task')
                command = OperationInput.from_dict(addition['command']['payload'])
                self.assertEqual(POLICY, command.options['agent_dialogue_policy'])
                self.assertEqual(135 + int(stage in AGENT_STAGES), app['agent_assignments'])


if __name__ == '__main__':
    unittest.main()
