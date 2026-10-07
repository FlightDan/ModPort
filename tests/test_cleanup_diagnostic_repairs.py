"""Late diagnostic corrections reach the candidate through private cleanup edits."""
from dataclasses import replace
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from modport.cleanup import CodeCleanupHandler
from modport.contracts import OperationInput
from modport.development import DevelopmentIntegrateHandler
from modport.diagnostic_repairs import collect, prepare
from modport.handlers import _result
from modport.workflow import WORKFLOW_VERSION


def git(root, *args):
    return subprocess.check_output([
        'git', '-c', 'core.hooksPath=/dev/null', '-c', 'user.name=Test',
        '-c', 'user.email=test@example.invalid', *args], cwd=root,
        stderr=subprocess.DEVNULL).decode().strip()


class CleanupDiagnosticRepairTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = self.root / 'worktree'
        (self.source / 'src').mkdir(parents=True)
        self.before = 'class A { void call() { wrong(); } }\n'
        self.after = 'class A { void call() { correct(); } }\n'
        (self.source / 'src/A.java').write_text(self.before)
        git(self.source, 'init')
        git(self.source, 'add', '.')
        git(self.source, 'commit', '-m', 'Integrated candidate')
        self.base = git(self.source, 'rev-parse', 'HEAD')
        self.task = {'id': 'a', 'objective': 'Preserve behavior', 'owned_paths': ['src'],
                     'dependencies': [], 'acceptance': [], 'complexity': 'simple'}
        self.plan_path = self.root / 'artifacts/plan.json'
        self.plan_path.parent.mkdir()
        self.plan_path.write_text(json.dumps({'base_commit': self.base, 'tasks': [self.task]}))
        self.plan_ref = {'path': 'artifacts/plan.json',
                         'metadata': {'development_base': self.base, 'execution_id': 'planner'}}
        self.integration = OperationInput('run', 'development_integrate', 'development_integrate',
            'integration', str(self.root), options={'workflow_version': WORKFLOW_VERSION},
            payload={'development_base': self.base, 'development_generation': 1,
                     'goal_scope': 'migration', 'development_results': []},
            artifact_refs={'development_plan': self.plan_ref})
        # The integration command is already frozen when the supervisor edits.
        target = {'task_id': 'a', 'plan_ref': self.plan_ref, 'source_workspace': 'worktree',
                  'source_execution_id': 'coder'}
        supervisor = OperationInput('run', 'supervisor', 'supervisor', 'supervisor', str(self.root),
            options={'workflow_version': WORKFLOW_VERSION}, payload={'diagnostic_repair_targets': [target]})
        prepared = prepare(supervisor)
        snapshot = self.root / prepared.options['workspace'] / 'source/task-0/src/A.java'
        snapshot.write_text(self.after)
        self.repair_refs = collect(prepared, _result(prepared, 'completed')).outputs['diagnostic_repairs']
        self.command = OperationInput('run', 'code_cleanup', 'code_cleanup', 'cleanup', str(self.root),
            options={'workflow_version': WORKFLOW_VERSION},
            payload={'diagnostic_repair_refs': self.repair_refs,
                     'diagnostic_repair_targets': [{'task_id': 'a', 'plan_ref': self.plan_ref}]})

    def invoke(self, model):
        with patch('modport.handlers.CodexStageHandler.__call__', model):
            return CodeCleanupHandler()(self.command)

    def test_repair_arriving_after_integration_dispatch_is_exported_by_cleanup(self):
        self.assertGreaterEqual(WORKFLOW_VERSION, 40)
        # The selected plan is already host-provided; do not add digest checks
        # to the diagnostic repair test. Git integration itself stays real.
        with patch('modport.development._verified', return_value=self.plan_path):
            integrated = DevelopmentIntegrateHandler()(self.integration)
        self.assertEqual('completed', integrated.status, integrated.detail)
        self.assertEqual(self.before, (self.source / 'src/A.java').read_text())

        def model(handler, command):
            workspace = self.root / command.options['workspace']
            self.assertEqual(self.after, (workspace / 'src/A.java').read_text())
            self.assertEqual(self.before, (self.source / 'src/A.java').read_text())
            self.assertEqual(self.base, git(self.source, 'rev-parse', 'HEAD'))
            self.assertIn('remaining cleanup work', handler.prompt)
            self.assertIn(self.repair_refs[0]['path'], handler.prompt)
            (workspace / 'remaining.txt').write_text('Remaining cleanup completed.\n')
            return _result(command, 'completed')

        result = self.invoke(model)
        self.assertEqual('completed', result.status, result.detail)
        self.assertEqual('integrated', result.outputs['diagnostic_repair_integration_status'])
        receipt = result.outputs['diagnostic_repair_receipts'][0]
        self.assertEqual('applied', receipt['status'])
        self.assertEqual('cleanup_clone', receipt['workspace_scope'])
        self.assertEqual(self.repair_refs, result.outputs['diagnostic_repair_refs'])
        self.assertEqual(self.after, (self.source / 'src/A.java').read_text())
        self.assertEqual('Remaining cleanup completed.\n', (self.source / 'remaining.txt').read_text())
        patch_ref = result.outputs['artifact_refs']['code_cleanup_patch']
        patch_text = (self.root / patch_ref['path']).read_text()
        self.assertIn('+class A { void call() { correct(); } }', patch_text)
        self.assertEqual('', git(self.source, 'status', '--porcelain'))
        self.assertEqual('unverified', result.outputs['acceptance_status'])

    def test_failed_cleanup_retains_clone_receipts_without_claiming_integration(self):
        def model(handler, command):
            workspace = self.root / command.options['workspace']
            self.assertEqual(self.after, (workspace / 'src/A.java').read_text())
            return _result(command, 'failed', detail='Provider disconnected', error_code='agent_failed')

        result = self.invoke(model)
        self.assertEqual('failed', result.status)
        self.assertEqual('not_integrated', result.outputs['diagnostic_repair_integration_status'])
        self.assertEqual('applied', result.outputs['diagnostic_repair_receipts'][0]['status'])
        self.assertEqual(self.repair_refs, result.outputs['diagnostic_repair_refs'])
        self.assertEqual(self.before, (self.source / 'src/A.java').read_text())
        self.assertEqual(self.base, git(self.source, 'rev-parse', 'HEAD'))

    def test_same_execution_recovery_preserves_later_edits_and_original_receipts(self):
        def interrupted(handler, command):
            workspace = self.root / command.options['workspace']
            (workspace / 'src/A.java').write_text(self.after.rstrip() + ' // later cleanup\n')
            return _result(command, 'failed', error_code='agent_failed')

        failed = self.invoke(interrupted)
        original_receipts = failed.outputs['diagnostic_repair_receipts']

        def resumed(handler, command):
            workspace = self.root / command.options['workspace']
            self.assertIn('// later cleanup', (workspace / 'src/A.java').read_text())
            self.assertIn('"status": "applied"', handler.prompt)
            return _result(command, 'completed')

        with patch('modport.diagnostic_repairs.apply', side_effect=AssertionError('must not reapply')):
            result = self.invoke(resumed)
        self.assertEqual('completed', result.status, result.detail)
        self.assertEqual(original_receipts, result.outputs['diagnostic_repair_receipts'])
        self.assertIn('// later cleanup', (self.source / 'src/A.java').read_text())

    def test_stale_repair_is_advisory_and_preserves_current_source(self):
        current = 'class A { void call() { newer(); } }\n'
        (self.source / 'src/A.java').write_text(current)
        git(self.source, 'add', '.')
        git(self.source, 'commit', '-m', 'Independent coder correction')

        def model(handler, command):
            workspace = self.root / command.options['workspace']
            self.assertEqual(current, (workspace / 'src/A.java').read_text())
            self.assertIn('"status": "conflict"', handler.prompt)
            return _result(command, 'completed')

        result = self.invoke(model)
        self.assertEqual('completed', result.status, result.detail)
        self.assertEqual('conflict', result.outputs['diagnostic_repair_receipts'][0]['status'])
        self.assertEqual(current, (self.source / 'src/A.java').read_text())

    def test_missing_recovery_receipt_preserves_clone_and_does_not_block_cleanup(self):
        later = self.after.rstrip() + ' // retained after interruption\n'

        def interrupted(handler, command):
            workspace = self.root / command.options['workspace']
            (workspace / 'src/A.java').write_text(later)
            return _result(command, 'failed', error_code='agent_failed')

        self.invoke(interrupted)
        receipt_path = (self.root / 'artifacts/executions' / self.command.command_id
                        / 'diagnostic-repair-application.json')
        receipt_path.unlink()

        def resumed(handler, command):
            workspace = self.root / command.options['workspace']
            self.assertEqual(later, (workspace / 'src/A.java').read_text())
            self.assertIn('"status": "unknown"', handler.prompt)
            return _result(command, 'completed')

        with patch('modport.diagnostic_repairs.apply', side_effect=AssertionError('must not reapply')):
            result = self.invoke(resumed)
        self.assertEqual('completed', result.status, result.detail)
        self.assertEqual('unknown', result.outputs['diagnostic_repair_receipts'][0]['status'])
        self.assertTrue(any('without reapplication' in diagnostic
                            for diagnostic in result.outputs['business_diagnostics']))
        self.assertEqual(later, (self.source / 'src/A.java').read_text())

    def test_frozen_previous_workflow_keeps_existing_cleanup_behavior(self):
        self.command = replace(self.command, options={'workflow_version': 39})

        def model(handler, command):
            workspace = self.root / command.options['workspace']
            self.assertEqual(self.before, (workspace / 'src/A.java').read_text())
            return _result(command, 'completed')

        with patch('modport.diagnostic_repairs.apply', side_effect=AssertionError('v39 must not apply')):
            result = self.invoke(model)
        self.assertEqual('completed', result.status, result.detail)
        self.assertNotIn('diagnostic_repair_receipts', result.outputs)
        self.assertEqual(self.before, (self.source / 'src/A.java').read_text())


if __name__ == '__main__':
    unittest.main()
