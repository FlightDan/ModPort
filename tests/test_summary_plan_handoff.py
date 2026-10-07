"""The first v25 OpenCode turn receives the host's bounded context reference."""
from dataclasses import replace
from hashlib import sha256
import json
from pathlib import Path
import subprocess
import tempfile
import time
import unittest
from unittest.mock import patch

from modport.handlers import CodexStageHandler
from modport.opencode_shell_mcp import _read_run_artifact
from modport.prompt_compressor import CompressedPrompt, prompt_regions
from modport.report_dialogue import dialogue_artifacts
import test_handlers


class SummaryPlanHandoffTests(unittest.TestCase):
    def test_context_preflight_is_sealed_for_dialogue_consumer(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            dialogue_dir = root / 'artifacts' / 'executions' / 'task' / 'dialogue'
            dialogue_dir.mkdir(parents=True)
            preflight = dialogue_dir / 'session-context-preflight.json'
            preflight.write_text('{"status":"passed","required_tokens":42}\n')
            refs = dialogue_artifacts(root, {
                'directory': dialogue_dir,
                'plan_path': dialogue_dir / 'plan.md',
                'schema_path': None,
            })
            self.assertEqual(sha256(preflight.read_bytes()).hexdigest(),
                             refs['agent_session_context_preflight']['sha256'])

    def test_frozen_v24_plan_keeps_its_original_context_contract(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'worktree').mkdir()
            original = test_handlers.HandlerTests._command(root, 'implementation')
            command = replace(original, options={**original.options,
                'workflow_version': 24,
                'agent_dialogue_policy': {'version': 1, 'turns': ['plan', 'execute']}})
            captured = {}

            class Compressor:
                def compress(self, text, **kwargs):
                    return CompressedPrompt(text, {'compressed': True,
                        'model': kwargs['model'],
                        'original_sha256': sha256(text.encode()).hexdigest()})

            def agent(**kwargs):
                captured.update(kwargs)
                kwargs['plan_path'].write_text('# Plan\n', encoding='utf-8')
                result = subprocess.CompletedProcess(['opencode'], 0,
                    json.dumps({'type': 'item.completed', 'item': {
                        'type': 'agent_message', 'text': 'executed'}}) + '\n')
                result.dialogue_metadata = {'thread_id': 'synthetic-thread',
                    'dialogue_phase': 'execute', 'planning_turns': 1,
                    'turns': 2, 'status': 'completed', 'deadline_epoch': time.time() + 60}
                return result

            with (patch('modport.handlers.PromptCompressor.from_environment', return_value=Compressor()),
                  patch('modport.rework_tools.prepare_session', return_value=None),
                  patch('modport.rework_tools.opencode_tool_config', return_value={}),
                  patch('modport.opencode_agent.run_agent', side_effect=agent)):
                outcome = CodexStageHandler('Implement the assigned migration task.')(command)

            self.assertEqual('completed', outcome.status, outcome.detail)
            self.assertNotIn('prompt_compression_plan_history', outcome.outputs['artifact_refs'])
            plan_context = json.loads(captured['planning_prompt'].partition('Host context: ')[2])
            task = json.loads(Path(plan_context['instructions']['path']).read_text())['task']
            self.assertNotIn('Host-prepared compressed historical context', task)

    def test_compressed_context_reaches_first_plan_turn_as_readable_ref(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'worktree').mkdir()
            original = test_handlers.HandlerTests._command(root, 'implementation')
            command = replace(original, options={**original.options,
                'workflow_version': 25,
                'agent_dialogue_policy': {'version': 1, 'turns': ['plan', 'execute']}})
            bounded = 'host-bounded-summary: planning nonce 4c2fbd\n' + '中' * 25000
            captured = {}

            class Compressor:
                def compress(self, text, **kwargs):
                    current, _, protected = prompt_regions(text)
                    result = current + bounded + protected
                    captured['expected_execution_prompt'] = result
                    return CompressedPrompt(result, {'compressed': True,
                        'model': kwargs['model'],
                        'context_window': 200000,
                        'input_token_budget': 140000,
                        'output_token_reserve': 40000,
                        'tool_token_reserve': 20000,
                        'original_sha256': sha256(text.encode()).hexdigest(),
                        'original_bytes': len(text.encode()),
                        'final_sha256': sha256(result.encode()).hexdigest(),
                        'final_bytes': len(result.encode())})

            def agent(**kwargs):
                captured.update(kwargs)
                kwargs['plan_path'].write_text('# Plan\nRead bounded context.\n', encoding='utf-8')
                result = subprocess.CompletedProcess(['opencode'], 0,
                    json.dumps({'type': 'item.completed', 'item': {
                        'type': 'agent_message', 'text': 'executed'}}) + '\n')
                result.dialogue_metadata = {'thread_id': 'synthetic-thread',
                    'dialogue_phase': 'execute', 'planning_turns': 1,
                    'turns': 2, 'status': 'completed', 'deadline_epoch': time.time() + 60}
                return result

            with (patch('modport.handlers.PromptCompressor.from_environment', return_value=Compressor()),
                  patch('modport.rework_tools.prepare_session', return_value=None),
                  patch('modport.rework_tools.opencode_tool_config', return_value={}),
                  patch('modport.opencode_agent.run_agent', side_effect=agent)):
                outcome = CodexStageHandler('Implement the assigned migration task.')(command)

            self.assertEqual('completed', outcome.status, outcome.detail)
            self.assertEqual(140000, captured['session_context_budget']['input_token_budget'])
            ref = outcome.outputs['artifact_refs']['prompt_compression_plan_history']
            self.assertEqual(sha256((root / ref['path']).read_bytes()).hexdigest(), ref['sha256'])
            execution_ref = outcome.outputs['artifact_refs']['prompt_compression_output']
            self.assertNotEqual(ref['sha256'], execution_ref['sha256'])
            plan = captured['planning_prompt']
            self.assertEqual(plan, (root / 'artifacts/executions/command-1/dialogue/plan-prompt.txt').read_text())
            plan_context = json.loads(plan.partition('Host context: ')[2])
            instructions = Path(plan_context['instructions']['path'])
            self.assertEqual(sha256(instructions.read_bytes()).hexdigest(),
                             plan_context['instructions']['sha256'])
            plan_task = json.loads(instructions.read_text())['task']
            self.assertIn(ref['path'], plan_task)
            self.assertIn(ref['sha256'], plan_task)
            self.assertNotIn(execution_ref['path'], plan_task)
            self.assertIn('modport_sandbox_read_run_artifact', plan_task)
            self.assertIn('next_offset', plan_task)
            self.assertIn('total_bytes', plan_task)
            self.assertIn('SHA-256', plan_task)
            self.assertIn('expected_sha256', plan_task)
            self.assertIn('plan-only', plan_task)
            chunks = []
            offset = 0
            while True:
                tool_result = _read_run_artifact({'root': str(root.resolve()),
                    'deadline_epoch': time.time() + 60}, ref['path'], offset=offset,
                    expected_sha256=ref['sha256'])
                self.assertEqual(ref['sha256'], tool_result['verified_sha256'])
                chunks.append(tool_result['content_utf8'])
                offset = tool_result['next_offset']
                if offset == tool_result['total_bytes']:
                    break
            reconstructed = ''.join(chunks)
            self.assertEqual(bounded, reconstructed)
            self.assertEqual(ref['sha256'], sha256(reconstructed.encode()).hexdigest())
            self.assertEqual(captured['expected_execution_prompt'], captured['prompt'])

            with (patch('modport.handlers.PromptCompressor.from_environment',
                        side_effect=AssertionError('cached compression must be reused')),
                  patch('modport.rework_tools.prepare_session', return_value=None),
                  patch('modport.rework_tools.opencode_tool_config', return_value={}),
                  patch('modport.opencode_agent.run_agent', side_effect=agent)):
                replay = CodexStageHandler('Implement the assigned migration task.')(command)
            self.assertEqual('completed', replay.status, replay.detail)
            self.assertEqual(ref, replay.outputs['artifact_refs']['prompt_compression_plan_history'])
            self.assertEqual(captured['expected_execution_prompt'], captured['prompt'])


if __name__ == '__main__':
    unittest.main()
