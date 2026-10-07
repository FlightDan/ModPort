"""Repair joins merge into the current product while retaining author provenance."""
from dataclasses import replace
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from modport.contracts import OperationInput, OperationResult
from modport.development import _artifact, validate_plan
from modport.repair_execution import RepairIntegrateHandler, RepairPrepareHandler
from modport.workflow import WORKFLOW_VERSION
from test_rework_caller_workspace import git


class CurrentRepairWorkspaceTests(unittest.TestCase):
    def fixture(self, scope, *, ignored_input=False):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        relative = 'baseline' if scope == 'contract' else 'worktree'
        source = root / relative
        source.mkdir()
        git(source, 'init')
        (source / 'source.txt').write_text('source behavior\n')
        (source / 'peer.txt').write_text('original peer\n')
        (source / '.modport').mkdir()
        (source / '.modport/harness.json').write_text('old harness\n')
        changed = '.modport/harness.json' if scope == 'contract' else 'source.txt'
        if ignored_input:
            (source / '.gitignore').write_text(changed + '\nuntouched.txt\n')
            (source / 'untouched.txt').write_text('unrelated ignored input\n')
        git(source, 'add', '.')
        git(source, 'commit', '-m', 'source')
        head = git(source, 'rev-parse', 'HEAD')
        plan = validate_plan({'schema_version': 1, 'base_commit': head,
            'shared_paths': [], 'tasks': [{'id': 'repair', 'objective': 'Repair the selected behavior',
                'dependencies': [], 'owned_paths': [changed],
                'acceptance': ['Preserve the peer while repairing the selected behavior']}]},
            allow_contract=scope == 'contract', workflow_version=WORKFLOW_VERSION)
        command = OperationInput('repair-run', scope + '_revise', scope + '_revise',
            'prepare-' + scope, str(root), options={'workflow_version': WORKFLOW_VERSION,
                                                  'deadline_epoch': time.time() + 600})
        # The planner's frozen base can precede legitimate current source work.
        (source / 'peer.txt').write_text('peer before prepare\n')
        git(source, 'add', 'peer.txt')
        git(source, 'commit', '-m', 'current source after planner')
        with patch('modport.planning.approved_repair_development_plan', return_value=plan):
            prepared = RepairPrepareHandler()(command)
        self.assertEqual('completed', prepared.status, prepared.detail)
        snapshot_path = root / prepared.outputs['artifact_refs']['repair_snapshot']['path']
        snapshot_bytes = snapshot_path.read_bytes()
        record = json.loads(snapshot_bytes)
        self.assertTrue(all(set(row) == {'mode'} for row in record['files'].values()))
        author_workspace = root / prepared.outputs['development_source_workspace']
        base = prepared.outputs['development_base']
        (author_workspace / changed).write_text('repaired behavior\n')
        git(author_workspace, 'add', changed)
        git(author_workspace, 'commit', '-m', 'coder repair')
        author_head = git(author_workspace, 'rev-parse', 'HEAD')
        data = git(author_workspace, 'diff', '--binary', '--full-index', base, author_head, '--').encode() + b'\n'
        patch_ref = _artifact(command, 'coder.patch', data, {'task_id': 'repair',
            'base': base, 'start': base, 'head': author_head, 'paths': [changed], 'generation': 7})
        result = OperationResult('completed', command.run_id, 'coder.g7.repair', 'coder',
            'repair-author', outputs={'development_task_id': 'repair',
                                     'artifact_refs': {'coder_patch': patch_ref}})
        integration = replace(command, task_id=scope + '_repair_integrate',
            stage_id=scope + '_repair_integrate', command_id='integrate-' + scope,
            payload={'development_base': base, 'development_generation': 7,
                     'development_source_workspace': prepared.outputs['development_source_workspace'],
                     'goal_scope': scope, 'development_results': [result.to_dict()]},
            artifact_refs=prepared.outputs['artifact_refs'])
        # Current source differs in both commit history and uncommitted files.
        (source / 'peer.txt').write_text('latest peer\n')
        git(source, 'add', 'peer.txt')
        git(source, 'commit', '-m', 'peer after repair author')
        (source / 'extra.txt').write_text('legal current edit\n')
        return root, source, integration, changed, snapshot_path, snapshot_bytes

    def check_merge(self, scope):
        root, source, command, changed, snapshot_path, snapshot_bytes = self.fixture(scope)
        result = RepairIntegrateHandler()(command)
        self.assertEqual('completed', result.status, result.detail)
        self.assertEqual('repaired behavior\n', (source / changed).read_text())
        self.assertEqual('latest peer\n', (source / 'peer.txt').read_text())
        self.assertEqual('legal current edit\n', (source / 'extra.txt').read_text())
        self.assertEqual(snapshot_bytes, snapshot_path.read_bytes())
        self.assertEqual(scope, result.outputs['goal_scope'])
        self.assertIn('repair_integration', result.outputs['artifact_refs'])
        self.assertIn('development_integration', result.outputs['artifact_refs'])
        self.assertEqual([changed], result.outputs['changed_paths'])
        record = json.loads((root / result.outputs['artifact_refs']['repair_integration']['path']).read_text())
        self.assertEqual(command.artifact_refs['repair_snapshot'], record['repair_snapshot'])
        return result

    def test_target_repair_merges_current_edits_without_snapshot_replay(self):
        self.check_merge('target')

    def test_target_repair_merges_ignored_input_without_tracking_unrelated_files(self):
        _root, source, command, changed, snapshot_path, snapshot_bytes = self.fixture(
            'target', ignored_input=True)
        self.assertNotIn(changed, git(source, 'ls-files').splitlines())
        result = RepairIntegrateHandler()(command)
        self.assertEqual('completed', result.status, result.detail)
        self.assertEqual('repaired behavior\n', (source / changed).read_text())
        self.assertEqual('latest peer\n', (source / 'peer.txt').read_text())
        self.assertEqual('unrelated ignored input\n', (source / 'untouched.txt').read_text())
        self.assertNotIn('untouched.txt', git(source, 'ls-files').splitlines())
        self.assertEqual(snapshot_bytes, snapshot_path.read_bytes())

    def test_ignored_current_input_conflict_returns_actual_merge_for_coder_repair(self):
        root, source, command, changed, _snapshot_path, _snapshot_bytes = self.fixture(
            'target', ignored_input=True)
        (source / changed).write_text('newer user behavior\n')
        result = RepairIntegrateHandler()(command)
        self.assertEqual('integration_merge_required', result.error_code, result.detail)
        self.assertEqual('newer user behavior\n', (source / changed).read_text())
        merge = json.loads((root / result.outputs['integration_merge_ref']['path']).read_text())
        self.assertEqual([changed], merge['conflicts'][0]['paths'])
        self.assertNotIn('untouched.txt', git(source, 'ls-files').splitlines())

    def test_deleted_ignored_input_is_materialized_only_in_conflict_workspace(self):
        root, source, command, changed, _snapshot_path, _snapshot_bytes = self.fixture(
            'target', ignored_input=True)
        (source / changed).unlink()
        result = RepairIntegrateHandler()(command)
        self.assertEqual('integration_merge_required', result.error_code, result.detail)
        self.assertFalse((source / changed).exists())
        merge = json.loads((root / result.outputs['integration_merge_ref']['path']).read_text())
        self.assertEqual([changed], merge['conflicts'][0]['paths'])
        self.assertIn('Delete/modify conflict', merge['conflicts'][0]['detail'])
        self.assertEqual('repaired behavior\n', (root / merge['workspace'] / changed).read_text())
        self.assertNotIn('untouched.txt', git(source, 'ls-files').splitlines())
        resolution_workspace = root / merge['workspace']
        (resolution_workspace / changed).unlink()
        git(resolution_workspace, 'add', '--all', '--', changed)
        git(resolution_workspace, 'commit', '-m', 'Retain the user deletion')
        head = git(resolution_workspace, 'rev-parse', 'HEAD')
        data = git(resolution_workspace, 'diff', '--binary', '--full-index',
                   merge['merge_head'], head, '--').encode() + b'\n'
        resolution = _artifact(command, 'deletion-resolution.patch', data)
        continued = replace(command, command_id='resolved-deleted-input',
            payload={**command.payload, 'integration_resolution': {
                'merge_ref': result.outputs['integration_merge_ref'],
                'coder_patch': resolution, 'coder_execution_id': 'deletion-repair-author'}})
        settled = RepairIntegrateHandler()(continued)
        self.assertEqual('completed', settled.status, settled.detail)
        self.assertFalse((source / changed).exists())
        self.assertEqual('latest peer\n', (source / 'peer.txt').read_text())

    def test_contract_repair_merges_current_baseline_without_snapshot_replay(self):
        result = self.check_merge('contract')
        self.assertEqual([], result.outputs['baseline_project_changes'])
        self.assertTrue(result.outputs['represents_original_source'])

    def test_foreign_snapshot_scope_does_not_authorize_a_product_write(self):
        _root, source, command, changed, snapshot_path, _snapshot_bytes = self.fixture('target')
        record = json.loads(snapshot_path.read_text())
        record['scope'] = 'contract'
        snapshot_path.write_text(json.dumps(record))
        result = RepairIntegrateHandler()(command)
        self.assertEqual('blocked', result.status)
        self.assertIn('scope or author binding', result.detail)
        self.assertEqual('source behavior\n', (source / changed).read_text())
        self.assertEqual('legal current edit\n', (source / 'extra.txt').read_text())

    def test_real_repair_conflict_preserves_product_and_propagates_coder_handoff(self):
        root, source, command, changed, _snapshot_path, _snapshot_bytes = self.fixture('target')
        (source / changed).write_text('current competing behavior\n')
        git(source, 'add', changed)
        git(source, 'commit', '-m', 'competing current change')
        result = RepairIntegrateHandler()(command)
        self.assertEqual('failed', result.status, result.detail)
        self.assertEqual('integration_merge_required', result.error_code)
        self.assertEqual('current competing behavior\n', (source / changed).read_text())
        self.assertEqual('latest peer\n', (source / 'peer.txt').read_text())
        self.assertEqual('legal current edit\n', (source / 'extra.txt').read_text())
        merge = json.loads((root / result.outputs['integration_merge_ref']['path']).read_text())
        original = json.loads((root / merge['command_ref']['path']).read_text())
        self.assertEqual(command.command_id, original['command_id'])
        self.assertEqual(command.stage_id, original['stage_id'])
        self.assertEqual('target', original['payload']['goal_scope'])
        self.assertEqual('worktree', original['options']['workspace'])
        self.assertEqual([changed], merge['conflicts'][0]['paths'])


if __name__ == '__main__':
    unittest.main()
