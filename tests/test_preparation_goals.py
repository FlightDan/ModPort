"""Shared preparation retains task acceptance through native goals and integration."""
from dataclasses import replace
import json
import unittest
from unittest.mock import patch

from modport import handlers
from modport.analysis_contract import VERIFICATION_STAGES
from modport.development import CoderHandler, ImplementationHandler, _artifact
from modport.goal_planning import validate_goal
from modport.planning import DevelopmentPrepareHandler, _resolution
from modport.preparation_execution import PreparationIntegrateHandler
import test_planning
from test_development import git


class PreparationGoalTests(unittest.TestCase):
    def setUp(self):
        self.fixture = test_planning.PlanningTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.workflow_version = 11
        self.root, self.work = self.fixture.root, self.fixture.work
        for stage in ('migration_inventory', 'migration_plan', 'migration_tasks', 'parallel_review'):
            result = self.fixture.run_stage(stage, prepare=True,
                mutation=lambda doc, stage=stage: self._scope(stage, doc))
            self.assertEqual('completed', result.status, result.detail)

    @staticmethod
    def _scope(stage, doc):
        if stage == 'migration_inventory':
            for issue in doc['issues']:
                issue.update(resolution_scope='current', resolution_stage='implementation',
                             scope_reason='Required shared inputs must be prepared now')
        if stage == 'migration_tasks':
            for task in doc['tasks']:
                task['validation_checks'] = [{'id': task['id'] + '-json', 'type': 'json_valid',
                                             'path': task['id'], 'acceptance': task['acceptance']}]
                if task['id'] == 'b':
                    task.update(kind='prepare', dependencies=[])

    def prepare(self):
        result = DevelopmentPrepareHandler()(self.fixture.command('development_prepare'))
        self.assertEqual('completed', result.status, result.detail)
        self.fixture.refs.update(result.outputs['artifact_refs'])
        return result

    def coder(self, prepared, task, *, valid=True):
        context = {stage: self.fixture.refs[stage] for stage in
                   ('migration_inventory', 'migration_plan', 'migration_tasks', 'parallel_review')}
        goal = validate_goal({'task_id': task['id'], 'objective': task['objective'],
            'owned_paths': task['owned_paths'], 'dependencies': task['dependencies'],
            'acceptance': task['acceptance'], 'context_refs': context,
            'acceptance_report': '.modport/goal-reports/' + task['id'] + '.json',
            'stop_conditions': ['Pass every frozen check'], 'checks': task['validation_checks']}, task, context)
        goal_ref = _artifact(self.fixture.command('goal_prepare', identifier='goal-' + task['id']),
                             'coder-goal.json', json.dumps(goal).encode())
        command = replace(self.fixture.command('coder', identifier='coder-' + task['id']),
            payload={**self.fixture.command('coder').payload, **prepared.outputs,
                     'development_generation': 1, 'development_task': task, 'planning_context': context,
                     'dependency_patches': []},
            artifact_refs={**self.fixture.refs, 'coder_goal': goal_ref},
            options={'workflow_version': 11, 'workspace': 'workspaces/development/g1/' + task['id']})
        def fake(handler, delegated):
            self.assertIsNotNone(handler.native_goal)
            workspace = self.root / delegated.options['workspace']
            (workspace / task['id']).write_text('{}\n' if valid else 'INVALID JSON\n')
            git(workspace, 'add', task['id']); git(workspace, 'commit', '-m', 'prepare ' + task['id'])
            report = workspace / goal['acceptance_report']
            report.parent.mkdir(parents=True, exist_ok=True)
            report.write_text(json.dumps({'acceptance': [
                {'criterion': criterion, 'state': 'passed', 'evidence': [task['id'] + '-json']}
                for criterion in task['acceptance']]}))
            verdict = handler.goal_validator()
            return handlers._result(delegated, 'completed' if verdict['accepted'] else 'failed',
                outputs={'native_goal': {'host_accepted': verdict['accepted']}},
                detail='; '.join(verdict['failures']))
        with patch('modport.handlers.CodexStageHandler.__call__', fake):
            return CoderHandler()(command)

    def integration_command(self):
        prepared = self.prepare()
        results = [self.coder(prepared, task) for task in prepared.outputs['development_tasks']]
        for result in results:
            self.assertEqual('completed', result.status, result.detail)
        return replace(self.fixture.command('development_prepare_integrate'),
            payload={**self.fixture.command('development_prepare_integrate').payload, **prepared.outputs,
                     'development_generation': 1, 'development_results': [result.to_dict() for result in results]})

    def test_prepare_freezes_independent_goals_and_exact_source_acceptance(self):
        before = git(self.work, 'rev-parse', 'HEAD')
        with patch('modport.handlers.CodexStageHandler.__call__', side_effect=AssertionError('no coder during prepare')):
            prepared = self.prepare()
        self.assertEqual('preparation', prepared.outputs['development_kind'])
        self.assertEqual(before, git(self.work, 'rev-parse', 'HEAD'))
        self.assertEqual(['shared', 'b'], [task['id'] for task in prepared.outputs['development_tasks']])
        for task in prepared.outputs['development_tasks']:
            self.assertEqual([task['id']], task['source_task_ids'])
            self.assertEqual({task['id']: task['acceptance']}, task['source_acceptance'])
            self.assertEqual('json_valid', task['validation_checks'][0]['type'])

    def test_invalid_preparation_fails_its_native_goal_check(self):
        prepared = self.prepare()
        result = self.coder(prepared, prepared.outputs['development_tasks'][0], valid=False)
        self.assertEqual('failed', result.status, result.detail)
        self.assertFalse(result.outputs['native_goal']['host_accepted'])
        self.assertEqual('original\n', (self.work / 'shared').read_text())

    def test_integrated_preparation_is_consumed_by_fresh_independent_review(self):
        result = PreparationIntegrateHandler()(self.integration_command())
        self.assertEqual('completed', result.status, result.detail)
        self.fixture.refs.update(result.outputs['artifact_refs'])
        record = json.loads((self.root / result.outputs['artifact_refs']['development_prepare']['path']).read_text())
        self.assertEqual('development_prepare', record['stage'])
        self.assertEqual(['shared', 'b'], record['completed_task_ids'])
        self.assertEqual(['b', 'shared'], record['changed_paths'])
        def review(doc):
            doc['coupling_checks'] = []
            for task in doc['development_plan']['tasks']:
                task['validation_checks'] = [{'id': task['id'] + '-json', 'type': 'json_valid',
                                             'path': task['id'], 'acceptance': task['acceptance']}]
        reviewed = self.fixture.run_stage('parallel_review', identifier='post-prepare-review', mutation=review)
        self.assertEqual('completed', reviewed.status, reviewed.detail)
        implementation = ImplementationHandler()(self.fixture.command('implementation'))
        self.assertEqual('completed', implementation.status, implementation.detail)
        self.assertEqual(['a'], [task['id'] for task in implementation.outputs['development_tasks']])

    def test_stale_worktree_rejected_before_publishing(self):
        command = self.integration_command()
        (self.work / 'shared').write_text('concurrent change\n')
        result = PreparationIntegrateHandler()(command)
        self.assertEqual('blocked', result.status)
        self.assertEqual('concurrent change\n', (self.work / 'shared').read_text())
        self.assertEqual('original\n', (self.work / 'b').read_text())

    def test_missing_native_acceptance_prevents_publication(self):
        command = self.integration_command()
        command.payload['development_results'][0]['outputs']['native_goal']['host_accepted'] = False
        result = PreparationIntegrateHandler()(command)
        self.assertEqual('blocked', result.status)
        self.assertIn('host acceptance', result.detail)
        self.assertEqual('original\n', (self.work / 'shared').read_text())

    def test_post_integration_failure_rolls_back_with_fresh_cleanup_deadline(self):
        command = self.integration_command()
        before = git(self.work, 'rev-parse', 'HEAD')
        from modport import preparation_execution
        original_git = preparation_execution._git
        seen_deadlines = []
        def observe_git(cmd, *args, **kwargs):
            if 'reset' in args:
                seen_deadlines.append(cmd.options['deadline_epoch'])
                self.assertGreater(cmd.options['deadline_epoch'], preparation_execution.time.time())
            return original_git(cmd, *args, **kwargs)
        with (patch('modport.preparation_execution._artifact', side_effect=OSError('publish failed')),
              patch('modport.preparation_execution._git', side_effect=observe_git)):
            result = PreparationIntegrateHandler()(command)
        self.assertEqual('blocked', result.status)
        self.assertEqual(1, len(seen_deadlines))
        self.assertEqual(before, git(self.work, 'rev-parse', 'HEAD'))
        self.assertEqual('original\n', (self.work / 'shared').read_text())

    def test_downstream_stages_match_final_verification_vocabulary(self):
        command = self.fixture.command('migration_inventory')
        for stage in VERIFICATION_STAGES:
            self.assertEqual(('downstream', stage), _resolution(command, {
                'resolution_scope': 'downstream', 'resolution_stage': stage, 'scope_reason': 'later evidence'}))
        for stage in ('test_design', 'code_review', 'acceptance_build', 'gap_review'):
            with self.assertRaises(ValueError):
                _resolution(command, {'resolution_scope': 'downstream', 'resolution_stage': stage,
                                      'scope_reason': 'later evidence'})
