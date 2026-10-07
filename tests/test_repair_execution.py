"""Repair snapshots preserve dirty inputs and publish only authenticated deltas."""
from dataclasses import replace
from hashlib import sha256
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from modport.contracts import OperationInput
from modport.development import _artifact, validate_plan
from modport.handlers import _result
from modport.repair_execution import RepairPrepareHandler, RepairIntegrateHandler
from test_development import git, task


class RepairExecutionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / 'baseline'
        self.prefix = '.modport'
        self.source.mkdir()
        git(self.source, 'init')
        (self.source / 'source.txt').write_text('frozen source')
        git(self.source, 'add', '.')
        git(self.source, 'commit', '-m', 'source')
        self.head = git(self.source, 'rev-parse', 'HEAD')
        (self.source / '.modport').mkdir()
        (self.source / '.modport/a.json').write_text('old a')
        (self.source / '.modport/b.json').write_text('old b')
        self.plan = validate_plan({'schema_version': 1, 'base_commit': self.head,
            'shared_paths': [], 'tasks': [{**task('repair', '.modport/a.json'),
                'owned_paths': ['.modport/a.json', '.modport/b.json']}]}, allow_contract=True)
        self.command = OperationInput('run', 'repair', 'contract_revise', 'prepare', str(self.root))

    def prepare(self):
        with patch('modport.planning.approved_repair_development_plan', return_value=self.plan, create=True):
            return RepairPrepareHandler()(self.command)

    def integration(self, *, delete=False, project=False):
        prepared = self.prepare()
        self.assertEqual('completed', prepared.status, prepared.detail)
        base = prepared.outputs['development_base']
        workspace = self.root / prepared.outputs['development_source_workspace']
        # Produce the same authenticated patch format as the coder handler.
        (workspace / self.prefix / 'a.json').write_text('new a')
        if delete:
            (workspace / self.prefix / 'b.json').unlink()
        else:
            (workspace / self.prefix / 'b.json').write_text('new b')
        if project:
            (workspace / 'source.txt').write_text('instrumented baseline')
        git(workspace, 'add', '.')
        git(workspace, 'commit', '-m', 'repair')
        destination = self.root / 'delta.patch'
        git(workspace, 'diff', '--binary', '--full-index', '--no-renames', '--output=' + str(destination), base, 'HEAD')
        patch_ref = _artifact(self.command, 'coder.patch', destination.read_bytes(), {
            'task_id': 'repair', 'base': base, 'generation': 1,
            'paths': [self.prefix + '/a.json', self.prefix + '/b.json']
                     + (['source.txt'] if project else [])})
        git(workspace, 'reset', '--hard', base)
        coder = _result(replace(self.command, stage_id='coder'), 'completed', outputs={
            'development_task_id': 'repair', 'artifact_refs': {'coder_patch': patch_ref}})
        return replace(self.command, stage_id=self.command.stage_id.replace('_revise', '_repair_integrate'), command_id='integrate',
            payload={**prepared.outputs, 'development_generation': 1, 'development_results': [coder.to_dict()]},
            artifact_refs=prepared.outputs['artifact_refs'])

    def test_snapshot_includes_untracked_harness_without_changing_source_head(self):
        result = self.prepare()
        self.assertEqual('completed', result.status, result.detail)
        workspace = self.root / result.outputs['development_source_workspace']
        self.assertEqual('old a', (workspace / '.modport/a.json').read_text())
        self.assertEqual('', git(workspace, 'status', '--porcelain'))
        self.assertEqual(self.head, git(self.source, 'rev-parse', 'HEAD'))
        self.assertIn('?? .modport/', git(self.source, 'status', '--porcelain'))

    def test_approved_patch_updates_harness_and_preserves_source_head(self):
        result = RepairIntegrateHandler()(self.integration())
        self.assertEqual('completed', result.status, result.detail)
        self.assertEqual('new a', (self.source / '.modport/a.json').read_text())
        self.assertEqual('new b', (self.source / '.modport/b.json').read_text())
        self.assertEqual('frozen source', (self.source / 'source.txt').read_text())
        self.assertEqual(self.head, git(self.source, 'rev-parse', 'HEAD'))

    def test_v17_publishes_project_repair_with_explicit_modified_baseline_provenance(self):
        self.command = replace(self.command, options={'workflow_version': 17})
        self.plan = validate_plan({'base_commit': self.head, 'tasks': [{
            **task('repair', 'source.txt'),
            'owned_paths': ['source.txt', '.modport']}],
            'shared_paths': []}, allow_contract=True, workflow_version=17)
        result = RepairIntegrateHandler()(self.integration(project=True))
        self.assertEqual('completed', result.status, result.detail)
        self.assertEqual('instrumented baseline', (self.source / 'source.txt').read_text())
        self.assertEqual(['source.txt'], result.outputs['baseline_project_changes'])
        self.assertFalse(result.outputs['represents_original_source'])
        self.assertEqual(self.head, git(self.source, 'rev-parse', 'HEAD'))

    def test_stale_source_rejected_before_any_publication(self):
        command = self.integration()
        (self.source / 'source.txt').write_text('concurrent update')
        result = RepairIntegrateHandler()(command)
        self.assertEqual('blocked', result.status)
        self.assertIn('stale', result.detail)
        self.assertEqual('old a', (self.source / '.modport/a.json').read_text())
        self.assertEqual('concurrent update', (self.source / 'source.txt').read_text())

    def test_snapshot_rejects_another_authenticated_plan_with_same_base(self):
        command = self.integration()
        original_ref = command.artifact_refs['development_plan']
        other = _artifact(self.command, 'other-plan.json',
            (self.root / original_ref['path']).read_bytes(), original_ref['metadata'])
        command = replace(command, artifact_refs={**command.artifact_refs, 'development_plan': other})
        result = RepairIntegrateHandler()(command)
        self.assertEqual('blocked', result.status)
        self.assertIn('identity mismatch', result.detail)
        self.assertEqual('old a', (self.source / '.modport/a.json').read_text())

    def test_modified_snapshot_digest_is_rejected(self):
        command = self.integration()
        path = self.root / command.artifact_refs['repair_snapshot']['path']
        path.write_text(path.read_text() + ' ')
        result = RepairIntegrateHandler()(command)
        self.assertEqual('blocked', result.status)
        self.assertIn('digest mismatch', result.detail)
        self.assertEqual('old a', (self.source / '.modport/a.json').read_text())

    def test_publication_failure_rolls_back_prior_files(self):
        command = self.integration()
        from modport.repair_execution import _install_file
        calls = []
        def install(*args):
            calls.append(args)
            if len(calls) == 2:
                raise OSError('injected publication failure')
            _install_file(*args)
        with patch('modport.repair_execution._install_file', side_effect=install):
            result = RepairIntegrateHandler()(command)
        self.assertEqual('blocked', result.status)
        self.assertEqual('old a', (self.source / '.modport/a.json').read_text())
        self.assertEqual('old b', (self.source / '.modport/b.json').read_text())
        self.assertEqual(self.head, git(self.source, 'rev-parse', 'HEAD'))

    def test_symlink_snapshot_rejected_without_reading_target(self):
        (self.source / 'escape').symlink_to(self.root)
        result = self.prepare()
        self.assertEqual('blocked', result.status)
        self.assertIn('symlinks', result.detail)


