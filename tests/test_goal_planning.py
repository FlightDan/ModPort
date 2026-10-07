import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from modport.contracts import OperationInput, OperationResult
from modport.goal_planning import GoalPreparationHandler, validate_goal


def sample():
    task = {'id': 'one', 'objective': 'Write valid metadata', 'owned_paths': ['metadata'],
            'dependencies': [], 'acceptance': ['Metadata is valid JSON']}
    context = {str(i): {'path': f'context-{i}.json'} for i in range(4)}
    goal = {'task_id': 'one', 'objective': 'Write valid metadata',
            'owned_paths': ['metadata'], 'dependencies': [], 'acceptance': task['acceptance'][:],
            'context_refs': context, 'stop_conditions': ['All frozen checks pass'],
            'acceptance_report': '.modport/goal-reports/one.json',
            'checks': [{'id': 'metadata-json', 'type': 'json_valid', 'path': 'metadata/output.json',
                        'acceptance': task['acceptance'][:]}]}
    return task, context, goal


class GoalPlanningTests(unittest.TestCase):
    def test_v17_uses_task_as_advisory_goal_without_check_schema_gate(self):
        task, context, _ = sample()
        task.update(owned_paths=['src/main/java'], acceptance=[],
                    validation_checks=[{'type': 'unknown', 'path': '../diagnostic'}])
        goal = validate_goal('free form output', task, {}, require_double_check=True,
                             gates_disabled=True)
        self.assertEqual(['src/main/java'], goal['owned_paths'])
        self.assertEqual([], goal['acceptance'])
        self.assertEqual(task['validation_checks'], goal['checks'])
        self.assertEqual('.modport/goal-reports/one.json', goal['acceptance_report'])

    def test_v17_prepares_goal_without_context_checks_scope_or_agent_report(self):
        with tempfile.TemporaryDirectory() as directory:
            task, _, _ = sample()
            command = OperationInput('run', 'task', 'goal_prepare', 'cmd', directory,
                payload={'development_task': task,
                         'planning_context': {'missing': {'path': 'not-there.json'}},
                         'goal_scope': 'unknown', 'goal_generation': 'unknown'},
                options={'workflow_version': 17})
            result = OperationResult('failed', detail='context author failed',
                                     error_code='agent_failed')
            with patch('modport.handlers.CodexStageHandler') as handler:
                handler.return_value.return_value = result
                output = GoalPreparationHandler()(command)
            self.assertEqual('completed', output.status, output.detail)
            self.assertEqual([], output.outputs['goal']['checks'])
            self.assertEqual({'missing': {'path': 'not-there.json'}},
                             output.outputs['goal']['context_refs'])
            self.assertGreaterEqual(len(output.outputs['business_diagnostics']), 4)
            self.assertFalse(handler.call_args.kwargs['baseline'])

    def test_rejects_changed_boundary_criteria_context_and_unmapped_criteria(self):
        task, context, goal = sample()
        for key, value in [('owned_paths', ['other']), ('dependencies', ['other']),
                           ('acceptance', ['Waived']), ('context_refs', {}), ('checks', [])]:
            with self.subTest(key=key), self.assertRaises(ValueError):
                validate_goal({**goal, key: value}, task, context)

    def test_rejects_shell_and_escape_and_self_report_checks(self):
        task, context, goal = sample()
        for change in [{'type': 'shell'}, {'path': '../secret'}, {'command': 'echo pass'},
                       {'path': goal['acceptance_report']}]:
            bad = copy.deepcopy(goal)
            bad['checks'][0].update(change)
            with self.subTest(change=change), self.assertRaises(ValueError):
                validate_goal(bad, task, context)

    def test_cannot_weaken_reviewed_checks(self):
        task, context, goal = sample()
        task['validation_checks'] = copy.deepcopy(goal['checks'])
        goal['checks'][0]['type'] = 'file_exists'
        with self.assertRaisesRegex(ValueError, 'frozen validation checks'):
            validate_goal(goal, task, context)

    def test_handler_preserves_refs_and_creates_goal_artifact(self):
        from hashlib import sha256
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            task, context, goal = sample()
            for ref in context.values():
                (root / ref['path']).write_text('{}')
                ref['sha256'] = sha256(b'{}').hexdigest()
            (root / 'last.json').write_text(json.dumps(goal))
            command = OperationInput('run', 'task', 'goal_prepare', 'cmd', directory,
                payload={'development_task': task, 'planning_context': context,
                         'goal_scope': 'migration', 'goal_generation': 1})
            result = OperationResult('completed', outputs={'last_message': 'last.json'})
            with patch('modport.handlers.CodexStageHandler') as handler:
                handler.return_value.return_value = result
                output = GoalPreparationHandler()(command)
            self.assertEqual(output.status, 'completed', output.detail)
            self.assertTrue(handler.call_args.kwargs['read_only'])
            from modport.author_contracts import acceptance_report_prompt
            self.assertIn(acceptance_report_prompt(), handler.call_args.args[0])
            ref = output.outputs['artifact_refs']['coder_goal']
            self.assertEqual(json.loads((root / ref['path']).read_text())['context_refs'], context)
            with patch('modport.handlers.CodexStageHandler') as handler:
                handler.return_value.return_value = result
                repeated = GoalPreparationHandler()(command)
            self.assertEqual(repeated.status, 'failed')
            self.assertIn('already exists', repeated.detail)

    def test_workflow_12_requires_regression_or_reviewed_structural_reason(self):
        task, context, goal = sample()
        with self.assertRaisesRegex(ValueError, 'frozen validation_checks'):
            validate_goal(goal, task, context, require_double_check=True)
        task['validation_checks'] = copy.deepcopy(goal['checks'])
        with self.assertRaisesRegex(ValueError, 'gradle_regression'):
            validate_goal(goal, task, context, require_double_check=True)
        task['validation_kind'] = 'structural'
        with self.assertRaisesRegex(ValueError, 'structural_reason'):
            validate_goal(goal, task, context, require_double_check=True)
        task['structural_reason'] = 'Metadata JSON syntax only'
        result = validate_goal(goal, task, context, require_double_check=True)
        self.assertEqual(result['structural_reason'], task['structural_reason'])
        with self.assertRaisesRegex(ValueError, 'frozen validation_kind'):
            validate_goal({**goal, 'validation_kind': 'regression'}, task, context, require_double_check=True)

    def test_regression_report_paths_must_be_explicit_safe_build_xml(self):
        task, context, goal = sample()
        goal['checks'] = [{'id': 'regression', 'type': 'gradle_regression', 'tasks': [':module:test'],
                           'reports': ['module/build/test-results/test/TEST-Suite.xml'],
                           'acceptance': task['acceptance']}]
        task['validation_checks'] = copy.deepcopy(goal['checks'])
        self.assertEqual(validate_goal(goal, task, context, require_double_check=True)['validation_kind'], 'regression')
        for reports in ([], ['build/*.xml'], ['../build/TEST.xml'], ['src/TEST.xml'],
                        ['build/test.json'], ['build/a.xml', 'build/a.xml'], ['build/.git/a.xml']):
            bad = copy.deepcopy(goal)
            bad['checks'][0]['reports'] = reports
            with self.subTest(reports=reports), self.assertRaises(ValueError):
                validate_goal(bad, task, context, require_double_check=True)

    def test_regression_must_cover_every_criterion_despite_supplementary_static_checks(self):
        task, context, goal = sample()
        task['acceptance'].append('Existing metadata behavior is preserved')
        goal['acceptance'] = task['acceptance'][:]
        regression = {'id': 'regression', 'type': 'gradle_regression', 'tasks': [':test'],
                      'reports': ['build/test-results/test/TEST-Metadata.xml'],
                      'acceptance': [task['acceptance'][1]]}
        goal['checks'].append(regression)
        task['validation_checks'] = copy.deepcopy(goal['checks'])
        with self.assertRaisesRegex(ValueError, 'cover every acceptance criterion'):
            validate_goal(goal, task, context, require_double_check=True)
        regression['acceptance'] = task['acceptance'][:]
        task['validation_checks'] = copy.deepcopy(goal['checks'])
        result = validate_goal(goal, task, context, require_double_check=True)
        self.assertEqual([check['type'] for check in result['checks']], ['json_valid', 'gradle_regression'])
        regression['acceptance'] = [task['acceptance'][1]]
        goal['checks'].append({**regression, 'id': 'metadata-regression', 'acceptance': [task['acceptance'][0]]})
        task['validation_checks'] = copy.deepcopy(goal['checks'])
        self.assertEqual(validate_goal(goal, task, context, require_double_check=True)['validation_kind'], 'regression')
