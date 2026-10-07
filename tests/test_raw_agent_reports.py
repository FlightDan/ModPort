import copy
import json
from dataclasses import replace
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from modport import handlers
from modport.agent_reports import review_decision
from modport.contracts import OperationInput, OperationResult
from modport.gap_research import GapResearchHandler, ResearchReviewHandler, RESEARCH_REPORT
from modport.goal_planning import GoalPreparationHandler
from modport.planning import PlanningHandler, approved_repair_development_plan
from test_development import git, task
import test_planning
from test_goal_planning import sample


class RawAgentReportsTests(unittest.TestCase):
    def test_review_prose_needs_only_explicit_routing_decision(self):
        raw = 'Found an issue. {broken JSON [\nMODPORT_DECISION: rejected'
        decision = review_decision(raw)
        self.assertEqual('rejected', decision['verdict'])
        self.assertEqual(raw, decision['raw_report'])
        with self.assertRaises(ValueError):
            review_decision('The author says approved but I disagree.')

    def test_raw_research_reaches_independent_reviewer(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / 'baseline'
            workspace.mkdir()
            gap = {'gap_id': 'knowledge:api', 'kind': 'knowledge', 'skill': 'platform',
                   'applicable': True, 'status': 'unresolved'}
            command = OperationInput('run', 'gap_research', 'gap_research', 'research:1', str(root),
                payload={'project_research_gaps': [gap], 'research_kinds': ['knowledge']})
            raw = 'API research: {forgot closing bracket. Still readable.\nUnknown: callback order.'
            def research(_):
                path = workspace / RESEARCH_REPORT
                path.parent.mkdir(parents=True)
                path.write_text(raw)
                return OperationResult('completed')
            with patch('modport.handlers.CodexStageHandler') as agent:
                agent.return_value.side_effect = research
                result = GapResearchHandler()(command)
            self.assertEqual('completed', result.status, result.detail)
            ref = result.outputs['artifact_refs']['gap_research']
            self.assertEqual(raw, (root / ref['path']).read_text())
            def review(_):
                (workspace / '.modport/research-review.json').write_text(
                    'Need callback source evidence. [\nMODPORT_DECISION: rejected')
                return OperationResult('completed')
            command = replace(command, stage_id='research_review', command_id='review:2', artifact_refs={'gap_research': ref})
            with patch('modport.handlers.CodexStageHandler') as agent:
                agent.return_value.side_effect = review
                result = ResearchReviewHandler()(command)
            self.assertEqual('completed', result.status, result.detail)
            self.assertEqual('rejected', result.outputs['verdict'])
            self.assertIn('callback source', result.outputs['prior_findings'][0]['report'])

    def test_raw_coder_context_uses_reviewed_checks_without_echo(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            assignment, context, goal = sample()
            assignment['validation_checks'] = copy.deepcopy(goal['checks'])
            for ref in context.values():
                (root / ref['path']).write_text('upstream prose {')
            raw = 'Implement metadata parsing in this Codex instance. [unclosed'
            path = root / 'answer.txt'
            path.write_text(raw)
            command = OperationInput('run', 'goal_prepare', 'goal_prepare', 'goal:1', str(root),
                payload={'development_task': assignment, 'planning_context': context,
                         'goal_scope': 'migration', 'goal_generation': 0})
            with patch('modport.handlers.CodexStageHandler') as agent:
                agent.return_value.return_value = OperationResult('completed', outputs={'last_message': str(path)})
                result = GoalPreparationHandler()(command)
            self.assertEqual('completed', result.status, result.detail)
            self.assertEqual(raw, result.outputs['goal']['raw_report'])
            self.assertEqual(assignment['validation_checks'], result.outputs['goal']['checks'])

    def test_raw_repair_chain_reaches_dispatch_and_rejects_unsafe_execution(self, reports=None):
        fixture = test_planning.PlanningTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.workflow_version = 12
        stages = ('target_diagnose', 'target_repair_plan', 'target_repair_tasks', 'target_repair_review')
        reports = reports or ['Diagnosis prose {', 'Strategy without fields ]', 'Four immediate tasks; later checks listed separately.']
        reviewed_task = task('a', 'a')
        reviewed_task.update(validation_kind='structural', structural_reason='Metadata-only fixture',
            validation_checks=[{'id': 'a-exists', 'type': 'file_exists', 'path': 'a',
                                'acceptance': reviewed_task['acceptance']}])
        decision = {'parallel_decision': 'sequential', 'reason': 'Independent evidence assessment',
                    'development_plan': {'schema_version': 1, 'base_commit': git(fixture.work, 'rev-parse', 'HEAD'),
                                         'shared_paths': [], 'tasks': [reviewed_task]}}
        for index, stage in enumerate(stages):
            command = replace(fixture.command(stage), payload=fixture.repair_payload(stage))
            def fake(handler, cmd, index=index):
                if index:
                    self.assertIn(reports[index - 1], handler.prompt)
                path = fixture.root / 'logs' / (cmd.command_id + '.txt')
                path.write_text(reports[index] if index < 3 else json.dumps(decision))
                return handlers._result(cmd, 'completed', outputs={'last_message': str(path)})
            with patch('modport.handlers.CodexStageHandler.__call__', fake):
                result = PlanningHandler()(command)
            self.assertEqual('completed', result.status, result.detail)
            fixture.refs.update(result.outputs['artifact_refs'])
        command = replace(fixture.command('target_revise'), payload=fixture.repair_payload('target_revise'))
        plan = approved_repair_development_plan(command)
        self.assertEqual(['a'], [row['id'] for row in plan['tasks']])
        from modport.prompts import build_prompt
        coder = replace(command, stage_id='coder', payload={**command.payload,
                        'development_task': plan['tasks'][0]})
        prompt = build_prompt('Implement the assigned task', coder, fixture.root, {},
                              {'rubric_id': 'rubric', 'rubric_version': 1})
        for raw in reports:
            self.assertIn(raw, prompt)
        from modport.planning import _validate_execution_review
        decision['base_commit'] = decision['development_plan']['base_commit']
        decision['development_plan']['tasks'][0]['owned_paths'] = ['../escape']
        with self.assertRaises(ValueError):
            _validate_execution_review(replace(command, stage_id='target_repair_review'), decision)

    def test_coder_handoff_prose_cannot_replace_actual_checks(self):
        import test_goal_validation
        fixture = test_goal_validation.GoalValidationTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        raw = 'Implemented parsing; reviewed changes. { no field structure'
        fixture.report.write_text(raw)
        (fixture.root / 'metadata/output.json').write_text('{}')
        result = fixture.validate()
        self.assertTrue(result['accepted'], result)
        self.assertEqual(raw, result['evidence']['acceptance_report']['raw_report'])
        (fixture.root / 'metadata/output.json').write_text('invalid')
        self.assertFalse(fixture.validate()['accepted'])
