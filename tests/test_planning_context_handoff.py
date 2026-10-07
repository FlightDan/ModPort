"""The v23 planner's authenticated context reaches the goal and coder boundary."""

import json
from hashlib import sha256
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from modport.contracts import OperationInput
from modport.development import ImplementationHandler
from modport.goal_planning import GoalPreparationHandler
from modport.handlers import _result
from modport.operations import MigrationOperations
from modport.planning import PlanningHandler
from modport.workflow import WORKFLOW_VERSION


def _git(root, *args):
    return subprocess.check_output(
        ['git', '-c', 'user.name=Test', '-c', 'user.email=test@example.invalid', *args],
        cwd=root, stderr=subprocess.DEVNULL).decode().strip()


class PlanningContextHandoffTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.worktree = self.root / 'worktree'
        self.worktree.mkdir()
        _git(self.worktree, 'init')
        source = self.worktree / 'src/main/java/example/Registration.java'
        source.parent.mkdir(parents=True)
        source.write_text('class Registration {}\n')
        _git(self.worktree, 'add', '.')
        _git(self.worktree, 'commit', '-m', 'source snapshot')
        self.base = _git(self.worktree, 'rev-parse', 'HEAD')

    def _artifact(self, name, value):
        path = self.root / 'artifacts' / name
        path.parent.mkdir(parents=True, exist_ok=True)
        data = json.dumps(value, ensure_ascii=False).encode()
        path.write_bytes(data)
        return {'path': path.relative_to(self.root).as_posix(),
                'sha256': sha256(data).hexdigest(), 'media_type': 'application/json'}

    def test_v23_plan_report_and_inventory_reach_goal_and_coder_artifact(self):
        inventory = self._artifact('migration-inventory.json', {
            'schema_version': 1, 'candidate_id': self.base,
            'issues': [{'issue_id': 'INV-001', 'summary': 'Registration API moved',
                        'evidence_refs': [], 'locations': [
                            {'path': 'src/main/java/example/Registration.java', 'line': 1}]}]})
        scan = self._artifact('mod-scan-report.json', {
            'schema_version': 1, 'scan_complete': True, 'skills': {}})
        report = ('# Concrete migration plan\n'
                  'Replace the removed registration call at the source location.\n'
                  '```json\n' + json.dumps({'development_plan': {'tasks': [{
                      'id': 'registration',
                      'objective': ('In src/main/java/example/Registration.java, replace the removed '
                                    'registration call with the locked target API and update its caller; '
                                    'verify registration occurs once.'),
                      'owned_paths': ['src/main/java/example/Registration.java'],
                      'dependencies': [], 'issue_ids': ['INV-001'],
                      'acceptance': ['Registration occurs once through the target lifecycle.'],
                      'validation_checks': []}] }}, ensure_ascii=False)
                  + '\n```\n')
        planning_command = OperationInput(
            'planning-run', 'migration_plan', 'migration_plan', 'migration-plan-v23',
            str(self.root), options={'workflow_version': WORKFLOW_VERSION,
                                     'workspace': 'worktree'},
            artifact_refs={'migration_inventory': inventory,
                           'mod_scan_report': scan})

        def author_plan(_handler, command):
            return _result(command, 'completed', outputs={'raw_report': report})

        with patch('modport.handlers.CodexStageHandler.__call__', author_plan):
            planned = PlanningHandler()(planning_command)
        self.assertEqual('completed', planned.status, planned.detail)
        plan_refs = planned.outputs['artifact_refs']
        self.assertTrue({
            'migration_plan', 'planning_report:migration_plan',
            'planning_input_index', 'planning_input_report', 'development_plan',
        } <= set(plan_refs), set(plan_refs))

        implementation_command = OperationInput(
            'planning-run', 'implementation', 'implementation', 'implementation-v23',
            str(self.root), options={'workflow_version': WORKFLOW_VERSION,
                                     'workspace': 'worktree'},
            artifact_refs=plan_refs)
        implementation = ImplementationHandler()(implementation_command)
        self.assertEqual('completed', implementation.status, implementation.detail)

        inventory_result = _result(OperationInput(
            'planning-run', 'migration_inventory', 'migration_inventory',
            'inventory-v23', str(self.root)), 'completed',
            outputs={'artifact_refs': {'migration_inventory': inventory}})
        scan_result = _result(OperationInput(
            'planning-run', 'mod_scan', 'mod_scan', 'scan-v23', str(self.root)),
            'completed', outputs={'artifact_refs': {'mod_scan_report': scan}})
        app = {'effective': {'migration_inventory': inventory_result.to_dict(),
                             'mod_scan': scan_result.to_dict(),
                             'migration_plan': planned.to_dict(),
                             'implementation': implementation.to_dict()},
               'development_generation': 0}
        header = {'initial_refs': {},
                  'definition': {'workflow_version': WORKFLOW_VERSION}}
        operations = MigrationOperations(handlers={})
        operations._start_development_group(header, app, 'implementation',
                                            implementation.outputs)
        group = app['active_group']
        context = group['planning_context']
        self.assertTrue({
            'migration_plan', 'planning_report:migration_plan',
            'planning_input_index', 'planning_input_report',
            'migration_inventory', 'mod_scan_report', 'development_plan',
        } <= set(context))
        self.assertNotIn('migration_tasks', context)
        self.assertNotIn('parallel_review', context)

        observed_prompts = []

        def author_goal(handler, command):
            observed_prompts.append(handler.prompt)
            message = self.root / 'logs' / 'goal-context.txt'
            message.parent.mkdir(parents=True, exist_ok=True)
            message.write_text('The inventory issue maps to this task and target API change.')
            return _result(command, 'completed', outputs={'last_message': str(message)})

        goal_command = OperationInput(
            'planning-run', 'goal.g1.registration', 'goal_prepare', 'goal-v23',
            str(self.root), payload={
                'development_task': group['tasks'][0],
                'planning_context': context,
                'goal_scope': 'migration', 'goal_generation': 1},
            options={'workflow_version': WORKFLOW_VERSION},
            artifact_refs=group['artifact_refs'])
        with patch('modport.handlers.CodexStageHandler.__call__', author_goal):
            goal = GoalPreparationHandler()(goal_command)
        self.assertEqual('completed', goal.status, goal.detail)
        coder_goal = goal.outputs['artifact_refs']['coder_goal']
        coder_goal_path = self.root / coder_goal['path']
        frozen_goal = json.loads(coder_goal_path.read_text())
        self.assertEqual(context, frozen_goal['context_refs'])
        self.assertIn('Read the authenticated planning references supplied with this task',
                      observed_prompts[0])
        self.assertIn('Do not assume that separate task-synthesis or dispatch reports exist',
                      observed_prompts[0])
        report_path = self.root / context['planning_input_report']['path']
        self.assertIn('INV-001', report_path.read_text())

    def test_v18_keeps_its_existing_context_aliases(self):
        legacy_refs = {
            name: {'path': f'artifacts/{name}', 'sha256': str(index) * 64}
            for index, name in enumerate(('current_plan', 'migration_tasks', 'parallel_review'))
        }
        legacy_refs['development_plan'] = {'path': 'artifacts/development_plan', 'sha256': 'a' * 64}
        new_refs = {
            name: {'path': f'artifacts/{name}', 'sha256': 'b' * 64}
            for name in ('migration_plan', 'planning_report:migration_plan',
                         'planning_input_index', 'planning_input_report', 'migration_inventory',
                         'mod_scan_report')
        }
        header = {'initial_refs': legacy_refs,
                  'definition': {'workflow_version': 18}}
        app = {'development_generation': 0, 'effective': {}}
        MigrationOperations(handlers={})._start_development_group(
            header, app, 'implementation', {
                'development_tasks': [{'id': 'legacy', 'objective': 'Keep the frozen route'}],
                'development_base': self.base, 'goal_scope': 'migration',
                'artifact_refs': {**legacy_refs, **new_refs}})
        self.assertEqual(set(legacy_refs), set(app['active_group']['planning_context']))

    def test_v18_goal_preparation_keeps_its_frozen_prompt(self):
        task = {'id': 'legacy', 'objective': 'Preserve the legacy assignment',
                'owned_paths': ['src'], 'dependencies': [], 'acceptance': []}
        command = OperationInput(
            'planning-run', 'goal.g1.legacy', 'goal_prepare', 'goal-v18', str(self.root),
            payload={'development_task': task, 'planning_context': {},
                     'goal_scope': 'migration', 'goal_generation': 1},
            options={'workflow_version': 18})
        prompts = []

        def author_goal(handler, received):
            prompts.append(handler.prompt)
            return _result(received, 'failed', detail='diagnostic only')

        with patch('modport.handlers.CodexStageHandler.__call__', author_goal):
            prepared = GoalPreparationHandler()(command)
        self.assertEqual('completed', prepared.status, prepared.detail)
        self.assertIn('Read the final authenticated Markdown plan, task synthesis, dispatch record',
                      prompts[0])
        self.assertNotIn('Do not assume that separate task-synthesis or dispatch reports exist',
                         prompts[0])


if __name__ == '__main__':
    unittest.main()
