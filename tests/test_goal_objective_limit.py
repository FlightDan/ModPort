"""Goal API size boundary and full-context preservation regressions."""
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from modport.goal_runtime import run_goal
from test_goal_runtime import FakeTransport, done


class LimitedTransport(FakeTransport):
    def request(self, method, params):
        if method == 'thread/goal/set':
            if len(params['objective'].encode()) > 4000:
                raise RuntimeError('goal objective must be at most 4000 characters')
            self.objective = params['objective']
        result = super().request(method, params)
        if method in {'thread/goal/set', 'thread/goal/get'}:
            result['goal']['objective'] = self.objective
        return result


class GoalObjectiveLimitTests(unittest.TestCase):
    def invoke(self, objective, version=20):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        command = SimpleNamespace(command_id='limit', options={'workflow_version': version})
        LimitedTransport.instances = []
        LimitedTransport.events = [done('turn')]
        LimitedTransport.statuses = ['complete']
        with patch('modport.goal_runtime._Transport', LimitedTransport):
            result = run_goal(command=command, root=root, worktree=root,
                              prompt='Assigned context', objective=objective,
                              validate=lambda: {'accepted': True}, timeout=30)
        return result, LimitedTransport.instances[0], root, command

    def test_long_unicode_objective_is_preserved_and_native_goal_is_bounded(self):
        objective = '完整任务约束。' * 900
        result, transport, _, _ = self.invoke(objective)
        self.assertEqual(0, result.returncode)
        self.assertLessEqual(len(transport.objective.encode()), 4000)
        turns = [row[1] for row in transport.calls
                 if isinstance(row, tuple) and row[0] == 'turn/start']
        self.assertEqual(1, len(turns))
        self.assertIn(objective, turns[0]['input'][0]['text'])
        self.assertEqual(objective, result.metadata['objective'])
        self.assertEqual(transport.objective, result.metadata['native_objective'])
        self.assertIn('execution_prompt_sha256', result.metadata)

    def test_exact_ascii_limit_retains_existing_identity_and_prompt(self):
        result, transport, _, _ = self.invoke('a' * 4000)
        self.assertEqual(0, result.returncode)
        self.assertEqual('a' * 4000, transport.objective)
        self.assertNotIn('native_objective', result.metadata)

    def test_legacy_long_goal_is_not_silently_rewritten(self):
        result, _, _, _ = self.invoke('a' * 4001, version=19)
        self.assertNotEqual(0, result.returncode)
        self.assertIn('4000', result.metadata['error'])

    def test_missing_new_identity_cannot_resume_an_older_long_goal(self):
        objective = 'a' * 4001
        result, _, root, command = self.invoke(objective)
        state = next((root / 'artifacts/native-goals').glob('*/state.json'))
        data = json.loads(state.read_text())
        data.pop('native_objective')
        state.write_text(json.dumps(data))
        command.options['native_goal_resume'] = True
        with patch('modport.goal_runtime._Transport') as transport:
            resumed = run_goal(command=command, root=root, worktree=root,
                               prompt='Assigned context', objective=objective,
                               validate=lambda: {'accepted': True}, timeout=30)
        self.assertEqual('native_goal_resume_identity_mismatch', resumed.metadata['error'])
        transport.assert_not_called()


if __name__ == '__main__':
    unittest.main()
