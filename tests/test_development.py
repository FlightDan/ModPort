"""Behavioral tests for isolated coders; no model or mod code runs."""
import copy
from dataclasses import replace
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
import unittest
from unittest.mock import patch

from modport.contracts import OperationInput
from modport.development import validate_plan, build_development_registry
from modport.handlers import _result


def git(root, *args):
    return subprocess.check_output(['git', '-c', 'user.name=Test', '-c', 'user.email=test@example.invalid', *args], cwd=root, stderr=subprocess.DEVNULL).decode().strip()


def task(identifier, owned, dependencies=()):
    return {'id': identifier, 'objective': 'Update ' + identifier, 'dependencies': list(dependencies),
            'owned_paths': [owned], 'acceptance': ['Preserve expected behavior'], 'complexity': 'simple'}


class PlanTests(unittest.TestCase):
    def test_v17_keeps_safe_project_paths_and_reports_policy_normalization(self):
        plan = {'base_commit': 'a' * 40, 'tasks': [{
            'id': 'repair', 'objective': 'Repair the harness integration',
            'owned_paths': ['build.gradle', 'src/main/java/**', '.modport'],
            'acceptance': [], 'dependencies': []}]}
        result = validate_plan(plan, allow_contract=True, workflow_version=17)
        self.assertEqual(['build.gradle', 'src/main/java', '.modport'],
                         result['tasks'][0]['owned_paths'])
        self.assertIn('diagnostics', result)

    def setUp(self):
        self.plan = {'schema_version': 1, 'base_commit': 'a' * 40, 'shared_paths': ['build.gradle'],
                     'tasks': [task('second', 'src/b', ['first']), task('first', 'src/a')]}

    def test_topological_order_and_models(self):
        result = validate_plan(self.plan)
        self.assertEqual(['first', 'second'], [x['id'] for x in result['tasks']])
        self.assertEqual('gpt-5.6-luna', result['tasks'][0]['model'])
        self.assertEqual('high', result['tasks'][0]['reasoning_effort'])
        self.plan['tasks'][0]['complexity'] = 'complex'
        complex_task = validate_plan(self.plan)['tasks'][1]
        self.assertEqual('gpt-5.6-luna', complex_task['model'])
        self.assertEqual('high', complex_task['reasoning_effort'])

    def test_planner_model_overrides_obey_policy(self):
        for model, effort in [('other', 'medium'), ('gpt-6-astra', 'high')]:
            with self.subTest(model=model, effort=effort), self.assertRaises(ValueError):
                value = copy.deepcopy(self.plan)
                value['tasks'][0].update(model=model, reasoning_effort=effort)
                validate_plan(value)
        self.plan['tasks'][0].update(model='gpt-5.6-luna', reasoning_effort='max')
        with self.assertRaises(ValueError):
            validate_plan(self.plan)

    def test_pre_v15_frozen_plan_keeps_legacy_model_policy(self):
        legacy = validate_plan(self.plan, workflow_version=14)
        self.assertEqual(('gpt-5.6-luna', 'max'),
                         (legacy['tasks'][0]['model'], legacy['tasks'][0]['reasoning_effort']))
        self.plan['tasks'][0]['complexity'] = 'complex'
        legacy = validate_plan(self.plan, workflow_version=14)
        self.assertEqual(('gpt-6-astra', 'medium'),
                         (legacy['tasks'][1]['model'], legacy['tasks'][1]['reasoning_effort']))

    def test_shared_contract_paths_are_allowed_but_cannot_overlap_coders(self):
        self.plan['shared_paths'].append('.modport/functional-contract.json')
        validate_plan(self.plan)
        self.plan['shared_paths'].append('.modport')
        self.plan['tasks'][0]['owned_paths'] = ['.modport/tests']
        with self.assertRaises(ValueError):
            validate_plan(self.plan)

    def test_cycles_missing_duplicate_and_overlap(self):
        variants = []
        v = copy.deepcopy(self.plan); v['tasks'][1]['dependencies'] = ['second']; variants.append(v)
        v = copy.deepcopy(self.plan); v['tasks'][0]['dependencies'] = ['missing']; variants.append(v)
        v = copy.deepcopy(self.plan); v['tasks'][0]['id'] = 'first'; variants.append(v)
        v = copy.deepcopy(self.plan); v['tasks'][0]['owned_paths'] = ['src/a/nested']; variants.append(v)
        v = copy.deepcopy(self.plan); v['tasks'][0]['owned_paths'] = ['build.gradle']; variants.append(v)
        for value in variants:
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_plan(value)

    def test_unsafe_paths_and_contracts_rejected(self):
        for path in ['/tmp/file', '../file', 'src/../file', '.git/config', 'src/.git/config',
                     '.modport', '.modport/functional-contract.json', '.modport/code-review.json', 'C:/file']:
            with self.subTest(path=path), self.assertRaises(ValueError):
                value = copy.deepcopy(self.plan); value['tasks'][0]['owned_paths'] = [path]
                validate_plan(value)
        self.plan['tasks'][0]['owned_paths'] = ['.modport/tests']
        validate_plan(self.plan)

    def test_preparation_can_own_harness_support_but_never_contract_or_evidence(self):
        value = copy.deepcopy(self.plan)
        value['tasks'][0].update(kind='prepare', owned_paths=['.modport/client-harness'])
        validate_plan(value, allow_preparation=True)
        for path in ('.modport', '.modport/functional-contract.json', '.modport/evidence/results.json'):
            value['tasks'][0]['owned_paths'] = [path]
            with self.assertRaises(ValueError):
                validate_plan(value, allow_preparation=True)


class IsolatedDevelopmentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.work = self.root / 'worktree'
        self.work.mkdir()
        git(self.work, 'init')
        (self.work / 'a.txt').write_text('original a\n')
        (self.work / 'b.txt').write_text('original b\n')
        git(self.work, 'add', '.'); git(self.work, 'commit', '-m', 'initial')
        self.plan = {'schema_version': 1, 'base_commit': git(self.work, 'rev-parse', 'HEAD'),
                     'shared_paths': [], 'tasks': [task('a', 'a.txt'), task('b', 'b.txt', ['a'])]}
        (self.work / '.modport').mkdir()
        (self.work / '.modport/development-plan.json').write_text(json.dumps(self.plan))
        git(self.work, 'add', '.'); git(self.work, 'commit', '-m', 'plan')
        self.registry = build_development_registry()
        # Coder tests start with a host-frozen artifact; approval gates are tested
        # separately in test_planning instead of accepting legacy on-disk plans.
        from modport.development import _artifact
        self.freeze_command = OperationInput('run', 'implementation', 'implementation', 'freeze', str(self.root))
        self.base = git(self.work, 'rev-parse', 'HEAD')
        self.plan['base_commit'] = self.base
        normalized = validate_plan(self.plan, workflow_version=getattr(self, "workflow_version", 15))
        ref = _artifact(self.freeze_command, 'development-plan.json', json.dumps(normalized).encode(), {'development_base': self.base})
        self.frozen = _result(self.freeze_command, 'completed', outputs={'development_base': self.base,
            'development_tasks': normalized['tasks'], 'artifact_refs': {'development_plan': ref}})

    def command(self, stage, identifier, *, patches=(), results=()):
        payload = {'development_base': self.base, 'development_generation': 1,
                   'dependency_patches': list(patches), 'development_results': list(results)}
        options = {}
        if stage == 'coder':
            item = next(t for t in self.frozen.outputs['development_tasks'] if t['id'] == identifier)
            payload['development_task'] = item
            options['workspace'] = f'workspaces/development/g1/{identifier}'
        return OperationInput('run', identifier, stage, stage + '-' + identifier, str(self.root),
                              payload=payload, options=options, artifact_refs=self.frozen.outputs['artifact_refs'])

    def coder(self, identifier, patches=(), mutation=None):
        command = IsolatedDevelopmentTests.command(self, 'coder', identifier, patches=patches)
        def fake(_handler, cmd):
            workspace = self.root / cmd.options['workspace']
            if mutation:
                mutation(workspace)
            else:
                if identifier == 'b':
                    self.assertEqual('changed a\n', (workspace / 'a.txt').read_text())
                (workspace / (identifier + '.txt')).write_text('changed ' + identifier + '\n')
                git(workspace, 'add', '.'); git(workspace, 'commit', '-m', 'coder change')
            return _result(cmd, 'completed')
        with patch('modport.handlers.CodexStageHandler.__call__', fake):
            return self.registry['coder'](command)

    def test_dependency_delta_and_integration(self):
        first = self.coder('a')
        self.assertEqual('completed', first.status, first.detail)
        second = self.coder('b', [first.outputs['artifact_refs']['coder_patch']])
        self.assertEqual('completed', second.status, second.detail)
        ref = second.outputs['artifact_refs']['coder_patch']
        self.assertEqual(['b.txt'], ref['metadata']['paths'])
        self.assertNotIn('diff --git a/a.txt', (self.root / ref['path']).read_text())
        self.assertEqual('original a\n', (self.work / 'a.txt').read_text())
        cmd = IsolatedDevelopmentTests.command(self, 'development_integrate', 'join', results=[second.to_dict(), first.to_dict()])
        joined = self.registry['development_integrate'](cmd)
        self.assertEqual('completed', joined.status, joined.detail)
        self.assertEqual('changed a\n', (self.work / 'a.txt').read_text())
        self.assertEqual('changed b\n', (self.work / 'b.txt').read_text())
        self.assertEqual('', git(self.work, 'status', '--porcelain'))
        self.assertTrue((self.root / 'workspaces/development/g1/a/.git').is_dir())

    def test_v25_dependency_patches_reach_production_coder_and_integration(self):
        def current_command(stage, identifier, *, patches=(), results=()):
            command = self.command(stage, identifier, patches=patches, results=results)
            return replace(command, options={**command.options, 'workflow_version': 25})

        def deterministic_agent(_handler, command):
            workspace = self.root / command.options['workspace']
            identifier = command.payload['development_task']['id']
            if identifier == 'b':
                self.assertEqual('v25 changed a\n', (workspace / 'a.txt').read_text())
            (workspace / (identifier + '.txt')).write_text(
                'v25 changed ' + identifier + '\n')
            return _result(command, 'completed')

        with patch('modport.handlers.CodexStageHandler.__call__', deterministic_agent):
            first = self.registry['coder'](current_command('coder', 'a'))
            self.assertEqual('completed', first.status, first.detail)
            a_patch = first.outputs['artifact_refs']['coder_patch']
            second = self.registry['coder'](current_command(
                'coder', 'b', patches=[a_patch]))
            self.assertEqual('completed', second.status, second.detail)
            self.assertEqual(['a'], second.outputs['dependency_patch_tasks'])
            self.assertEqual('unverified', second.outputs['acceptance_status'])

        b_patch = second.outputs['artifact_refs']['coder_patch']
        self.assertEqual(['b.txt'], b_patch['metadata']['paths'])
        self.assertNotIn('diff --git a/a.txt', (self.root / b_patch['path']).read_text())
        joined = self.registry['development_integrate'](current_command(
            'development_integrate', 'join', results=[second.to_dict(), first.to_dict()]))
        self.assertEqual('completed', joined.status, joined.detail)
        self.assertEqual(['a', 'b'], joined.outputs['integrated_task_ids'])
        self.assertEqual('v25 changed a\n', (self.work / 'a.txt').read_text())
        self.assertEqual('v25 changed b\n', (self.work / 'b.txt').read_text())

    def test_unowned_committed_edit_rejected(self):
        def mutate(root):
            (root / 'b.txt').write_text('forbidden\n')
            git(root, 'add', '.'); git(root, 'commit', '-m', 'forbidden')
        result = self.coder('a', mutation=mutate)
        self.assertEqual('blocked', result.status)
        self.assertIn('unowned', result.detail)
        self.assertNotIn('coder_patch', result.outputs.get('artifact_refs', {}))

    def test_v17_exports_committed_delta_outside_declared_ownership(self):
        command = self.command('coder', 'a')
        command = replace(command, options={**command.options, 'workflow_version': 17})
        def fake(_handler, cmd):
            workspace = self.root / cmd.options['workspace']
            (workspace / 'b.txt').write_text('advisory ownership\n')
            git(workspace, 'add', 'b.txt'); git(workspace, 'commit', '-m', 'broader repair')
            return _result(cmd, 'completed', outputs={
                'native_goal': {'host_accepted': False},
                'business_diagnostics': ['check failed']})
        with patch('modport.handlers.CodexStageHandler.__call__', fake):
            result = self.registry['coder'](command)
        self.assertEqual('completed', result.status, result.detail)
        self.assertEqual(['b.txt'], result.outputs['paths'])
        self.assertIn('coder_patch', result.outputs['artifact_refs'])

    def test_v17_integrates_available_patch_without_all_task_results(self):
        first = self.coder('a')
        partial = first.to_dict()
        partial['status'] = 'failed'
        command = self.command('development_integrate', 'join', results=[partial])
        command = replace(command, options={'workflow_version': 17})
        result = self.registry['development_integrate'](command)
        self.assertEqual('completed', result.status, result.detail)
        self.assertEqual('changed a\n', (self.work / 'a.txt').read_text())
        self.assertEqual(['a'], result.outputs['integrated_task_ids'])
        self.assertTrue(result.outputs['business_diagnostics'])

    def test_v17_partial_failed_coder_keeps_failure_and_committed_patch(self):
        command = self.command('coder', 'a')
        command = replace(command, options={**command.options, 'workflow_version': 17})
        def fake(_handler, cmd):
            workspace = self.root / cmd.options['workspace']
            (workspace / 'a.txt').write_text('partial commit\n')
            git(workspace, 'add', 'a.txt'); git(workspace, 'commit', '-m', 'partial')
            (workspace / 'notes.txt').write_text('unfinished regular file\n')
            return _result(cmd, 'failed', error_code='agent_failed', detail='partial execution')
        with patch('modport.handlers.CodexStageHandler.__call__', fake):
            result = self.registry['coder'](command)
        self.assertEqual('failed', result.status, result.detail)
        self.assertEqual('agent_failed', result.error_code)
        self.assertEqual('unverified', result.outputs['acceptance_status'])
        self.assertIn('coder_patch', result.outputs['artifact_refs'])

    def test_v17_native_timeout_reserves_time_to_export_stopped_partial_work(self):
        command = replace(self.command('coder', 'a'),
                          options={**self.command('coder', 'a').options, 'workflow_version': 17})

        def timed_out(_handler, delegated):
            self.assertNotIn('deadline_epoch', delegated.options)
            self.assertLess(delegated.options['model_deadline_epoch'], time.time() + 7080)
            self.assertGreater(delegated.options['host_settlement_deadline_epoch'],
                               delegated.options['model_deadline_epoch'])
            (self.root / delegated.options['workspace'] / 'a.txt').write_text('partial timeout\n')
            return _result(delegated, 'blocked', error_code='budget_exhausted',
                           outputs={'native_goal': {'producer_stopped': True}})

        with patch('modport.handlers.CodexStageHandler.__call__', timed_out):
            result = self.registry['coder'](command)
        self.assertEqual('blocked', result.status, result.detail)
        self.assertEqual('budget_exhausted', result.error_code)
        self.assertIn('coder_patch', result.outputs['artifact_refs'])
        self.assertIn('partial timeout', (self.root /
            result.outputs['artifact_refs']['coder_patch']['path']).read_text())

    def test_v17_continued_coder_receives_exact_explicit_rework_instruction(self):
        old = self.command('coder', 'a')
        command = replace(old, options={**old.options, 'workflow_version': 17},
            payload={**old.payload, 'recovered_rework_request': {
                'request_id': 'focused', 'instructions': 'Fix a.txt:1 and rerun the target check',
                'failure': 'target check failed'}})

        def fake(handler, delegated):
            self.assertIn('Fix a.txt:1 and rerun the target check', handler.prompt)
            self.assertIn('focused', handler.prompt)
            return _result(delegated, 'completed')

        with patch('modport.handlers.CodexStageHandler.__call__', fake):
            result = self.registry['coder'](command)
        self.assertEqual('completed', result.status, result.detail)

    def test_v17_host_collects_uncommitted_add_modify_delete_and_mode(self):
        command = replace(self.command('coder', 'a'),
                          options={**self.command('coder', 'a').options, 'workflow_version': 17})
        def fake(handler, cmd):
            self.assertTrue(cmd.options['host_collect_candidate'])
            workspace = self.root / cmd.options['workspace']
            (workspace / 'a.txt').write_text('uncommitted edit\n')
            (workspace / 'new.sh').write_text('#!/bin/sh\nexit 0\n')
            (workspace / 'new.sh').chmod(0o755)
            (workspace / 'b.txt').unlink()
            return _result(cmd, 'blocked', error_code='agent_blocked', detail='handoff only')
        with patch('modport.handlers.CodexStageHandler.__call__', fake):
            result = self.registry['coder'](command)
        self.assertEqual('blocked', result.status, result.detail)
        self.assertEqual(['a.txt', 'b.txt', 'new.sh'], result.outputs['paths'])
        refs = result.outputs['artifact_refs']
        self.assertIn('host_candidate', refs)
        patch_text = (self.root / refs['coder_patch']['path']).read_text()
        self.assertIn('new file mode 100755', patch_text)
        self.assertIn('deleted file mode', patch_text)
        candidate = json.loads((self.root / refs['host_candidate']['path']).read_text())
        self.assertEqual(result.outputs['head'], candidate['head'])
        workspace = self.root / command.options['workspace']
        self.assertEqual(self.base, git(workspace, 'rev-parse', 'HEAD'))
        self.assertEqual(candidate['head'], git(
            self.root / candidate['repository'], 'rev-parse', candidate['ref']))

    def test_v17_host_collection_excludes_reports_credentials_caches_and_builds(self):
        command = self.command('coder', 'a')
        command = replace(command, options={**command.options, 'workflow_version': 17})
        def fake(_handler, cmd):
            workspace = self.root / cmd.options['workspace']
            (workspace / 'a.txt').write_text('safe\n')
            for relative in ('.env', '.gradle/cache.bin', 'module/build/output.bin',
                             '.modport/goal-reports/a.json'):
                path = workspace / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text('must not leave workspace')
            return _result(cmd, 'completed')
        with patch('modport.handlers.CodexStageHandler.__call__', fake):
            result = self.registry['coder'](command)
        self.assertEqual('completed', result.status, result.detail)
        self.assertEqual(['a.txt'], result.outputs['paths'])
        patch_text = (self.root / result.outputs['artifact_refs']['coder_patch']['path']).read_text()
        for excluded in ('.env', '.gradle', 'module/build', '.modport/goal-reports'):
            self.assertNotIn(excluded, patch_text)
        self.assertTrue(any('host candidate excluded' in item
                            for item in result.outputs['business_diagnostics']))

    def test_v17_host_collection_is_idempotent_for_the_same_filesystem(self):
        from modport.host_candidate import collect_host_candidate, export_host_candidate
        command = self.command('coder', 'a')
        command = replace(command, options={**command.options, 'workflow_version': 17})
        workspace = self.root / command.options['workspace']
        workspace.parent.mkdir(parents=True, exist_ok=True)
        git(self.root, 'clone', '--no-hardlinks', '--no-checkout', str(self.work), str(workspace))
        git(workspace, 'checkout', '--detach', self.base)
        (workspace / 'a.txt').write_text('recoverable\n')
        partial = self.root / 'artifacts/executions' / command.command_id / 'host-candidate.git'
        partial.mkdir(parents=True)
        first = collect_host_candidate(command, workspace, self.base, 'a')
        second = collect_host_candidate(command, workspace, self.base, 'a')
        self.assertEqual(first.record, second.record)
        destination = self.root / 'artifacts/executions' / command.command_id / 'coder.patch'
        self.assertEqual(export_host_candidate(command, first, destination),
                         export_host_candidate(command, second, destination))
        (workspace / 'a.txt').write_text('different recovery state\n')
        changed = collect_host_candidate(command, workspace, self.base, 'a')
        self.assertNotEqual(first.record['ref'], changed.record['ref'])
        with self.assertRaises(ValueError):
            export_host_candidate(command, changed, destination)
        self.assertEqual(first.head, git(first.repository, 'rev-parse', first.record['ref']))

    def test_v17_materialized_projection_uses_candidate_blobs_and_omits_tracked_builds(self):
        from modport.host_candidate import collect_host_candidate, materialize_host_candidate_view
        command = self.command('coder', 'a')
        command = replace(command, options={**command.options, 'workflow_version': 17})
        workspace = self.root / command.options['workspace']
        workspace.parent.mkdir(parents=True, exist_ok=True)
        git(self.root, 'clone', '--no-hardlinks', '--no-checkout', str(self.work), str(workspace))
        git(workspace, 'checkout', '--detach', self.base)
        tracked_build = workspace / 'module/build/tracked.bin'
        tracked_build.parent.mkdir(parents=True)
        tracked_build.write_text('tracked baseline build output')
        git(workspace, 'add', 'module/build/tracked.bin')
        git(workspace, 'commit', '-m', 'tracked generated fixture')
        start = git(workspace, 'rev-parse', 'HEAD')
        (workspace / 'a.txt').write_text('candidate bytes\n')
        candidate = collect_host_candidate(command, workspace, start, 'a')
        (workspace / 'a.txt').write_text('later live bytes\n')
        view = materialize_host_candidate_view(command, candidate)
        self.assertEqual('candidate bytes\n', (view / 'a.txt').read_text())
        self.assertFalse((view / 'module/build/tracked.bin').exists())

    def test_v17_committed_symlink_is_not_exported_as_project_patch(self):
        command = self.command('coder', 'a')
        command = replace(command, options={**command.options, 'workflow_version': 17})
        def fake(_handler, cmd):
            workspace = self.root / cmd.options['workspace']
            (workspace / 'a.txt').unlink()
            (workspace / 'a.txt').symlink_to(self.root / 'outside')
            (workspace / 'escape').symlink_to(self.root / 'outside')
            git(workspace, 'add', 'a.txt', 'escape'); git(workspace, 'commit', '-m', 'unsafe link')
            outside = self.root / 'outside-credential'
            outside.write_text('host-only')
            (workspace / 'b.txt').unlink()
            os.link(outside, workspace / 'b.txt')
            return _result(cmd, 'completed')
        with patch('modport.handlers.CodexStageHandler.__call__', fake):
            result = self.registry['coder'](command)
        self.assertEqual('completed', result.status)
        self.assertEqual([], result.outputs['paths'])
        self.assertEqual(self.base, result.outputs['head'])
        self.assertIn('coder_patch', result.outputs.get('artifact_refs', {}))
        self.assertTrue(any('host candidate excluded' in item
                            for item in result.outputs['business_diagnostics']))

    def test_dirty_and_untracked_rejected(self):
        result = self.coder('a', mutation=lambda root: (root / 'untracked.txt').write_text('dirty'))
        self.assertEqual('blocked', result.status)
        self.assertIn('dirty', result.detail)

    def test_rename_old_and_new_paths_checked(self):
        def mutate(root):
            git(root, 'mv', 'a.txt', 'other.txt'); git(root, 'commit', '-m', 'rename')
        result = self.coder('a', mutation=mutate)
        self.assertEqual('blocked', result.status)
        self.assertIn('other.txt', result.detail)

    def test_forbidden_edit_then_revert_still_rejected(self):
        def mutate(root):
            (root / 'b.txt').write_text('forbidden\n')
            git(root, 'add', '.'); git(root, 'commit', '-m', 'forbidden')
            git(root, 'revert', '--no-edit', 'HEAD')
        result = self.coder('a', mutation=mutate)
        self.assertEqual('blocked', result.status)
        self.assertIn('unowned', result.detail)

    def test_owned_deletion_is_exported(self):
        def mutate(root):
            git(root, 'rm', 'a.txt'); git(root, 'commit', '-m', 'delete owned file')
        result = self.coder('a', mutation=mutate)
        self.assertEqual('completed', result.status, result.detail)
        self.assertEqual(['a.txt'], result.outputs['paths'])
        path = self.root / result.outputs['artifact_refs']['coder_patch']['path']
        self.assertIn('deleted file mode', path.read_text())

    def test_missing_dependencies_rejected_before_model(self):
        result = self.coder('b')
        self.assertEqual('blocked', result.status)
        self.assertIn('transitive', result.detail)

    def test_failed_result_and_tampered_patch_do_not_integrate(self):
        first = self.coder('a')
        second = self.coder('b', [first.outputs['artifact_refs']['coder_patch']])
        values = [first.to_dict(), second.to_dict()]
        values[1]['status'] = 'failed'
        cmd = IsolatedDevelopmentTests.command(self, 'development_integrate', 'failed', results=values)
        self.assertEqual('failed', self.registry['development_integrate'](cmd).status)
        ref = first.outputs['artifact_refs']['coder_patch']
        (self.root / ref['path']).write_text('tampered')
        cmd = IsolatedDevelopmentTests.command(self, 'development_integrate', 'tampered', results=[first.to_dict(), second.to_dict()])
        result = self.registry['development_integrate'](cmd)
        self.assertEqual('failed', result.status)
        self.assertEqual(self.base, git(self.work, 'rev-parse', 'HEAD'))

    def test_conflict_rolls_back_prior_integrations(self):
        first = self.coder('a')
        second = self.coder('b', [first.outputs['artifact_refs']['coder_patch']])
        # Authenticated but invalid patch exercises deterministic rollback after task a.
        ref = copy.deepcopy(second.outputs['artifact_refs']['coder_patch'])
        path = self.root / ref['path']; path.write_text('invalid patch\n')
        from hashlib import sha256
        ref['sha256'] = sha256(path.read_bytes()).hexdigest()
        value = second.to_dict(); value['outputs']['artifact_refs']['coder_patch'] = ref
        cmd = IsolatedDevelopmentTests.command(self, 'development_integrate', 'conflict', results=[first.to_dict(), value])
        result = self.registry['development_integrate'](cmd)
        self.assertEqual('integration_conflict', result.error_code)
        self.assertEqual(self.base, git(self.work, 'rev-parse', 'HEAD'))
        self.assertEqual('original a\n', (self.work / 'a.txt').read_text())
        self.assertTrue(path.exists())


if __name__ == '__main__':
    unittest.main()
