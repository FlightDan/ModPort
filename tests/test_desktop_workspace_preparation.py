"""Real filesystem evidence for developer workspaces and sterile Git commands."""
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from modport.desktop_local_source import inspect_local_source, prepare_local_workspace


@unittest.skipUnless(shutil.which('git'), 'Git required for source snapshots')
class DesktopWorkspacePreparationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.source = self.root / '源码项目'
        self.source.mkdir()
        self.application = self.root / 'application'
        self.identity = 'desktop-' + 'b' * 32
        (self.source / 'gradle.properties').write_text('minecraft_version=1.20.1\n')
        (self.source / 'marker.txt').write_bytes(b'original\r\n')

    def git(self, *arguments, cwd=None):
        environment = {key: value for key, value in os.environ.items() if not key.upper().startswith('GIT_')}
        environment.update(GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_SYSTEM=os.devnull,
                           GIT_CONFIG_NOSYSTEM='1', GIT_OPTIONAL_LOCKS='0')
        return subprocess.run(['git', '-c', 'core.hooksPath=' + str(self.root / 'no-hooks'),
            '-c', 'core.fsmonitor=false', '-c', 'core.autocrlf=false',
            '-c', 'filter.attack.clean=', '-c', 'filter.attack.smudge=',
            '-c', 'filter.attack.process=', '-c', 'filter.attack.required=false',
            *arguments], cwd=cwd or self.source, env=environment, check=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE).stdout

    def repository(self):
        self.git('init', '-q', '--initial-branch=original')
        self.git('config', 'user.name', 'Workspace fixture')
        self.git('config', 'user.email', 'workspace@example.invalid')
        self.git('add', '--all')
        self.git('commit', '-qm', 'Original project')

    def inspection(self, source=None):
        return inspect_local_source(str(source or self.source), application_root=self.application)['git']

    def prepare(self, mode, branch=None):
        return prepare_local_workspace(str(self.source), application_root=self.application,
                                       snapshot_id=self.identity, mode=mode, branch=branch)

    def original_files(self):
        return {path.relative_to(self.source).as_posix():
            (path.read_bytes(), path.stat().st_mtime_ns, path.stat().st_mode)
            for path in self.source.rglob('*') if path.is_file()}

    def test_git_worktree_is_real_clean_new_branch_with_source_ancestry(self):
        self.repository()
        head = self.git('rev-parse', 'HEAD').strip()
        index = (self.source / '.git' / 'index').read_bytes()
        original = {name: (self.source / name).read_bytes() for name in ('marker.txt', 'gradle.properties')}
        self.assertTrue(self.inspection()['can_branch'])
        result = self.prepare('git_worktree', 'migration/neoforge')
        workspace = Path(result['workspace']['path'])
        self.assertEqual(workspace.parent, self.source.parent)
        self.assertTrue((workspace / '.git').is_file())
        self.assertEqual(result['workspace']['branch'], 'migration/neoforge')
        self.assertEqual(self.git('branch', '--show-current', cwd=workspace).strip(), b'migration/neoforge')
        self.assertEqual(self.git('rev-parse', 'HEAD', cwd=workspace).decode().strip(), result['source_revision'])
        self.assertEqual(result['source_revision'], head.decode())
        self.assertFalse(result['source_snapshot'])
        self.git('merge-base', '--is-ancestor', head.decode(), result['source_revision'], cwd=workspace)
        self.assertEqual(self.git('status', '--porcelain', cwd=workspace), b'')
        self.assertEqual(self.git('rev-parse', 'HEAD').strip(), head)
        self.assertEqual(self.git('branch', '--show-current').strip(), b'original')
        self.assertEqual((self.source / '.git' / 'index').read_bytes(), index)
        for name, data in original.items():
            self.assertEqual((self.source / name).read_bytes(), data)
            self.assertEqual((workspace / name).read_bytes(), data)
        (workspace / 'marker.txt').write_text('migration edit\n')
        self.assertEqual((self.source / 'marker.txt').read_bytes(), original['marker.txt'])

    def test_linked_worktree_is_detected_as_exact_repository_root(self):
        self.repository()
        linked = self.root / 'linked'
        self.git('worktree', 'add', '-q', '-b', 'linked-source', str(linked))
        value = self.inspection(linked)
        self.assertTrue(value['can_branch'], value['reason'])
        self.assertEqual(value['root'], str(linked))
        self.assertEqual(value['branch'], 'linked-source')
        result = prepare_local_workspace(str(linked), application_root=self.application,
            snapshot_id=self.identity, mode='git_worktree', branch='migration/linked')
        workspace = Path(result['workspace']['path'])
        self.assertEqual(self.git('status', '--porcelain', cwd=workspace), b'')
        self.assertEqual(self.git('branch', '--show-current', cwd=linked).strip(), b'linked-source')

    def test_dirty_git_disables_branch_but_copy_preserves_current_bytes(self):
        self.repository()
        (self.source / 'marker.txt').write_text('dirty current content\n')
        (self.source / 'untracked.txt').write_text('new source\n')
        value = self.inspection()
        self.assertTrue(value['dirty'])
        self.assertFalse(value['can_branch'])
        with self.assertRaisesRegex(ValueError, '未提交'):
            self.prepare('git_worktree', 'migration/dirty')
        result = self.prepare('copy')
        workspace = Path(result['workspace']['path'])
        self.assertEqual((workspace / 'marker.txt').read_text(), 'dirty current content\n')
        self.assertTrue((workspace / 'untracked.txt').is_file())
        self.assertFalse((workspace / '.git').exists())

    def test_no_git_project_copy_and_direct_keep_original_untouched(self):
        value = self.inspection()
        self.assertTrue(value['available'])
        self.assertFalse(value['is_repository'])
        original = self.original_files()
        copied = self.prepare('copy')
        workspace = Path(copied['workspace']['path'])
        self.assertEqual(workspace.parent, self.source.parent)
        self.assertFalse((workspace / '.git').exists())
        self.assertEqual(self.original_files(), original)
        self.identity = 'desktop-' + 'c' * 32
        direct = self.prepare('direct')
        self.assertEqual(direct['workspace']['path'], str(self.source))
        self.assertFalse((self.source / '.git').exists())
        self.assertEqual(self.original_files(), original)
        (self.source / 'marker.txt').write_text('later edit')
        self.assertEqual((Path(direct['snapshot_path']) / 'marker.txt').read_bytes(), b'original\r\n')

    def test_direct_does_not_change_original_git_metadata_or_dirty_index(self):
        self.repository()
        (self.source / 'marker.txt').write_text('staged user edit\n')
        self.git('add', 'marker.txt')
        (self.source / 'marker.txt').write_text('unstaged user edit\n')
        original = self.original_files()
        result = self.prepare('direct')
        self.assertEqual(self.original_files(), original)
        self.assertEqual(result['workspace']['path'], str(self.source))
        self.assertEqual((Path(result['snapshot_path']) / 'marker.txt').read_text(), 'unstaged user edit\n')

    def test_existing_or_invalid_branch_is_rejected_before_snapshot(self):
        self.repository()
        for branch in ('original', 'bad..branch', '-option', '@{-1}', 'HEAD', ' space '):
            with self.subTest(branch=branch), self.assertRaises(ValueError):
                self.prepare('git_worktree', branch)
            self.assertFalse((self.application / 'source-snapshots' / self.identity).exists())
        self.assertEqual(self.git('branch', '--show-current').strip(), b'original')

    def test_unborn_invalid_nested_and_subdirectory_disable_branch(self):
        self.git('init', '-q', '--initial-branch=original')
        unborn = self.inspection()
        self.assertTrue(unborn['is_repository'])
        self.assertFalse(unborn['has_commits'])
        self.assertFalse(unborn['can_branch'])
        shutil.rmtree(self.source / '.git')
        (self.source / '.git').mkdir()
        invalid = self.inspection()
        self.assertFalse(invalid['can_branch'])
        self.assertIn('无效', invalid['reason'])
        shutil.rmtree(self.source / '.git')
        self.repository()
        nested = self.source / 'nested'
        nested.mkdir()
        self.git('init', '-q', cwd=nested)
        self.assertFalse(self.inspection()['can_branch'])
        self.assertIn('嵌套', self.inspection()['reason'])
        shutil.rmtree(nested / '.git')
        value = self.inspection(nested)
        self.assertFalse(value['can_branch'])
        self.assertEqual(value['root'], str(self.source))
        self.assertIn('根目录', value['reason'])

    def test_hostile_hooks_filters_fsmonitor_and_inherited_git_are_not_executed(self):
        self.repository()
        flag = self.root / 'executed'
        script = self.root / 'attack.sh'
        script.write_text('#!/bin/sh\ntouch "' + str(flag) + '"\ncat\n')
        script.chmod(0o755)
        hooks = self.root / 'hooks'
        hooks.mkdir()
        for name in ('post-checkout', 'reference-transaction', 'post-index-change'):
            shutil.copyfile(script, hooks / name)
            (hooks / name).chmod(0o755)
        self.git('config', 'core.hooksPath', str(hooks))
        self.git('config', 'core.fsmonitor', str(script))
        self.git('config', 'filter.attack.clean', str(script))
        self.git('config', 'filter.attack.smudge', str(script))
        self.git('config', 'filter.attack.required', 'true')
        self.git('config', 'alias.status', '!' + str(script))
        self.git('config', 'core.sshCommand', str(script))
        self.git('config', 'url.ssh://hostile/.insteadOf', str(self.root))
        (self.source / '.gitattributes').write_text('marker.txt filter=attack\n')
        self.git('add', '.gitattributes')
        self.git('commit', '-qm', 'Hostile fixture config')
        before_index = (self.source / '.git' / 'index').read_bytes()
        injected_index = self.root / 'injected-index'
        with patch.dict(os.environ, {'GIT_INDEX_FILE': str(injected_index),
                'GIT_CONFIG_COUNT': '1', 'GIT_CONFIG_KEY_0': 'core.fsmonitor',
                'GIT_CONFIG_VALUE_0': str(script), 'GIT_WORK_TREE': str(self.root)}):
            inspected = self.inspection()
            self.assertTrue(inspected['can_branch'], inspected['reason'])
            result = self.prepare('git_worktree', 'migration/safe')
        self.assertFalse(flag.exists())
        self.assertFalse(injected_index.exists())
        self.assertEqual((self.source / '.git' / 'index').read_bytes(), before_index)
        self.assertEqual((Path(result['workspace']['path']) / 'marker.txt').read_bytes(), b'original\r\n')

    def test_copy_excludes_credentials_and_does_not_overwrite_existing_folder(self):
        (self.source / '.env').write_text('credential fixture')
        (self.source / '.env.example').write_text('EXAMPLE=\n')
        (self.source / 'build').mkdir()
        (self.source / 'build' / 'old.jar').write_text('generated fixture')
        existing = self.root / 'already-exists'
        existing.mkdir()
        (existing / 'keep.txt').write_text('user file')
        with patch('modport.desktop_local_source._new_workspace_path', return_value=existing), \
                self.assertRaisesRegex(ValueError, '不会覆盖'):
            self.prepare('copy')
        self.assertEqual((existing / 'keep.txt').read_text(), 'user file')
        result = self.prepare('copy')
        workspace = Path(result['workspace']['path'])
        self.assertFalse((workspace / '.env').exists())
        self.assertFalse((workspace / 'build').exists())
        self.assertTrue((workspace / '.env.example').exists())

    def test_git_worktree_builtin_eol_encoding_and_autocrlf_keep_ordinary_status_clean(self):
        (self.source / '.gitattributes').write_text('marker.txt text eol=crlf\nencoding.txt text working-tree-encoding=UTF-16\n')
        (self.source / 'encoding.txt').write_bytes('encoded source\n'.encode('utf-16'))
        self.repository()
        self.git('config', 'core.autocrlf', 'true')
        environment = {**os.environ, 'GIT_CONFIG_GLOBAL': os.devnull, 'GIT_CONFIG_NOSYSTEM': '1',
                       'GIT_OPTIONAL_LOCKS': '0'}
        original_status = subprocess.run(['git', 'status', '--porcelain'], cwd=self.source,
            env=environment, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE).stdout
        self.assertEqual(original_status, b'')
        inspection = self.inspection()
        self.assertTrue(inspection['can_branch'], inspection['reason'])
        index = (self.source / '.git' / 'index').read_bytes()
        config = (self.source / '.git' / 'config').read_bytes()
        attributes = self.source / '.git' / 'info' / 'attributes'
        self.assertFalse(attributes.exists())
        result = self.prepare('git_worktree', 'migration/encoding')
        workspace = Path(result['workspace']['path'])
        status = subprocess.run(['git', 'status', '--porcelain'], cwd=workspace,
            env=environment, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE).stdout
        self.assertEqual(status, b'')
        self.assertFalse(result['source_snapshot'])
        self.assertEqual((workspace / 'marker.txt').read_bytes(), (self.source / 'marker.txt').read_bytes())
        self.assertEqual((workspace / 'encoding.txt').read_bytes(), (self.source / 'encoding.txt').read_bytes())
        self.assertEqual((self.source / '.git' / 'index').read_bytes(), index)
        self.assertEqual((self.source / '.git' / 'config').read_bytes(), config)
        self.assertFalse(attributes.exists())

    def test_git_worktree_excludes_unrelated_ignored_files(self):
        (self.source / '.gitignore').write_text('ignored.txt\n')
        self.repository()
        (self.source / 'ignored.txt').write_text('unrelated local file')
        result = self.prepare('git_worktree', 'migration/tracked')
        workspace = Path(result['workspace']['path'])
        self.assertFalse((workspace / 'ignored.txt').exists())
        self.assertEqual(self.git('status', '--porcelain', cwd=workspace), b'')
        self.assertEqual((self.source / 'ignored.txt').read_text(), 'unrelated local file')

    def test_tracked_excluded_credentials_disable_git_mode_and_copy_still_sanitizes(self):
        (self.source / '.env').write_text('tracked credential fixture')
        self.repository()
        inspection = self.inspection()
        self.assertFalse(inspection['can_branch'])
        self.assertIn('.env', inspection['reason'])
        self.assertIn('复制', inspection['reason'])
        with self.assertRaises(ValueError):
            self.prepare('git_worktree', 'migration/secret')
        result = self.prepare('copy')
        self.assertTrue(result['source_snapshot'])
        self.assertFalse((Path(result['workspace']['path']) / '.env').exists())
        self.assertTrue((self.source / '.env').exists())

    def test_failed_workspace_copy_keeps_snapshot_and_removes_owned_branch_and_folder(self):
        from modport.desktop_local_source import _copy_tree as original_copy
        self.repository()
        destination = self.root / 'new-workspace'
        def fail_workspace(source, target, **options):
            if target == destination:
                raise ValueError('simulated workspace copy failure')
            return original_copy(source, target, **options)
        with patch('modport.desktop_local_source._new_workspace_path', return_value=destination), \
                patch('modport.desktop_local_source._copy_tree', side_effect=fail_workspace), \
                self.assertRaisesRegex(ValueError, 'simulated workspace'):
            self.prepare('git_worktree', 'migration/failure')
        self.assertFalse(destination.exists())
        self.assertTrue((self.application / 'source-snapshots' / self.identity).is_dir())
        self.assertNotIn(b'migration/failure', self.git('branch', '--list'))
        self.assertEqual(self.git('branch', '--show-current').strip(), b'original')

    def test_missing_git_capability_is_actionable_and_does_not_probe(self):
        with patch('modport.desktop_local_source.shutil.which', return_value=None):
            value = self.inspection()
        self.assertFalse(value['available'])
        self.assertFalse(value['can_branch'])
        self.assertIn('Git', value['reason'])


if __name__ == '__main__':
    unittest.main()
