"""Native coder verification runs inside the same goal completion callback."""
from dataclasses import replace
import json
import os
import unittest
from unittest.mock import patch

from modport.development import _artifact, validate_plan
from modport.handlers import _result
import test_development
from test_development import git, task


class CoderGoalTests(unittest.TestCase):
    def setUp(self):
        self.fixture = test_development.IsolatedDevelopmentTests()
        self.fixture.workflow_version = 11
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.root = self.fixture.root
        command = self.fixture.command('coder', 'a')
        context = {}
        for index in range(4):
            context[f'round{index}'] = _artifact(command, f'context{index}.json', b'{}')
        item = command.payload['development_task']
        self.goal = {'task_id': 'a', 'objective': item['objective'],
                     'owned_paths': item['owned_paths'], 'dependencies': [],
                     'acceptance': item['acceptance'], 'context_refs': context,
                     'stop_conditions': ['Stop if host budget expires'],
                     'acceptance_report': '.modport/goal-reports/a.json',
                     'checks': [{'id': 'file', 'type': 'file_exists', 'path': 'a.txt',
                                 'acceptance': item['acceptance']}]}
        ref = _artifact(command, 'coder-goal.json', json.dumps(self.goal).encode())
        self.command = replace(command, payload={**command.payload, 'planning_context': context},
                               options={**command.options, 'workflow_version': 11},
                               artifact_refs={**command.artifact_refs, 'coder_goal': ref})

    def report(self, workspace):
        path = workspace / self.goal['acceptance_report']
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({'acceptance': [{'criterion': self.goal['acceptance'][0],
                                                   'state': 'passed', 'evidence': ['file']}]}))
        return path

    def run_coder(self, fake):
        with patch('modport.handlers.CodexStageHandler.__call__', fake):
            return self.fixture.registry['coder'](self.command)

    def test_dirty_candidate_feedback_then_accepts_and_archives_report(self):
        verdicts = []
        def fake(handler, command):
            self.assertEqual('a', handler.native_goal['task_id'])
            self.assertIn('deadline_epoch', command.options)
            workspace = self.root / command.options['workspace']
            (workspace / 'a.txt').write_text('updated')
            report = self.report(workspace)
            verdicts.append(handler.goal_validator())
            self.assertFalse(verdicts[-1]['accepted'])
            self.assertIn('dirty', ' '.join(verdicts[-1]['failures']))
            self.assertTrue(report.exists())
            git(workspace, 'add', 'a.txt'); git(workspace, 'commit', '-m', 'coder')
            verdicts.append(handler.goal_validator())
            self.assertTrue(verdicts[-1]['accepted'], verdicts[-1])
            return _result(command, 'completed', outputs={'native_goal': {'host_accepted': True}})
        result = self.run_coder(fake)
        self.assertEqual('completed', result.status, result.detail)
        self.assertEqual(['a.txt'], result.outputs['paths'])
        self.assertFalse((self.root / self.command.options['workspace'] / self.goal['acceptance_report']).exists())
        archived = result.outputs['artifact_refs']['goal_acceptance_report']
        self.assertTrue((self.root / archived['path']).is_file())

    def test_legacy_goal_keeps_one_shared_deadline(self):
        observed = {}

        def fake(handler, command):
            observed['options'] = dict(command.options)
            workspace = self.root / command.options['workspace']
            (workspace / 'a.txt').write_text('legacy shared deadline\n')
            git(workspace, 'add', 'a.txt'); git(workspace, 'commit', '-m', 'legacy')
            self.report(workspace)
            self.assertTrue(handler.goal_validator()['accepted'])
            return _result(command, 'completed', outputs={
                'native_goal': {'host_accepted': True}})

        result = self.run_coder(fake)
        self.assertEqual('completed', result.status, result.detail)
        self.assertIn('deadline_epoch', observed['options'])
        self.assertNotIn('model_deadline_epoch', observed['options'])
        self.assertNotIn('host_settlement_deadline_epoch', observed['options'])

    def test_v17_goal_edits_and_hands_off_while_host_commits_candidate(self):
        original_objective = self.goal['objective']
        self.command = replace(
            self.command,
            options={**self.command.options, 'workflow_version': 17},
        )
        def fake(handler, command):
            self.assertTrue(command.options['host_collect_candidate'])
            self.assertEqual(original_objective, handler.native_goal['source_objective'])
            self.assertIn('host will collect and commit', handler.native_goal['objective'])
            self.assertIn('do not wait for builds', handler.native_goal['objective'])
            workspace = self.root / command.options['workspace']
            (workspace / 'a.txt').write_text('uncommitted native goal edit\n')
            return _result(command, 'blocked', error_code='native_blocked', detail='handoff')
        result = self.run_coder(fake)
        self.assertEqual('blocked', result.status, result.detail)
        self.assertEqual(['a.txt'], result.outputs['paths'])
        self.assertIn('coder_patch', result.outputs['artifact_refs'])
        self.assertEqual(self.fixture.base, git(
            self.root / self.command.options['workspace'], 'rev-parse', 'HEAD'))

    def test_v17_never_collects_while_native_producer_may_still_be_live(self):
        self.command = replace(
            self.command,
            options={**self.command.options, 'workflow_version': 17},
        )
        def fake(_handler, command):
            workspace = self.root / command.options['workspace']
            (workspace / 'a.txt').write_text('still being written\n')
            return _result(command, 'blocked', outputs={'native_goal': {
                'owned_pid': 1234, 'producer_stopped': False}},
                error_code='native_goal_previous_process_alive', detail='producer live')
        result = self.run_coder(fake)
        self.assertEqual('blocked', result.status)
        self.assertNotIn('coder_patch', result.outputs.get('artifact_refs', {}))
        self.assertTrue(any('not proven stopped' in item
                            for item in result.outputs['business_diagnostics']))

    def test_v17_callback_validates_materialized_host_commit_projection(self):
        self.command = replace(
            self.command,
            options={**self.command.options, 'workflow_version': 17},
        )
        observed = []
        def fake(handler, command):
            workspace = self.root / command.options['workspace']
            (workspace / 'a.txt').write_text('candidate projection\n')
            (workspace / '.gradle').mkdir()
            (workspace / '.gradle/cache.bin').write_text('excluded')
            verdict = handler.goal_validator()
            observed.append(verdict)
            self.assertIn('candidate', verdict['evidence'])
            return _result(command, 'blocked', outputs={'native_goal': {
                'producer_stopped': True}}, error_code='handoff', detail='done')
        result = self.run_coder(fake)
        self.assertEqual('blocked', result.status, result.detail)
        self.assertEqual(['a.txt'], result.outputs['paths'])
        self.assertEqual(result.outputs['head'],
                         observed[0]['evidence']['candidate']['head'])

    def test_forbidden_commit_is_rejected_inside_callback_even_after_revert(self):
        def fake(handler, command):
            workspace = self.root / command.options['workspace']
            (workspace / 'b.txt').write_text('forbidden')
            git(workspace, 'add', 'b.txt'); git(workspace, 'commit', '-m', 'bad')
            git(workspace, 'revert', '--no-edit', 'HEAD')
            self.report(workspace)
            verdict = handler.goal_validator()
            self.assertFalse(verdict['accepted'])
            self.assertIn('unowned', ' '.join(verdict['failures']))
            return _result(command, 'blocked')
        self.assertEqual('blocked', self.run_coder(fake).status)

    def test_committed_report_is_rejected_inside_callback(self):
        def fake(handler, command):
            workspace = self.root / command.options['workspace']
            self.report(workspace)
            git(workspace, 'add', '.'); git(workspace, 'commit', '-m', 'report')
            verdict = handler.goal_validator()
            self.assertFalse(verdict['accepted'])
            self.assertIn('never be committed', ' '.join(verdict['failures']))
            return _result(command, 'blocked')
        self.assertEqual('blocked', self.run_coder(fake).status)

    def test_hidden_worktree_modifications_are_not_accepted(self):
        def fake(handler, command):
            workspace = self.root / command.options['workspace']
            self.report(workspace)
            path = workspace / 'a.txt'
            original = path.read_bytes()
            for flag in ('assume-unchanged', 'skip-worktree'):
                with self.subTest(flag=flag):
                    saved = path.stat()
                    git(workspace, 'update-index', '--' + flag, 'a.txt')
                    path.write_bytes(b'X' * len(original))
                    os.utime(path, ns=(saved.st_atime_ns, saved.st_mtime_ns))
                    self.assertNotIn('a.txt', git(workspace, 'status', '--porcelain'))
                    verdict = handler.goal_validator()
                    self.assertFalse(verdict['accepted'])
                    self.assertIn('differ from committed HEAD', ' '.join(verdict['failures']))
                    path.write_bytes(original)
                    git(workspace, 'update-index', '--no-' + flag, 'a.txt')
            return _result(command, 'blocked')
        self.assertEqual('blocked', self.run_coder(fake).status)

    def test_export_rejects_a_different_head_after_acceptance(self):
        def fake(handler, command):
            workspace = self.root / command.options['workspace']
            self.report(workspace)
            self.assertTrue(handler.goal_validator()['accepted'])
            (workspace / 'a.txt').write_text('changed after verification')
            git(workspace, 'add', 'a.txt'); git(workspace, 'commit', '-m', 'late change')
            return _result(command, 'completed', outputs={'native_goal': {'host_accepted': True}})
        result = self.run_coder(fake)
        self.assertEqual('blocked', result.status)
        self.assertFalse((self.root / 'artifacts/executions' / self.command.command_id / 'coder.patch').exists())

    def test_host_checks_use_snapshot_bound_to_committed_head(self):
        from modport.goal_validation import validate_goal_candidate
        def checked(command, snapshot, goal):
            workspace = self.root / command.options['workspace']
            self.assertNotEqual(snapshot, workspace)
            self.assertFalse((snapshot / '.git').exists())
            self.assertEqual((snapshot / 'a.txt').read_bytes(), (workspace / 'a.txt').read_bytes())
            return validate_goal_candidate(command, snapshot, goal)
        def fake(handler, command):
            workspace = self.root / command.options['workspace']
            self.report(workspace)
            self.assertTrue(handler.goal_validator()['accepted'])
            return _result(command, 'completed', outputs={'native_goal': {'host_accepted': True}})
        with patch('modport.goal_validation.validate_goal_candidate', side_effect=checked):
            result = self.run_coder(fake)
        self.assertEqual('completed', result.status, result.detail)

    def test_changed_context_is_rejected_before_launch(self):
        context = self.command.payload['planning_context']['round0']
        (self.root / context['path']).write_text('{"changed":true}')
        with patch('modport.handlers.CodexStageHandler.__call__') as launch:
            result = self.fixture.registry['coder'](self.command)
        self.assertEqual('blocked', result.status)
        launch.assert_not_called()

    def test_workflow_11_requires_goal_before_launch(self):
        command = replace(self.command, artifact_refs=self.fixture.frozen.outputs['artifact_refs'])
        with patch('modport.handlers.CodexStageHandler.__call__') as launch:
            result = self.fixture.registry['coder'](command)
        self.assertEqual('blocked', result.status)
        self.assertIn('required', result.detail)
        launch.assert_not_called()

    def test_workflow_12_rejects_missing_goal_or_static_regression_before_launch(self):
        for refs in (self.command.artifact_refs, self.fixture.frozen.outputs['artifact_refs']):
            command = replace(self.command, options={**self.command.options, 'workflow_version': 12},
                              artifact_refs=refs)
            with self.subTest(refs=list(refs)), patch('modport.handlers.CodexStageHandler.__call__') as launch:
                result = self.fixture.registry['coder'](command)
                self.assertEqual('blocked', result.status)
                launch.assert_not_called()

    def test_workflow_12_self_review_failure_returns_to_same_goal_before_export(self):
        from hashlib import sha256
        ref = self.command.artifact_refs['development_plan']
        path = self.root / ref['path']
        plan = json.loads(path.read_text())
        item = plan['tasks'][0]
        item.update(validation_kind='structural', structural_reason='Only text file structure changes',
                    validation_checks=self.goal['checks'])
        path.write_text(json.dumps(plan))
        refs = {**self.command.artifact_refs, 'development_plan': {**ref, 'sha256': sha256(path.read_bytes()).hexdigest()}}
        self.command = replace(self.command, payload={**self.command.payload, 'development_task': item},
                               artifact_refs=refs, options={**self.command.options, 'workflow_version': 12})
        def fake(handler, command):
            workspace = self.root / command.options['workspace']
            (workspace / 'a.txt').write_text('reviewed change')
            git(workspace, 'add', 'a.txt'); git(workspace, 'commit', '-m', 'change')
            report = self.report(workspace)
            self.assertFalse(handler.goal_validator()['accepted'])
            body = json.loads(report.read_text())
            body['self_check'] = {'state': 'passed', 'reviewed_paths': ['a.txt/not-the-change'],
                                  'checks': ['file'], 'summary': 'Reviewed the change.'}
            report.write_text(json.dumps(body))
            verdict = handler.goal_validator()
            self.assertFalse(verdict['accepted'])
            self.assertIn('omits changed candidate paths', ' '.join(verdict['failures']))
            body['self_check']['reviewed_paths'] = ['a.txt']
            report.write_text(json.dumps(body))
            self.assertTrue(handler.goal_validator()['accepted'])
            return _result(command, 'completed', outputs={'native_goal': {'host_accepted': True}})
        result = self.run_coder(fake)
        self.assertEqual('completed', result.status, result.detail)

        proof_ref = result.outputs['artifact_refs']['goal_host_validation']
        proof = json.loads((self.root / proof_ref['path']).read_text())
        self.assertEqual(proof['candidate']['head'], result.outputs['head'])
        self.assertEqual(proof['acceptance_report']['report']['self_check']['reviewed_paths'], ['a.txt'])
        exported = {ref['path'] for ref in result.outputs['artifact_refs'].values()}
        for record in (proof['candidate']['candidate_manifest'], proof['acceptance_report'],
                       proof['checks']['file']):
            self.assertIn(record['path'], exported)
            self.assertTrue((self.root / record['path']).is_file())

    def test_reference_verification_does_not_clone_large_operation_input(self):
        from modport.development import _verified
        ref = self.command.artifact_refs['development_plan']
        with patch('modport.development.replace', side_effect=AssertionError('must not clone command')):
            self.assertEqual(_verified(self.command, ref), self.root / ref['path'])

    def interrupt_coder(self):
        def fake(handler, command):
            workspace = self.root / command.options['workspace']
            (workspace / 'a.txt').write_text('unfinished candidate')
            return _result(command, 'failed', outputs={'native_goal': {'host_accepted': False}})
        self.assertEqual('failed', self.run_coder(fake).status)

    def test_explicit_resume_preserves_checkout_and_start_commit(self):
        self.interrupt_coder()
        original = self.command
        setup = self.root / 'artifacts/executions' / original.command_id / 'coder-setup.json'
        start = json.loads(setup.read_text())['record']['start_commit']
        self.command = replace(original, options={**original.options, 'native_goal_resume': True})
        def fake(handler, command):
            workspace = self.root / command.options['workspace']
            self.assertTrue(command.options['native_goal_resume'])
            self.assertEqual((workspace / 'a.txt').read_text(), 'unfinished candidate')
            git(workspace, 'add', 'a.txt'); git(workspace, 'commit', '-m', 'finish resumed task')
            self.report(workspace)
            self.assertTrue(handler.goal_validator()['accepted'])
            return _result(command, 'completed', outputs={'native_goal': {'host_accepted': True}})
        from modport import development
        with patch('modport.development._git', wraps=development._git) as commands, \
             patch('modport.development._apply', side_effect=AssertionError('cannot reapply dependencies')):
            result = self.run_coder(fake)
        self.assertEqual('completed', result.status, result.detail)
        self.assertEqual(result.outputs['start'], start)
        self.assertFalse(any(call.args[2] == 'clone' for call in commands.call_args_list))

    def test_duplicate_without_resume_flag_never_launches(self):
        self.interrupt_coder()
        with patch('modport.handlers.CodexStageHandler.__call__') as launch:
            result = self.fixture.registry['coder'](self.command)
        self.assertEqual('blocked', result.status)
        self.assertIn('must be new', result.detail)
        launch.assert_not_called()

    def test_resume_rejects_missing_tampered_or_changed_setup(self):
        self.interrupt_coder()
        original = self.command
        self.command = replace(original, options={**original.options, 'native_goal_resume': True})
        setup = self.root / 'artifacts/executions' / original.command_id / 'coder-setup.json'
        saved = setup.read_bytes()
        for kind in ('missing', 'malformed', 'tampered', 'changed_context'):
            setup.write_bytes(saved)
            command = self.command
            if kind == 'missing':
                setup.unlink()
            elif kind == 'malformed':
                setup.write_text('[]')
            elif kind == 'tampered':
                envelope = json.loads(saved)
                envelope['record']['start_commit'] = '0' * 40
                setup.write_text(json.dumps(envelope))
            else:
                command = replace(command, payload={**command.payload, 'goal_generation': 99})
            with self.subTest(kind=kind), patch('modport.handlers.CodexStageHandler.__call__') as launch:
                result = self.fixture.registry['coder'](command)
                self.assertEqual('blocked', result.status)
                launch.assert_not_called()

    def test_resume_cannot_create_a_new_checkout(self):
        command = replace(self.command, options={**self.command.options, 'native_goal_resume': True})
        with patch('modport.handlers.CodexStageHandler.__call__') as launch:
            result = self.fixture.registry['coder'](command)
        self.assertEqual('blocked', result.status)
        self.assertIn('existing workspace', result.detail)
        launch.assert_not_called()

    def test_contract_plan_ownership_cannot_escape_modport(self):
        plan = {'schema_version': 1, 'base_commit': 'a' * 40, 'shared_paths': [],
                'tasks': [task('repair', '.modport/functional-contract.json')]}
        validate_plan(plan, allow_contract=True)
        for owned in ['src', '.modport', '.modport/goal-reports', '.modport/goal-reports/a.json']:
            plan['tasks'][0]['owned_paths'] = [owned]
            with self.subTest(owned=owned), self.assertRaises(ValueError):
                validate_plan(plan, allow_contract=True)


if __name__ == '__main__':
    unittest.main()