class TargetRepairExecutionTests(unittest.TestCase):
    prepare = RepairExecutionTests.prepare
    integration = RepairExecutionTests.integration

    def setUp(self):
        RepairExecutionTests.setUp(self)
        target = self.root / 'worktree'
        self.source.rename(target)
        self.source = target
        (target / '.modport').rename(target / 'src')
        self.prefix = 'src'
        git(target, 'add', '.')
        git(target, 'commit', '-m', 'target input')
        self.head = git(target, 'rev-parse', 'HEAD')
        self.plan = validate_plan({'schema_version': 1, 'base_commit': self.head,
            'shared_paths': [], 'tasks': [task('repair', 'src')]})
        self.command = replace(self.command, stage_id='target_revise')

    def test_target_commits_only_owned_files_preserving_unrelated_index_and_working_changes(self):
        (self.source / 'source.txt').write_text('staged unrelated')
        git(self.source, 'add', 'source.txt')
        (self.source / 'source.txt').write_text('unstaged unrelated')
        result = RepairIntegrateHandler()(self.integration())
        self.assertEqual('completed', result.status, result.detail)
        self.assertNotEqual(self.head, git(self.source, 'rev-parse', 'HEAD'))
        self.assertEqual(self.head, git(self.source, 'rev-parse', 'HEAD^'))
        self.assertEqual('staged unrelated', git(self.source, 'show', ':source.txt'))
        self.assertEqual('frozen source', git(self.source, 'show', 'HEAD:source.txt'))
        self.assertEqual('unstaged unrelated', (self.source / 'source.txt').read_text())
        self.assertEqual('MM source.txt', git(self.source, 'status', '--porcelain'))

    def test_target_deletion_is_committed_and_checkout_is_clean(self):
        result = RepairIntegrateHandler()(self.integration(delete=True))
        self.assertEqual('completed', result.status, result.detail)
        self.assertFalse((self.source / 'src/b.json').exists())
        self.assertEqual('', git(self.source, 'status', '--porcelain'))
        self.assertEqual('new a', git(self.source, 'show', 'HEAD:src/a.json'))

    def test_artifact_failure_rolls_back_target_commit_index_and_working_files(self):
        (self.source / 'source.txt').write_text('staged unrelated')
        git(self.source, 'add', 'source.txt')
        command = self.integration()
        original_index = (self.source / '.git/index').read_bytes()
        from modport.repair_execution import _artifact as artifact
        def fail_final(cmd, name, *args, **kwargs):
            if name == 'repair-integration.json':
                raise OSError('injected final artifact failure')
            return artifact(cmd, name, *args, **kwargs)
        with patch('modport.repair_execution._artifact', side_effect=fail_final):
            result = RepairIntegrateHandler()(command)
        self.assertEqual('blocked', result.status)
        self.assertEqual(self.head, git(self.source, 'rev-parse', 'HEAD'))
        self.assertEqual(original_index, (self.source / '.git/index').read_bytes())
        self.assertEqual('old a', (self.source / 'src/a.json').read_text())
        self.assertEqual('old b', (self.source / 'src/b.json').read_text())
        self.assertEqual('staged unrelated', git(self.source, 'show', ':source.txt'))

    def test_expired_work_budget_still_rolls_back_partial_head_update(self):
        command = replace(self.integration(), options={'deadline_epoch': 1005})
        index = self.source / '.git/index'
        original_index = index.read_bytes()
        from modport import repair_execution
        replace_file = repair_execution.os.replace
        git_command = repair_execution._git
        clock = [1000]
        updates = []
        def record_update(cmd, workspace, *args, **kwargs):
            result = git_command(cmd, workspace, *args, **kwargs)
            if args[0] == 'update-ref':
                updates.append(cmd.options['deadline_epoch'])
            return result
        def fail_publication(source, destination):
            if Path(destination) == index and clock[0] == 1000:
                clock[0] = 1006
                raise OSError('index publication failed after work deadline')
            return replace_file(source, destination)
        with patch('modport.handlers.time.time', side_effect=lambda: clock[0]), \
             patch('modport.repair_execution.os.replace', side_effect=fail_publication), \
             patch('modport.repair_execution._git', side_effect=record_update):
            result = RepairIntegrateHandler()(command)
        self.assertEqual(result.status, 'blocked')
        self.assertEqual(result.error_code, 'repair_integration_invalid', result.detail)
        self.assertEqual(updates, [1005, 1036])
        self.assertEqual(self.head, git(self.source, 'rev-parse', 'HEAD'))
        self.assertEqual(original_index, index.read_bytes())
        self.assertEqual('old a', (self.source / 'src/a.json').read_text())
        self.assertEqual('old b', (self.source / 'src/b.json').read_text())

    def test_internal_git_rollback_failure_is_explicit_and_files_are_restored(self):
        command = self.integration()
        index = self.source / '.git/index'
        from modport import repair_execution
        replace_file = repair_execution.os.replace
        def fail_publication(source, destination):
            if Path(destination) == index:
                raise OSError('index publication failed')
            return replace_file(source, destination)
        with patch('modport.repair_execution.os.replace', side_effect=fail_publication), \
             patch('modport.repair_execution._rollback_target', side_effect=OSError('Git rollback unavailable')):
            result = RepairIntegrateHandler()(command)
        self.assertEqual(result.error_code, 'repair_rollback_failed', result.detail)
        self.assertIn('Git rollback unavailable', result.detail)
        self.assertEqual('old a', (self.source / 'src/a.json').read_text())
        self.assertEqual('old b', (self.source / 'src/b.json').read_text())


if __name__ == '__main__':
    unittest.main()
