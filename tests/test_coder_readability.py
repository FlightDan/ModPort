"""Current authoring entrypoints retain readable-code guidance and real failures."""

from dataclasses import replace
import json
from pathlib import Path
import shutil
import subprocess
from tempfile import TemporaryDirectory
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from dispatcher_sdk.execution_kernel import BudgetEnvelope, ClockCheckpoint, DeadlineConstraint

from modport.coder_readability import (
    AUTHORING_STAGES, CORE_REQUIREMENTS, SKILL_PATH, append_readability, requirements,
)
from modport.contracts import OperationInput
from modport.development import CoderHandler, _artifact, validate_plan
from modport.execution_budget import execution_budget
from modport.handlers import _result
from modport.prompt_compressor import (
    PromptCompressor, build_prompt_regions, prompt_regions,
)
from modport.prompts import build_prompt
from modport.report_dialogue import phase_command, prepare_dialogue
from modport.rework_coder import run_coder_rework
from modport.workflow import WORKFLOW_VERSION


class SummaryBackend:
    name = 'readability-test'
    tool_free = True

    def summarize(self, request, **kwargs):
        return json.dumps({'objective': 'Read previous implementation observations',
                           'important_details': [], 'completed': [], 'active': [],
                           'blocked': [], 'next_steps': [], 'evidence_refs': []})


class BudgetContext:
    def __init__(self, command):
        self.command = command
        self.envelope = BudgetEnvelope((DeadlineConstraint(
            'readability-rework', 'execution', time.time() + 300, 0),), self.sample())

    @staticmethod
    def sample():
        return ClockCheckpoint(time.time(), time.monotonic(), 'readability-test', 'boot')

    @property
    def budget(self):
        self.envelope = self.envelope.recheckpoint(sample=self.sample())
        return self.envelope.view(sample=self.envelope.checkpoint)


def git(root, *args):
    return subprocess.check_output(
        ['git', '-c', 'user.name=Test', '-c', 'user.email=test@example.invalid', *args],
        cwd=root, stderr=subprocess.DEVNULL).decode().strip()


class CoderReadabilityTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.assertGreaterEqual(WORKFLOW_VERSION, 37)

    def command(self, stage, **payload):
        return OperationInput(
            'run', 'task-' + stage, stage, 'execution-' + stage, str(self.root),
            payload=payload, options={'workflow_version': WORKFLOW_VERSION})

    def test_all_target_authoring_envelopes_and_archives_include_core(self):
        for stage in AUTHORING_STAGES:
            with self.subTest(stage=stage):
                command = self.command(stage)
                prompt = build_prompt('Perform this assigned target operation.', command,
                                      self.root, {}, {})
                _, _, protected = prompt_regions(prompt)
                packet = json.loads((self.root / 'artifacts/executions' /
                                     command.command_id / 'task-instructions.json').read_text())
                self.assertIn(CORE_REQUIREMENTS, protected)
                self.assertIn(CORE_REQUIREMENTS, packet['protected_context'])
                self.assertIn(str(self.root / SKILL_PATH), protected)
                self.assertIn('modport_sandbox_read_run_artifact', protected)

    def test_planning_and_execution_dialogue_retain_core_and_permissions(self):
        command = self.command('coder')
        command = replace(command, options={**command.options,
            'agent_dialogue_policy': {'version': 1, 'turns': ['plan', 'execute']}})
        dialogue = prepare_dialogue(command, self.root, 'Implement the assigned adapter.')
        for phase, key in [('plan', 'planning_task'), ('execute', 'execution_task')]:
            prompt = build_prompt(dialogue[key], phase_command(command, phase),
                                  self.root, {}, {})
            self.assertIn(CORE_REQUIREMENTS, prompt_regions(prompt)[2])
            packet = json.loads((self.root / 'artifacts/executions' / command.command_id /
                                 ('task-instructions.' + phase + '.json')).read_text())
            self.assertIn(CORE_REQUIREMENTS, packet['protected_context'])
        self.assertIn('do not', dialogue['planning_task'].lower())

    def test_compression_keeps_core_and_skill_reference_verbatim(self):
        command = self.command('coder')
        command = replace(command, options={**command.options, 'model': 'test-model'})
        prompt = build_prompt('Implement the locked target adapter.', command, self.root, {}, {})
        current, _, protected = prompt_regions(prompt)
        source = build_prompt_regions(current, 'Previous observation of target adapter.\n' * 800,
                                      protected)
        catalog = {'models': [
            {'slug': 'test-model', 'context_window': 12000},
            {'slug': 'gpt-6-luna', 'context_window': 12000, 'variants': {'max': {}}},
        ]}
        result = PromptCompressor(catalog=catalog, summary_backend=SummaryBackend()).compress(
            source, model='test-model', command=command, root=self.root, worktree=self.root)
        self.assertTrue(result.metadata['compressed'])
        self.assertIn(CORE_REQUIREMENTS, result.text)
        self.assertIn(str(self.root / SKILL_PATH), result.text)
        self.assertTrue(result.text.endswith(protected))

    def test_native_turn_reinjection_is_idempotent_and_role_specific(self):
        command = self.command('coder')
        prompt = append_readability('Continue from the interrupted tool result.', command, self.root)
        self.assertIn(CORE_REQUIREMENTS, prompt)
        self.assertEqual(prompt, append_readability(prompt, command, self.root))
        self.assertEqual('', requirements(self.command('code_review'), self.root))
        self.assertEqual('', requirements(self.command('agent_rework'), self.root))
        self.assertEqual('', requirements(replace(command, options={'workflow_version': 35}),
                                          self.root))

    def test_bundled_skill_is_readable_through_registered_artifact_tool(self):
        from modport import coder_readability
        from modport.opencode_shell_mcp import _read_run_artifact
        source = Path(coder_readability.__file__).parent / 'vendor_skills/code-simplifier/SKILL.md'
        target = self.root / SKILL_PATH
        target.parent.mkdir(parents=True)
        shutil.copyfile(source, target)
        result = _read_run_artifact({'root': str(self.root), 'deadline_epoch': time.time() + 30},
                                    str(target))
        self.assertIn('exact locked Java', result['content_utf8'])
        self.assertIn('Never delete, skip, rename, weaken or replace an assertion', result['content_utf8'])
        self.assertNotIn('React', result['content_utf8'])

    def setup_coder(self):
        worktree = self.root / 'worktree'
        worktree.mkdir()
        git(worktree, 'init')
        (worktree / 'Adapter.java').write_text('class Adapter {}\n')
        git(worktree, 'add', '.')
        git(worktree, 'commit', '-m', 'initial')
        base = git(worktree, 'rev-parse', 'HEAD')
        task = {'id': 'adapter', 'objective': 'Preserve target adapter behavior',
                'owned_paths': ['Adapter.java'], 'dependencies': [],
                'acceptance': ['Existing behavior remains intact'], 'complexity': 'simple'}
        plan = validate_plan({'base_commit': base, 'shared_paths': [], 'tasks': [task]},
                             workflow_version=WORKFLOW_VERSION)
        seed = self.command('coder')
        plan_ref = _artifact(seed, 'development-plan.json', json.dumps(plan).encode(),
                             {'development_base': base})
        original = replace(seed, payload={
            'development_base': base, 'development_generation': 1,
            'development_task': plan['tasks'][0], 'planning_context': {},
            'dependency_patches': [],
        }, options={**seed.options, 'workspace': 'workspaces/development/g1/adapter'},
            artifact_refs={'development_plan': plan_ref})
        return worktree, original

    def capture_coder_failure(self, handler, command):
        prompt = build_prompt(handler.prompt, command, self.root, {}, {})
        self.observed.append((command, prompt))
        return _result(command, 'failed', error_code='agent_failed',
                       detail='target adapter compilation failed')

    def test_fresh_first_coder_receives_guidance_before_returned_failure(self):
        _, command = self.setup_coder()
        self.observed = []
        with patch('modport.handlers.CodexStageHandler.__call__',
                   lambda handler, child: self.capture_coder_failure(handler, child)):
            result = CoderHandler()(command)
        self.assertEqual('failed', result.status)
        self.assertEqual(1, len(self.observed), result.detail)
        child, prompt = self.observed[0]
        self.assertEqual('coder', child.stage_id)
        self.assertIn(CORE_REQUIREMENTS, prompt_regions(prompt)[2])
        self.assertIn('target adapter compilation failed', result.detail)

    def test_actual_rework_delegation_carries_guidance_and_returned_failure(self):
        worktree, original = self.setup_coder()
        child = self.command('agent_rework', rework_generation=2,
                             reviewer_execution_id='review-execution',
                             reviewer_report='Adapter needs a readable repair.',
                             reviewer_report_paths=[], review_tool_context={},
                             rework_context_refs={})
        sdk_context = BudgetContext(SimpleNamespace(
            execution_id=child.command_id, timeout_seconds=300, payload=child.to_dict()))
        self.observed = []
        with execution_budget(sdk_context), patch(
                'modport.handlers.CodexStageHandler.__call__',
                lambda handler, delegated: self.capture_coder_failure(handler, delegated)):
            result = run_coder_rework(child, original, worktree,
                                     'Repair the adapter while preserving assertion IDs.')
        self.assertEqual('failed', result.status)
        self.assertEqual(1, len(self.observed), result.detail)
        delegated, prompt = self.observed[0]
        self.assertEqual('agent_rework', delegated.stage_id)
        self.assertIn('development_task', delegated.payload)
        self.assertIn(CORE_REQUIREMENTS, prompt_regions(prompt)[2])
        packet = json.loads((self.root / 'artifacts/executions' / delegated.command_id /
                             'task-instructions.json').read_text())
        self.assertIn('Reviewer-requested rework', packet['task'])
        self.assertIn('preserving assertion IDs', packet['task'])
        self.assertEqual('agent_failed', result.error_code)
        self.assertIn('target adapter compilation failed',
                      '\n'.join(result.outputs['business_diagnostics']))


class CurrentUserScopeTests(unittest.TestCase):
    setUp = CoderReadabilityTests.setUp
    command = CoderReadabilityTests.command

    def test_current_user_scope_survives_protected_prompt_and_archive(self):
        command = self.command('migration_plan', request={'requirements': 'Include dependency port; exclude old save upgrades.'})
        prompt = build_prompt('Plan migration.', command, self.root, {}, {})
        self.assertIn('Include dependency port; exclude old save upgrades.', prompt_regions(prompt)[2])
        packet = next((self.root / 'artifacts/executions').rglob('task-instructions.json'))
        self.assertIn('Include dependency port; exclude old save upgrades.', json.loads(packet.read_text())['protected_context'])


    def test_user_scope_round_trips_and_cli_freezes_it(self):
        from modport.models import MigrationRequest
        from modport.cli import parser, _request
        args = parser().parse_args(['compile', '--mod-id', 'hyperbox',
            '--source-repository', 'https://example.invalid/hyperbox.git',
            '--source-revision', 'main', '--source-minecraft', '1.21', '--target-minecraft', '26.1',
            '--requirements', 'Include dependency port; exclude old save upgrades.'])
        request = _request(args)
        self.assertEqual(request.requirements, MigrationRequest.from_mapping(request.to_dict()).requirements)


if __name__ == '__main__':
    unittest.main()
