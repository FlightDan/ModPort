import json
from pathlib import Path
import tempfile
import unittest

from modport.independent_tests import _safe_copy
from modport.progress_watchdog import capture_progress
from modport.repair_execution import _install_file
from modport.rework_coder import _reviewer_scope


class ExternalWorkspaceConsumerTests(unittest.TestCase):
    def make_run(self, root, project):
        root.mkdir(parents=True)
        project.mkdir(parents=True)
        (root / 'run.json').write_text(json.dumps({'request': {'local_workspace': {
            'mode': 'direct', 'path': str(project), 'original_path': str(project)}}}))

    def test_reviewer_and_progress_use_logical_registered_workspace(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            root, project = base / 'run', base / 'project'
            self.make_run(root, project)
            (project / 'src').mkdir()
            (project / 'src' / 'Main.java').write_text('class Main {}')
            (project / '.env').write_text('TOKEN=secret')
            with self.assertRaises(ValueError):
                _reviewer_scope(root, project / 'src')
            self.assertEqual(_reviewer_scope(root, Path('worktree')), (project, 'worktree'))

            command = 'assignment-1'
            execution = root / 'artifacts' / 'executions' / command
            execution.mkdir(parents=True)
            (execution / 'artifact-compile-session.json').write_text(json.dumps({
                'command_id': command, 'kind': 'artifact_compile',
                'workspace': str(project),
                'operation': {'options': {'artifact_init_script': 'init.gradle'}}}))
            (execution / 'artifact-compile-result.json').write_text(json.dumps({
                'acceptance_evidence': False,
                'tasks': ['compileJava', 'compileTestJava'], 'exit_code': 0}))
            observed = capture_progress(root, {
                'run_id': 'run', 'task_id': 'coder', 'stage_id': 'development',
                'command_id': command, 'attempt': 1,
                'options': {'workspace': 'worktree'}})
            self.assertIn('worktree/src/Main.java', observed['content'], observed['limitations'])
            self.assertNotIn('worktree/.env', observed['content'])
            self.assertEqual(observed['tool_outcomes']['artifact_harness_compile']['status'], 'passed')

    def test_recursive_copy_excludes_credentials_and_install_stages_at_destination(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            source = base / 'source'
            source.mkdir()
            (source / 'src').mkdir()
            (source / 'src' / 'ok.txt').write_text('ok')
            (source / 'src' / '.env.local').write_text('TOKEN=secret')
            (source / '.git').mkdir()
            (source / '.git' / 'config').write_text('credential=secret')
            copied = base / 'copied'
            _safe_copy(source, copied)
            self.assertTrue((copied / 'src' / 'ok.txt').is_file())
            self.assertFalse((copied / 'src' / '.env.local').exists())
            self.assertFalse((copied / '.git').exists())

            staged = base / 'run-stage' / 'file'
            staged.parent.mkdir()
            _install_file(copied / 'src' / 'ok.txt', staged, 0o640)
            self.assertEqual(staged.read_text(), 'ok')
            self.assertEqual(staged.stat().st_mode & 0o777, 0o640)

    def test_project_files_cannot_supply_a_second_workspace_binding(self):
        from modport.repair_execution import _contained as repair_path
        from modport.independent_tests import _contained as test_path
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            project, outside = base / 'project', base / 'outside'
            self.make_run(project, outside)
            for resolve in (repair_path, test_path):
                self.assertEqual(resolve(project, 'worktree/file'), project / 'worktree/file')
                with self.assertRaises(ValueError):
                    resolve(project, 'nested/D:secret')


if __name__ == '__main__':
    unittest.main()
