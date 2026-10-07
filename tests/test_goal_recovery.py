"""Explicit SDK effect recovery retains native goal and frozen input identity."""
from contextlib import contextmanager
from dataclasses import replace
from hashlib import sha256
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from modport.contracts import OperationInput, OperationResult
from modport.evidence import atomic_json, digest, file_digest, read_json
from modport.kernel_runtime import resume_interrupted_goal


class NativeGoalRecoveryTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.workspace = self.root / 'workspaces/development/g0/task'
        self.workspace.mkdir(parents=True)
        goal_path = self.root / 'artifacts/goal.json'
        atomic_json(goal_path, {'objective': 'bounded objective'})
        self.goal_ref = {'path': 'artifacts/goal.json', 'sha256': file_digest(goal_path)}
        self.operation = OperationInput('run', 'task', 'coder', 'run:coder:1', str(self.root),
            options={'workspace': 'workspaces/development/g0/task', 'deadline_epoch': time.time() + 120},
            artifact_refs={'coder_goal': self.goal_ref})
        self.directory = self.root / 'artifacts/executions' / self.operation.command_id
        atomic_json(self.directory / 'input.json', self.operation.to_dict())
        record = {key: getattr(self.operation, key) for key in ('run_id', 'task_id', 'stage_id', 'command_id')}
        record.update(workspace=self.operation.options['workspace'], goal_ref=self.goal_ref)
        atomic_json(self.directory / 'coder-setup.json', {'record': record, 'sha256': sha256(
            json.dumps(record, sort_keys=True, separators=(',', ':')).encode()).hexdigest()})
        self.native_directory = self.root / 'artifacts/native-goals' / sha256(self.operation.command_id.encode()).hexdigest()[:24]
        self.native_directory.mkdir(parents=True)
        (self.native_directory / 'claim').touch()
        self.native = {'command_id': self.operation.command_id, 'worktree': str(self.workspace),
                       'objective': 'bounded objective', 'thread_id': 'existing-native-thread',
                       'prompt_sha256': 'a' * 64, 'deadline_epoch': time.time() + 60}
        atomic_json(self.native_directory / 'state.json', self.native)
        self.effect = SimpleNamespace(request=self.expected())
        self.context_operations = []
        @contextmanager
        def context(operation):
            self.context_operations.append(operation)
            yield {'invocation_id': 'recovery-invocation'}
        for target, kwargs in (
            ('modport.kernel_runtime.operation_context', {'side_effect': context}),
            ('modport.kernel_runtime.record_event', {}),
            ('modport.kernel_runtime.repository_facts', {'return_value': {'after': True}}),
        ):
            mocked = patch(target, **kwargs)
            mocked.start()
            self.addCleanup(mocked.stop)

    def expected(self):
        return {'input_sha256': digest(self.operation.to_dict()), 'run_dir': str(self.root), 'stage': self.operation.stage_id}

    def recover(self):
        return resume_interrupted_goal(self.root, {'payload': self.operation.to_dict()}, self.effect)

    def result(self, operation):
        output = self.root / 'artifacts/output.txt'
        output.write_text('validated candidate')
        return OperationResult('completed', operation.run_id, operation.task_id, operation.stage_id,
            operation.command_id, outputs={'artifact_refs': {'candidate': {'path': 'artifacts/output.txt'}}})

    def test_resumes_original_effect_and_seals_output_without_rewriting_input(self):
        before = (self.directory / 'input.json').read_bytes()
        with patch('modport.development.CoderHandler.__call__', side_effect=self.result) as invoke:
            response = self.recover()
        delegated = invoke.call_args.args[0]
        self.assertTrue(delegated.options['native_goal_resume'])
        self.assertEqual(delegated.command_id, self.operation.command_id)
        self.assertEqual(delegated.attempt, self.operation.attempt)
        self.assertEqual(delegated.options['deadline_epoch'], self.native['deadline_epoch'])
        self.assertEqual(before, (self.directory / 'input.json').read_bytes())
        self.assertEqual(self.context_operations, [self.operation])
        receipt = read_json(self.directory / 'receipt.json')
        self.assertEqual(receipt['effect_request'], self.expected())
        self.assertEqual(receipt['response'], response)
        self.assertEqual(receipt['after'], {'after': True})
        self.assertEqual(response['outputs']['artifact_refs']['candidate']['metadata']['execution_id'], self.operation.command_id)

    def test_complete_receipt_takes_precedence_even_after_deadline(self):
        response = self.result(self.operation).to_dict()
        atomic_json(self.directory / 'receipt.json', {'execution_id': self.operation.command_id,
            'effect_request': self.expected(), 'response': response, 'after': {}})
        (self.native_directory / 'state.json').unlink()
        with patch('modport.development.CoderHandler.__call__') as invoke:
            self.assertEqual(self.recover(), response)
        invoke.assert_not_called()

    def test_wrong_effect_identity_never_runs_coder(self):
        self.effect.request = {**self.expected(), 'input_sha256': 'wrong'}
        with patch('modport.development.CoderHandler.__call__') as invoke:
            with self.assertRaisesRegex(ValueError, 'identity'):
                self.recover()
        invoke.assert_not_called()

    def test_wrong_stage_never_runs_coder(self):
        self.operation = replace(self.operation, stage_id='gap_research')
        self.effect.request = self.expected()
        with patch('modport.development.CoderHandler.__call__') as invoke:
            with self.assertRaisesRegex(ValueError, 'native coder'):
                self.recover()
        invoke.assert_not_called()

    def test_missing_or_changed_thread_identity_never_runs_coder(self):
        self.native['thread_id'] = None
        atomic_json(self.native_directory / 'state.json', self.native)
        with patch('modport.development.CoderHandler.__call__') as invoke:
            with self.assertRaisesRegex(ValueError, 'thread identity'):
                self.recover()
        invoke.assert_not_called()

    def test_v17_recovery_uses_the_normalized_goal_stored_at_execution(self):
        """Free-form v17 context is not the native goal session objective."""
        goal_path = self.root / 'artifacts/goal.json'
        atomic_json(goal_path, {'objective': 'long free-form coder context'})
        self.goal_ref = {'path': 'artifacts/goal.json', 'sha256': file_digest(goal_path)}
        self.operation = replace(self.operation, artifact_refs={'coder_goal': self.goal_ref})
        atomic_json(self.directory / 'input.json', self.operation.to_dict())
        record = {key: getattr(self.operation, key)
                  for key in ('run_id', 'task_id', 'stage_id', 'command_id')}
        record.update(workspace=self.operation.options['workspace'], goal_ref=self.goal_ref,
                      goal={'objective': 'bounded objective'})
        atomic_json(self.directory / 'coder-setup.json', {
            'record': record,
            'sha256': sha256(json.dumps(record, sort_keys=True, separators=(',', ':')).encode()).hexdigest(),
        })
        self.native['objective'] = 'bounded objective'
        atomic_json(self.native_directory / 'state.json', self.native)
        self.effect.request = self.expected()

        with patch('modport.development.CoderHandler.__call__', side_effect=self.result) as invoke:
            self.recover()

        invoke.assert_called_once()

    def test_v24_recovery_matches_the_advisory_native_objective(self):
        from modport.development import coder_runtime_goal

        self.operation = replace(self.operation, options={**self.operation.options,
                                                           'workflow_version': 24})
        atomic_json(self.directory / 'input.json', self.operation.to_dict())
        record = {key: getattr(self.operation, key)
                  for key in ('run_id', 'task_id', 'stage_id', 'command_id')}
        record.update(workspace=self.operation.options['workspace'], goal_ref=self.goal_ref,
                      goal={'objective': 'bounded objective'})
        atomic_json(self.directory / 'coder-setup.json', {
            'record': record,
            'sha256': sha256(json.dumps(record, sort_keys=True, separators=(',', ':')).encode()).hexdigest(),
        })
        self.native['objective'] = coder_runtime_goal(record['goal'], advisory=True)['objective']
        atomic_json(self.native_directory / 'state.json', self.native)
        self.effect.request = self.expected()

        with patch('modport.development.CoderHandler.__call__', side_effect=self.result) as invoke:
            self.recover()
        invoke.assert_called_once()

        self.native['objective'] = 'bounded objective'
        atomic_json(self.native_directory / 'state.json', self.native)
        (self.directory / 'receipt.json').unlink()
        with patch('modport.development.CoderHandler.__call__') as invoke:
            with self.assertRaisesRegex(ValueError, 'thread identity'):
                self.recover()
        invoke.assert_not_called()

    def test_expired_original_deadline_does_not_grant_another_assignment(self):
        self.native['deadline_epoch'] = time.time() - 1
        atomic_json(self.native_directory / 'state.json', self.native)
        with patch('modport.development.CoderHandler.__call__') as invoke:
            with self.assertRaisesRegex(ValueError, 'deadline.*exhausted'):
                self.recover()
        invoke.assert_not_called()
        self.assertFalse((self.directory / 'receipt.json').exists())

    def test_modified_frozen_input_never_runs_coder(self):
        atomic_json(self.directory / 'input.json', {})
        with patch('modport.development.CoderHandler.__call__') as invoke:
            with self.assertRaisesRegex(ValueError, 'frozen operation'):
                self.recover()
        invoke.assert_not_called()

    def test_invalid_result_cannot_write_receipt(self):
        wrong = replace(self.result(self.operation), command_id='different')
        with patch('modport.development.CoderHandler.__call__', return_value=wrong):
            with self.assertRaisesRegex(ValueError, 'command_id mismatch'):
                self.recover()
        self.assertFalse((self.directory / 'receipt.json').exists())


class NativeGoalRecoveryCLITests(unittest.TestCase):
    def test_existing_recover_requires_explicit_opt_in(self):
        from modport.cli import parser
        args = ['recover', '--run-dir', '/tmp/run', '--run-id', 'run']
        self.assertFalse(parser().parse_args(args).resume_native_goals)
        self.assertTrue(parser().parse_args([*args, '--resume-native-goals']).resume_native_goals)

    def test_conflicting_recovery_actions_are_rejected(self):
        from contextlib import redirect_stderr
        from io import StringIO
        from modport.cli import parser
        with redirect_stderr(StringIO()), self.assertRaises(SystemExit):
            parser().parse_args(['recover', '--run-dir', '/tmp/run', '--run-id', 'run',
                                 '--resume-native-goals', '--cancel-interrupted-research'])


if __name__ == '__main__':
    unittest.main()
