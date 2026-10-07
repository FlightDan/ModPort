"""Handler boundary regressions; no external agent or migration is launched."""
from dataclasses import replace
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

from tests import test_handlers
from modport.goal_runtime import GoalRunResult
from modport.handlers import CodexStageHandler
from modport.opencode_runtime import OpenCodeCleanupError
from modport.prompt_compressor import PromptCompressionError, PromptCompressor


class GoalHandlerTests(unittest.TestCase):
    def setUp(self):
        compressor = PromptCompressor(catalog={"models": [
            {"slug": "gpt-6-luna", "context_window": 1_000_000},
            {"slug": "gpt-6-sol", "context_window": 1_000_000},
            {"slug": "gpt-5.6-luna", "context_window": 1_000_000},
        ]})
        patcher = patch('modport.handlers.PromptCompressor.from_environment',
                        return_value=compressor)
        patcher.start()
        self.addCleanup(patcher.stop)

    def setup_command(self, root):
        (root / 'worktree/.modport').mkdir(parents=True)
        return test_handlers.HandlerTests._command(root, 'implementation')

    def test_native_goal_receives_full_prompt_and_preserves_evidence(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            command = self.setup_command(root)
            validator = Mock(return_value={'accepted': True, 'failures': [], 'evidence': {}})
            state = root / 'artifacts/native-goals/test/state.json'
            state.parent.mkdir(parents=True)
            state.write_text('{"host_accepted":true}')
            metadata = {'host_accepted': True, 'native_goal_status': 'complete',
                        'tokens_used': 25, 'state_path': state.relative_to(root).as_posix()}
            stdout = json.dumps({'type': 'item.completed', 'item': {'type': 'agent_message', 'text': 'checked'}})
            with patch('modport.goal_runtime.run_goal', return_value=GoalRunResult(stdout, 0, metadata)) as run, \
                 patch('modport.opencode_agent.run_agent') as ordinary:
                result = CodexStageHandler('Implement the assigned task with all context.',
                    read_only=True, native_goal={'objective': 'Do the assigned work'},
                    goal_validator=validator)(command)
            self.assertEqual('completed', result.status, result.detail)
            ordinary.assert_not_called()
            self.assertIs(run.call_args.kwargs['validate'], validator)
            self.assertTrue(run.call_args.kwargs['command'].options['goal_read_only'])
            self.assertIn('Work autonomously', run.call_args.kwargs['prompt'])
            self.assertIn('Implement the assigned task', run.call_args.kwargs['prompt'])
            self.assertEqual(metadata, result.outputs['native_goal'])
            self.assertIn('native_goal_state', result.outputs['artifact_refs'])
            self.assertIn('agent_prompt', result.outputs['artifact_refs'])
            self.assertEqual('checked', (root / result.outputs['last_message']).read_text().strip())
            self.assertNotIn('transport_args', run.call_args.kwargs)

    def test_native_goal_receives_host_generated_rework_transport_config(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            target = {'target_agent': 'coder-task', 'execution_id': 'coder-execution',
                      'stage': 'coder', 'description': 'Repair adapter'}
            command = self.setup_command(root)
            command = replace(command, stage_id='code_review',
                payload={'review_rework_targets': [target]},
                options={**command.options, 'workflow_version': 17})
            metadata = {'host_accepted': True, 'native_goal_status': 'complete'}
            with patch('modport.goal_runtime.run_goal',
                       return_value=GoalRunResult('', 0, metadata)) as run:
                result = CodexStageHandler('Review the candidate.', read_only=True,
                    native_goal={'objective': 'Review candidate'},
                    goal_validator=lambda: {'accepted': True})(command)
            self.assertEqual('completed', result.status, result.detail)
            config = run.call_args.kwargs['transport_args']
            self.assertIsInstance(config, dict)
            self.assertIn('modport_rework', config)
            mcp_args = config['modport_rework']['command']
            self.assertEqual(['/usr/bin/env', '-i'], mcp_args[:2])
            self.assertEqual(str(root / 'artifacts/rework-tools/command-1/session.json'),
                             mcp_args[-1])
            self.assertIn('modport.rework_mcp', ' '.join(mcp_args))

    def test_native_goal_host_rejection_is_not_completed(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            command = self.setup_command(root)
            metadata = {'host_accepted': False, 'native_goal_status': 'blocked'}
            # A successful transport exit cannot substitute for host acceptance.
            with patch('modport.goal_runtime.run_goal', return_value=GoalRunResult('', 0, metadata)):
                result = CodexStageHandler('task', native_goal={'objective': 'task'},
                    goal_validator=lambda: {})(command)
            self.assertEqual('failed', result.status)
            self.assertEqual(metadata, result.outputs['native_goal'])

    def test_prelaunch_failures_preserve_existing_required_output(self):
        for failure in ('compression', 'budget', 'goal'):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as raw:
                root = Path(raw)
                command = self.setup_command(root)
                target = root / 'worktree/.modport/functional-contract.json'
                target.write_bytes(b'original contract\n')
                handler = CodexStageHandler('task', required_paths=('.modport/functional-contract.json',))
                if failure == 'budget':
                    command = replace(command, options={**command.options, 'deadline_epoch': 1})
                elif failure == 'goal':
                    handler.native_goal = {'objective': 'task'}
                with patch('modport.opencode_agent.run_agent') as ordinary, \
                     patch('modport.goal_runtime.run_goal') as native:
                    if failure == 'compression':
                        compressor = Mock()
                        compressor.compress.side_effect = PromptCompressionError('cannot fit protected context')
                        with patch('modport.handlers.PromptCompressor.from_environment', return_value=compressor):
                            result = handler(command)
                    else:
                        result = handler(command)
                self.assertNotEqual('completed', result.status)
                self.assertEqual(b'original contract\n', target.read_bytes())
                ordinary.assert_not_called()
                native.assert_not_called()

    def test_existing_output_is_backed_up_and_not_accepted_as_fresh(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            command = self.setup_command(root)
            target = root / 'worktree/.modport/functional-contract.json'
            target.write_text('original')
            with patch('modport.opencode_agent.run_agent', return_value=subprocess.CompletedProcess([], 0, '')):
                result = CodexStageHandler('task', required_paths=('.modport/functional-contract.json',))(command)
            self.assertEqual('agent_output_missing', result.error_code)
            backup = root / 'artifacts/executions/command-1/previous-outputs/.modport/functional-contract.json'
            self.assertEqual('original', backup.read_text())
            self.assertEqual('original', target.read_text())

    def test_agent_startup_cleanup_failure_retains_only_safe_process_identity(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            command = self.setup_command(root)
            failure = OpenCodeCleanupError({
                'cleanup_confirmed': False, 'target_pid': 900004,
                'target_birth': 'boot:123', 'detail': 'private provider message'})
            with patch('modport.opencode_agent.run_agent', side_effect=failure):
                result = CodexStageHandler('task')(command)
            self.assertEqual('failed', result.status)
            self.assertEqual('opencode_cleanup_unconfirmed', result.error_code)
            ref = result.outputs['artifact_refs']['opencode_cleanup']
            record = json.loads((root / ref['path']).read_text())
            self.assertFalse(record['cleanup_confirmed'])
            self.assertEqual(900004, record['target_pid'])
            self.assertNotIn('private provider message', json.dumps(record))

    def test_failed_launch_or_candidate_restores_old_output_and_archives_rejection(self):
        for launch_error in (True, False):
            with self.subTest(launch_error=launch_error), tempfile.TemporaryDirectory() as raw:
                root = Path(raw)
                command = self.setup_command(root)
                target = root / 'worktree/.modport/functional-contract.json'
                target.write_text('original')
                def execute(*args, **kwargs):
                    self.assertFalse(target.exists())
                    if launch_error:
                        raise OSError(2, 'missing executable')
                    target.write_text('rejected candidate')
                    return subprocess.CompletedProcess([], 1, '')
                with patch('modport.opencode_agent.run_agent', side_effect=execute):
                    result = CodexStageHandler('task', required_paths=('.modport/functional-contract.json',))(command)
                self.assertEqual('failed', result.status)
                self.assertEqual('original', target.read_text())
                rejected = root / 'artifacts/executions/command-1/rejected-outputs/.modport/functional-contract.json'
                if not launch_error:
                    self.assertEqual('rejected candidate', rejected.read_text())
