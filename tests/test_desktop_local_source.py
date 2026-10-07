"""Local snapshots use current files without executing or changing the source."""
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from modport.desktop_local_source import inspect_local_source, prepare_local_source


@unittest.skipUnless(shutil.which('git'), 'Git required for local source snapshots')
class DesktopLocalSourceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.source = self.root / '本地项目'
        self.source.mkdir()
        self.application = self.root / 'application'
        self.identity = 'desktop-' + 'a' * 32

    def prepare(self):
        return prepare_local_source(str(self.source), application_root=self.application, snapshot_id=self.identity)

    def git(self, *arguments, cwd=None):
        environment = {**os.environ, 'GIT_CONFIG_GLOBAL': os.devnull, 'GIT_CONFIG_NOSYSTEM': '1'}
        return subprocess.run(['git', *arguments], cwd=cwd or self.source, env=environment,
                              check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE).stdout

    def test_unicode_folder_without_git_inspection_and_frozen_snapshot(self):
        (self.source / 'gradle.properties').write_text('minecraft_version=1.20.1\nmod_id=sample\n')
        (self.source / '当前文件.txt').write_text('尚未提交的内容\n', encoding='utf-8')
        inspection = inspect_local_source(str(self.source), application_root=self.application)
        self.assertEqual(inspection['name'], '本地项目')
        self.assertIn('gradle.properties', inspection['documents'])
        self.assertFalse((self.source / '.git').exists())
        original = {path.name: (path.read_bytes(), path.stat().st_mtime_ns, path.stat().st_mode)
                    for path in self.source.iterdir()}
        result = self.prepare()
        snapshot = Path(result['snapshot_path'])
        self.assertEqual(result['source_repository'], snapshot.as_uri())
        self.assertEqual(result['file_count'], 2)
        self.assertEqual(self.git('rev-parse', 'HEAD', cwd=snapshot).decode().strip(), result['source_revision'])
        for name, before in original.items():
            actual = self.source / name
            self.assertEqual((actual.read_bytes(), actual.stat().st_mtime_ns, actual.stat().st_mode), before)
        self.assertFalse((self.source / '.git').exists())
        (self.source / '当前文件.txt').write_text('后来修改的内容\n', encoding='utf-8')
        self.assertEqual((snapshot / '当前文件.txt').read_text(), '尚未提交的内容\n')
        with self.assertRaises(ValueError): self.prepare()
        self.assertEqual((snapshot / '当前文件.txt').read_text(), '尚未提交的内容\n')

    def test_dirty_git_ignored_files_executable_and_source_attributes_are_preserved(self):
        self.git('init', '-q')
        self.git('config', 'user.name', 'Local fixture')
        self.git('config', 'user.email', 'fixture@example.invalid')
        (self.source / 'tracked.txt').write_text('committed\n')
        self.git('add', 'tracked.txt')
        self.git('commit', '-qm', 'fixture')
        original_head = self.git('rev-parse', 'HEAD')
        (self.source / 'tracked.txt').write_text('current dirty content\n')
        (self.source / '.gitignore').write_text('ignored.txt\n')
        (self.source / 'ignored.txt').write_text('current ignored content\n')
        (self.source / 'gradlew').write_bytes(b'#!/bin/sh\r\necho do-not-execute\r\n')
        (self.source / 'gradlew').chmod(0o755)
        # Selected .gitattributes must not normalize the snapshot's current bytes.
        (self.source / '.gitattributes').write_text('* text=auto\n')
        status = self.git('status', '--porcelain=v1', '--untracked-files=all')
        result = self.prepare()
        snapshot = Path(result['snapshot_path'])
        self.assertEqual((snapshot / 'tracked.txt').read_text(), 'current dirty content\n')
        self.assertEqual((snapshot / 'ignored.txt').read_text(), 'current ignored content\n')
        self.assertEqual(self.git('show', 'HEAD:gradlew', cwd=snapshot), (self.source / 'gradlew').read_bytes())
        if os.name != 'nt':
            self.assertTrue((snapshot / 'gradlew').stat().st_mode & stat.S_IXUSR)
            self.assertTrue(self.git('ls-files', '--stage', 'gradlew', cwd=snapshot).startswith(b'100755'))
        self.assertEqual(self.git('rev-parse', 'HEAD'), original_head)
        self.assertEqual(self.git('status', '--porcelain=v1', '--untracked-files=all'), status)

    def test_named_exclusions_do_not_read_sensitive_or_generated_subtrees(self):
        (self.source / 'build.gradle').write_text('// source\n')
        for name in ('.git', '.gradle', 'build', 'out', 'node_modules', '.aws', '.ssh', '.codex', 'host-runtime', 'model-settings'):
            folder = self.source / name
            folder.mkdir()
            (folder / 'marker').write_text('excluded fixture')
        for name in ('.env', '.env.local', 'credentials.json', 'auth.json', '.git-credentials', 'kernel.sqlite3'):
            (self.source / name).write_text('excluded fixture')
        (self.source / '.env.example').write_text('EXAMPLE=\n')
        nested = self.source / 'src'
        nested.mkdir()
        (nested / '.git').mkdir()
        (nested / '.git' / 'marker').write_text('excluded nested fixture')
        result = self.prepare()
        snapshot = Path(result['snapshot_path'])
        selected = self.git('ls-files', cwd=snapshot).decode().splitlines()
        self.assertEqual(set(selected), {'.env.example', 'build.gradle'})
        self.assertEqual(result['excluded_count'], 17)
        self.assertFalse((snapshot / 'src' / '.git').exists())
        self.assertEqual((self.source / 'credentials.json').read_text(), 'excluded fixture')

    def test_common_generated_directories_are_excluded_but_same_named_files_are_kept(self):
        for name in ('run', 'runs', 'logs', '.venv', 'venv', '__pycache__', '.cache', 'target', 'dist'):
            folder = self.source / name
            folder.mkdir()
            (folder / 'marker.txt').write_text('generated fixture')
        (self.source / 'src').mkdir()
        (self.source / 'src' / 'target').write_text('source file named target')
        snapshot = self.prepare()
        self.assertEqual(snapshot['excluded_count'], 9)
        selected = self.git('ls-files', cwd=Path(snapshot['snapshot_path'])).decode().splitlines()
        self.assertEqual(selected, ['src/target'])

    def test_links_and_nonregular_files_fail_without_publishing_or_touching_original(self):
        if os.name == 'nt': self.skipTest('POSIX fixture creation; native Windows reparse checks require a Windows host')
        (self.source / 'source.txt').write_text('source fixture')
        outside = self.root / 'outside.txt'
        outside.write_text('outside fixture')
        link = self.source / 'escape.txt'
        link.symlink_to(outside)
        with self.assertRaises((OSError, ValueError)): self.prepare()
        self.assertEqual(outside.read_text(), 'outside fixture')
        self.assertFalse((self.application / 'source-snapshots' / self.identity).exists())
        self.assertFalse(list((self.application / 'source-snapshots').glob('.staging-*')))
        link.unlink()
        os.mkfifo(self.source / 'pipe')
        with self.assertRaises((OSError, ValueError)): self.prepare()
        self.assertTrue(stat.S_ISFIFO((self.source / 'pipe').stat().st_mode))

    def test_metadata_fifo_is_rejected_without_waiting_for_a_writer(self):
        if os.name == 'nt': self.skipTest('POSIX FIFO fixture')
        os.mkfifo(self.source / 'gradle.properties')
        with self.assertRaises((OSError, ValueError)):
            inspect_local_source(str(self.source), application_root=self.application)

    def test_file_replaced_by_fifo_before_open_is_rejected_without_waiting(self):
        if os.name == 'nt': self.skipTest('POSIX FIFO fixture')
        from modport.desktop_local_source import safe_open as real_safe_open
        original = self.source / 'source.txt'
        original.write_text('source fixture')
        def replace_before_open(root, relative, *arguments, **options):
            if Path(root) == self.source and Path(relative).as_posix() == 'source.txt':
                original.unlink()
                os.mkfifo(original)
            return real_safe_open(root, relative, *arguments, **options)
        with patch('modport.desktop_local_source.safe_open', side_effect=replace_before_open), self.assertRaises((OSError, ValueError)):
            self.prepare()
        self.assertFalse((self.application / 'source-snapshots' / self.identity).exists())

    def test_overlap_broad_paths_limits_and_metadata_link_are_rejected(self):
        with self.assertRaises(ValueError):
            inspect_local_source(str(self.root), application_root=self.root / 'application')
        with self.assertRaises(ValueError):
            inspect_local_source(str(self.source), application_root=self.source / 'application')
        with self.assertRaises(ValueError):
            inspect_local_source(self.source.anchor, application_root=self.application)
        (self.source / 'too-large.txt').write_bytes(b'12345')
        with patch('modport.desktop_local_source.MAX_FILE_BYTES', 4), self.assertRaises(ValueError): self.prepare()
        self.assertFalse((self.application / 'source-snapshots' / self.identity).exists())
        if os.name != 'nt':
            (self.source / 'gradle.properties').symlink_to(self.source / 'too-large.txt')
            with self.assertRaises((OSError, ValueError)):
                inspect_local_source(str(self.source), application_root=self.application)

    def test_host_git_ignores_inherited_config_and_does_not_run_source_hooks(self):
        from modport.desktop_local_source import capture_process as real_capture_process
        (self.source / 'source.txt').write_text('source fixture')
        home = self.root / 'hostile-home'
        home.mkdir()
        hook = home / 'hooks'
        hook.mkdir()
        (hook / 'pre-commit').write_text('#!/bin/sh\nexit 99\n')
        (hook / 'pre-commit').chmod(0o755)
        config = home / 'config'
        config.write_text('[core]\n hooksPath = ' + str(hook) + '\n[commit]\n gpgsign = true\n')
        system_root = os.environ.get('SYSTEMROOT') or os.environ.get('SystemRoot') or '/fixture/systemroot'
        with patch.dict(os.environ, {'GIT_CONFIG_GLOBAL': str(config), 'GIT_CONFIG_COUNT': '1',
                'GIT_CONFIG_KEY_0': 'core.hooksPath', 'GIT_CONFIG_VALUE_0': str(hook),
                'GIT_INDEX_FILE': str(self.source / 'injected-index'), 'SYSTEMROOT': system_root}), \
                patch('modport.desktop_local_source.capture_process', wraps=real_capture_process) as execute:
            result = self.prepare()
        for call in execute.call_args_list:
            self.assertEqual(call.kwargs['environment']['SYSTEMROOT'], system_root)
            self.assertNotIn('GIT_INDEX_FILE', call.kwargs['environment'])
        self.assertTrue(Path(result['snapshot_path']).is_dir())
        self.assertFalse((self.source / 'injected-index').exists())

    def test_actual_validate_input_handler_consumes_snapshot_after_original_changes(self):
        from modport.contracts import OperationInput
        from modport.handlers import ValidateInputHandler
        from modport.workflow import WORKFLOW_VERSION
        (self.source / 'marker.txt').write_text('frozen current content\n')
        (self.source / 'encoding.txt').write_bytes('current UTF-16 content\n'.encode('utf-16'))
        (self.source / '.gitattributes').write_text('marker.txt text eol=crlf\nencoding.txt working-tree-encoding=UTF-16\n')
        snapshot = self.prepare()
        (self.source / 'marker.txt').write_text('later original content\n')
        run = self.root / 'run'
        (run / 'artifacts').mkdir(parents=True)
        (run / 'artifacts' / 'rubric.json').write_text(json.dumps({'rubric_id': 'local-source-fixture', 'rubric_version': 1}))
        command = OperationInput(run_id='local-source-fixture', task_id='input_validation',
            stage_id='input_validation', command_id='local-source-input', run_dir=str(run),
            payload={'source_repository': snapshot['source_repository'], 'source_revision': snapshot['source_revision'], 'source_snapshot': True},
            options={'workflow_version': WORKFLOW_VERSION},
            artifact_refs={'acceptance_rubric': {'path': 'artifacts/rubric.json'}})
        outcome = ValidateInputHandler()(command)
        self.assertEqual(outcome.status, 'completed', outcome.detail)
        self.assertEqual(outcome.outputs['source_commit'], snapshot['source_revision'])
        self.assertTrue(outcome.outputs['source_snapshot'])
        self.assertEqual((run / 'baseline' / 'marker.txt').read_text(), 'frozen current content\n')
        self.assertEqual((run / 'worktree' / 'marker.txt').read_text(), 'frozen current content\n')
        self.assertEqual((run / 'baseline' / 'marker.txt').read_bytes(), b'frozen current content\n')
        self.assertEqual((run / 'worktree' / 'marker.txt').read_bytes(), b'frozen current content\n')
        expected_encoding = 'current UTF-16 content\n'.encode('utf-16')
        self.assertEqual((run / 'baseline' / 'encoding.txt').read_bytes(), expected_encoding)
        self.assertEqual((run / 'worktree' / 'encoding.txt').read_bytes(), expected_encoding)
        self.assertEqual((self.source / 'marker.txt').read_text(), 'later original content\n')

    def test_ordinary_repository_request_has_no_snapshot_attributes_override(self):
        from modport.contracts import OperationInput
        from modport.handlers import ValidateInputHandler
        from modport.workflow import WORKFLOW_VERSION
        self.git('init', '-q')
        self.git('config', 'user.name', 'Local fixture')
        self.git('config', 'user.email', 'fixture@example.invalid')
        (self.source / 'marker.txt').write_text('ordinary repository\n')
        self.git('add', 'marker.txt')
        self.git('commit', '-qm', 'fixture')
        revision = self.git('rev-parse', 'HEAD').decode().strip()
        run = self.root / 'ordinary-run'
        (run / 'artifacts').mkdir(parents=True)
        (run / 'artifacts' / 'rubric.json').write_text(json.dumps({'rubric_id': 'local-source-fixture', 'rubric_version': 1}))
        operation = OperationInput(run_id='ordinary-fixture', task_id='input_validation',
            stage_id='input_validation', command_id='ordinary-input', run_dir=str(run),
            payload={'source_repository': self.source.as_uri(), 'source_revision': revision},
            options={'workflow_version': WORKFLOW_VERSION},
            artifact_refs={'acceptance_rubric': {'path': 'artifacts/rubric.json'}})
        outcome = ValidateInputHandler()(operation)
        self.assertEqual(outcome.status, 'completed', outcome.detail)
        self.assertNotIn('source_snapshot', outcome.outputs)
        self.assertFalse((run / 'repository.git' / 'info' / 'attributes').exists())


if __name__ == '__main__':
    unittest.main()
