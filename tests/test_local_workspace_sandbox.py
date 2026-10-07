import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from modport.local_workspace_sandbox import (
    _exclusive_output, _synthetic_build_git, convert_workspace_paths, create_windows_stage, linux_workspace_masks,
    prepare_external_build, sensitive_name,
    sync_windows_stage, workspace_inventory, workspace_sensitive_permissions,
)
from modport.windows_build import build_command
from modport.windows_sandbox import SandboxMount, WindowsSandboxSpec, _grant_objects


class LocalWorkspaceSandboxTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.source = self.root / 'selected-project'
        self.source.mkdir()
        self.run = self.root / 'run'
        self.run.mkdir()

    def fixture(self):
        (self.source / 'nested').mkdir()
        (self.source / 'nested' / '.AWS').mkdir()
        (self.source / 'nested' / '.AWS' / 'secret').write_text('private')
        (self.source / '.git').mkdir()
        (self.source / '.git' / 'config').write_text('private repository credentials')
        (self.source / '.env').write_text('private environment')
        (self.source / '.env.example').write_text('example=placeholder')
        (self.source / 'build').mkdir()
        (self.source / 'build' / 'result.jar').write_bytes(b'build output')
        (self.source / '.modport').mkdir()
        (self.source / '.modport' / 'functional-contract.json').write_text('{}')
        (self.source / 'source.java').write_text('original')

    def test_inventory_does_not_visit_excluded_trees_and_preserves_build_protocol(self):
        self.fixture()
        from modport.local_workspace_sandbox import file_os
        original = file_os.scandir
        visited = []
        def scan(directory):
            if os.name != 'nt':
                visited.append(os.readlink('/proc/self/fd/' + str(directory)))
            else:
                visited.append(str(directory.path))
            return original(directory)
        with patch.object(file_os, 'scandir', side_effect=scan):
            visible, excluded = workspace_inventory(self.source)
        self.assertEqual(set(excluded), {Path('.git'), Path('.env'), Path('nested/.AWS')})
        self.assertIn(Path('build/result.jar'), visible)
        self.assertIn(Path('.modport/functional-contract.json'), visible)
        self.assertIn(Path('.env.example'), visible)
        self.assertFalse(any(value.endswith(('.git', '.AWS')) for value in visited))

    def test_linux_masks_sensitive_files_and_directories_readonly(self):
        self.fixture()
        with patch('modport.local_workspace_sandbox._workspace_spec', return_value={
                'mode': 'direct', 'path': str(self.source), 'original_path': str(self.source)}):
            args = linux_workspace_masks(self.run, self.source)
        triples = [args[index:index + 3] for index in range(0, len(args), 3)]
        self.assertEqual({item[2] for item in triples},
                         {'/workspace/.git', '/workspace/.env', '/workspace/nested/.AWS'})
        self.assertTrue(all(item[0] == '--ro-bind' for item in triples))
        for _, source, target in triples:
            source = Path(source)
            if target == '/workspace/.env':
                self.assertEqual(source.read_bytes(), b'')
            else:
                self.assertEqual(list(source.iterdir()), [])

    def test_legacy_workspace_does_not_acquire_local_masks(self):
        with patch('modport.local_workspace_sandbox._workspace_spec', return_value=None):
            self.assertEqual(linux_workspace_masks(self.run, self.source), [])

    def test_visible_links_special_files_and_inventory_limits_fail_closed(self):
        if os.name != 'nt':
            (self.source / 'link').symlink_to(self.root)
            with self.assertRaisesRegex(ValueError, 'unsafe workspace'):
                workspace_inventory(self.source)
            (self.source / 'link').unlink()
            os.mkfifo(self.source / 'fifo')
            with self.assertRaisesRegex(ValueError, 'unsafe workspace'):
                workspace_inventory(self.source)
            (self.source / 'fifo').unlink()
        (self.source / 'one').write_text('one')
        os.link(self.source / 'one', self.source / 'two')
        with self.assertRaisesRegex(ValueError, 'hard-link'):
            workspace_inventory(self.source)
        (self.source / 'two').unlink()
        with patch('modport.local_workspace_sandbox.MAX_ENTRIES', 0):
            with self.assertRaisesRegex(ValueError, 'bounded'):
                workspace_inventory(self.source)

    def test_opencode_rules_deny_actual_mixed_case_secrets_and_keep_examples(self):
        self.fixture()
        rules = workspace_sensitive_permissions(self.source)
        self.assertEqual(rules['read']['nested/.AWS/**'], 'deny')
        self.assertEqual(rules['edit'][str(self.source / '.git')], 'deny')
        self.assertEqual(rules['read']['**/.env.example'], 'allow')
        self.assertTrue(sensitive_name('Credentials.JSON'))
        self.assertFalse(sensitive_name('.modport'))

    def test_native_launcher_uses_trusted_staging_instead_of_original_project_grant(self):
        self.fixture()
        spec = {'mode': 'direct', 'path': str(self.source), 'original_path': str(self.source)}
        with patch('modport.local_workspace_sandbox._workspace_spec', return_value=spec), \
                patch('modport.local_workspace_sandbox._build_staged_command',
                      return_value=['python', '--spec', str(self.run / 'command.json'), '--timeout', '30']):
            command = build_command(self.run, self.source, ['java', 'Main'],
                                    cache_name='gradle-cache', timeout_seconds=30)
        self.assertIn('local_workspace_sandbox', command[3])
        self.assertNotIn('--spec', command)
        self.assertIn('--request', command)

    def test_generic_linux_preparation_binds_only_filtered_run_contained_stage(self):
        self.fixture()
        import json
        observed = {}
        def build(root, stage, args, options, *, windows):
            observed.update(stage=stage, args=args, options=options, windows=windows)
            self.assertFalse((stage / '.git').exists())
            self.assertFalse((stage / '.env').exists())
            return ['bwrap', '--bind', str(stage), '/workspace', '--', *args]
        spec = {'mode': 'direct', 'path': str(self.source), 'original_path': str(self.source)}
        diagnostics = {}
        with patch('modport.local_workspace_sandbox._workspace_spec', return_value=spec), \
                patch('modport.local_workspace_sandbox._build_staged_command', side_effect=build):
            command = prepare_external_build(self.run, self.source,
                ['cat', str(self.source / 'source.java')], timeout_seconds=30,
                environment={'INPUT': str(self.source / 'source.java')}, wrapper_cache_info=diagnostics,
                _native_windows=False)
        request = json.loads(Path(command[-1]).read_text())
        self.assertTrue(observed['stage'].is_relative_to(self.run))
        self.assertEqual(observed['args'], ['cat', '/workspace/source.java'])
        self.assertEqual(observed['options']['environment']['INPUT'], '/workspace/source.java')
        self.assertIs(observed['options']['wrapper_cache_info'], diagnostics)
        self.assertNotIn(str(self.source), request['argv'])
        # A credential appearing later in the original never enters staging.
        (self.source / 'nested' / '.env.local').write_text('late secret')
        self.assertFalse((observed['stage'] / 'nested' / '.env.local').exists())

    def test_path_conversion_obeys_token_and_separator_boundaries_and_windows_case(self):
        convert = lambda value: convert_workspace_paths(value, r'C:\Project', r'D:\Stage', windows=True)
        self.assertEqual(convert(r'C:\PROJECT\src'), r'D:\Stage\src')
        self.assertEqual(convert('c:/project/src'), r'D:\Stage/src')
        self.assertEqual(convert('-Dinput=c:/PROJECT/src'), r'-Dinput=D:\Stage/src')
        self.assertEqual(convert(r'"C:\Project\src"'), r'"D:\Stage\src"')
        self.assertEqual(convert(r'C:\Project-other\src'), r'C:\Project-other\src')
        self.assertEqual(convert(r'prefixC:\Project\src'), r'prefixC:\Project\src')
        self.assertEqual(convert_workspace_paths('/else/root/project/file', '/root/project', '/workspace'),
                         '/else/root/project/file')
        self.assertEqual(convert_workspace_paths('/root/project-other', '/root/project', '/workspace'),
                         '/root/project-other')

    def test_isolated_git_snapshot_has_working_tree_and_version_tag_without_original_secrets(self):
        environment = dict(os.environ, GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_SYSTEM=os.devnull,
                           GIT_CONFIG_NOSYSTEM='1', GIT_TERMINAL_PROMPT='0')
        def git(*values, env=None, expected=0):
            result = subprocess.run(['git', '-c', 'core.hooksPath=' + os.devnull,
                '-c', 'user.name=Test', '-c', 'user.email=test@localhost', *values],
                cwd=self.source, env=env or environment, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, timeout=15)
            self.assertEqual(result.returncode, expected, result.stderr.decode())
            return result.stdout.decode().strip()
        git('init', '--template=')
        (self.source / '.env').write_text('historical-only-secret')
        (self.source / 'source.java').write_text('allowed source')
        git('add', '--', '.env', 'source.java')
        git('commit', '-m', 'Original source')
        git('tag', 'v1.2.3')
        private = self.run / 'stage'
        stage, _ = create_windows_stage(self.source, private)
        repository = _synthetic_build_git(self.run, self.source, stage, private)
        isolated = dict(environment, GIT_DIR=str(repository), GIT_WORK_TREE=str(stage), GIT_OPTIONAL_LOCKS='0')
        self.assertEqual(git('status', '--porcelain', env=isolated), '')
        self.assertEqual(git('describe', '--tags', env=isolated), 'v1.2.3')
        self.assertEqual(git('rev-list', '--count', '--all', env=isolated), '1')
        self.assertNotIn('.env', git('ls-tree', '--name-only', 'HEAD', env=isolated))
        git('grep', 'historical-only-secret', 'HEAD', env=isolated, expected=1)
        self.assertEqual(git('log', '-1', '--format=%s', env=isolated), 'Isolated ModPort build inputs')
        self.assertEqual(git('log', '-1', '--format=%s'), 'Original source')
        self.assertEqual((self.source / '.env').read_text(), 'historical-only-secret')

    def test_directory_only_additions_and_deletions_are_synchronized(self):
        (self.source / 'remove-empty').mkdir()
        stage, baseline = create_windows_stage(self.source, self.run / 'stage')
        (stage / 'remove-empty').rmdir()
        (stage / 'add-empty').mkdir()
        (stage / 'add-empty' / 'child').mkdir()
        sync_windows_stage(self.source, stage, baseline)
        self.assertFalse((self.source / 'remove-empty').exists())
        self.assertTrue((self.source / 'add-empty' / 'child').is_dir())

    def test_directory_deletion_preserves_new_concurrent_file_before_any_write(self):
        (self.source / 'remove').mkdir()
        (self.source / 'unchanged').write_text('original')
        stage, baseline = create_windows_stage(self.source, self.run / 'stage')
        (stage / 'remove').rmdir()
        (stage / 'unchanged').write_text('build change')
        (self.source / 'remove' / 'concurrent').write_text('user file')
        with self.assertRaisesRegex(ValueError, 'directory entries'):
            sync_windows_stage(self.source, stage, baseline)
        self.assertEqual((self.source / 'remove' / 'concurrent').read_text(), 'user file')
        self.assertEqual((self.source / 'unchanged').read_text(), 'original')

    def test_directory_deletion_preserves_file_created_after_preflight(self):
        (self.source / 'remove').mkdir()
        stage, baseline = create_windows_stage(self.source, self.run / 'stage')
        (stage / 'remove').rmdir()
        from modport.local_workspace_sandbox import _remove_output_directory
        def remove(source, relative):
            (source / relative / 'concurrent').write_text('user file')
            return _remove_output_directory(source, relative)
        with patch('modport.local_workspace_sandbox._remove_output_directory', side_effect=remove):
            with self.assertRaises(OSError):
                sync_windows_stage(self.source, stage, baseline)
        self.assertEqual((self.source / 'remove' / 'concurrent').read_text(), 'user file')

    def test_staging_drops_credentials_and_syncs_changes_new_outputs_and_deletions(self):
        self.fixture()
        stage, baseline = create_windows_stage(self.source, self.run / 'stage')
        self.assertFalse((stage / '.git').exists())
        self.assertFalse((stage / 'nested' / '.AWS').exists())
        (stage / 'source.java').write_text('built repair')
        (stage / 'build' / 'result.jar').unlink()
        (stage / 'build' / 'fresh').mkdir()
        (stage / 'build' / 'fresh' / 'result.jar').write_bytes(b'new output')
        (stage / '.env').write_text('generated private data')
        sync_windows_stage(self.source, stage, baseline)
        self.assertEqual((self.source / 'source.java').read_text(), 'built repair')
        self.assertFalse((self.source / 'build' / 'result.jar').exists())
        self.assertEqual((self.source / 'build' / 'fresh' / 'result.jar').read_bytes(), b'new output')
        self.assertEqual((self.source / '.env').read_text(), 'private environment')
        self.assertEqual((self.source / '.git' / 'config').read_text(), 'private repository credentials')

    def test_sync_conflict_preserves_all_originals_before_any_write(self):
        self.fixture()
        stage, baseline = create_windows_stage(self.source, self.run / 'stage')
        (stage / 'source.java').write_text('staged')
        (stage / 'build' / 'result.jar').write_text('staged output')
        (self.source / 'source.java').write_text('concurrent user edit')
        with self.assertRaisesRegex(ValueError, 'concurrent workspace edit'):
            sync_windows_stage(self.source, stage, baseline)
        self.assertEqual((self.source / 'source.java').read_text(), 'concurrent user edit')
        self.assertEqual((self.source / 'build' / 'result.jar').read_bytes(), b'build output')

    def test_source_edit_while_reading_stage_output_is_detected_before_write(self):
        (self.source / 'source.java').write_text('original')
        stage, baseline = create_windows_stage(self.source, self.run / 'stage')
        (stage / 'source.java').write_text('staged output')
        from modport.local_workspace_sandbox import _read_file
        def read(root, relative, **options):
            result = _read_file(root, relative, **options)
            if root == stage:
                (self.source / relative).write_text('edit during stage read')
            return result
        with patch('modport.local_workspace_sandbox._read_file', side_effect=read):
            with self.assertRaisesRegex(ValueError, 'concurrent workspace edit'):
                sync_windows_stage(self.source, stage, baseline)
        self.assertEqual((self.source / 'source.java').read_text(), 'edit during stage read')

    @unittest.skipIf(os.name == 'nt', 'portable symlink fixture')
    def test_unsafe_generated_output_is_not_synchronized(self):
        self.fixture()
        stage, baseline = create_windows_stage(self.source, self.run / 'stage')
        (stage / 'build' / 'leak').symlink_to(self.source / '.env')
        with self.assertRaisesRegex(ValueError, 'unsafe workspace'):
            sync_windows_stage(self.source, stage, baseline)
        self.assertFalse((self.source / 'build' / 'leak').exists())

    def test_windows_grants_reject_unfiltered_tree_before_any_acl_mutation(self):
        self.fixture()
        spec = WindowsSandboxSpec([sys.executable], self.source, self.run / 'native',
            [SandboxMount(self.source, '/workspace', False, credential_filtered=True)])
        with self.assertRaisesRegex(RuntimeError, 'excluded entries'):
            _grant_objects(spec)
        stage, _ = create_windows_stage(self.source, self.run / 'stage')
        clean = WindowsSandboxSpec([sys.executable], stage, self.run / 'native',
            [SandboxMount(stage, '/workspace', False, credential_filtered=True)])
        grants = dict(_grant_objects(clean))
        self.assertIn(stage / 'build' / 'result.jar', grants)
        self.assertFalse(any(sensitive_name(path.name) for path in grants))

    @unittest.skipIf(os.name == 'nt', 'portable native API construction probe')
    def test_native_output_handle_excludes_concurrent_writers_and_deleters(self):
        target = self.source / 'result'
        target.write_bytes(b'original')
        calls = []
        def create(path, access, sharing, security, disposition, flags, template):
            calls.append((access, sharing, disposition, flags))
            return os.open(path, os.O_RDWR)
        def information(handle, pointer):
            pointer._obj.attributes = 0
            pointer._obj.links = 1
            return True
        api = SimpleNamespace(CreateFileW=create, GetFileInformationByHandle=information,
                              CloseHandle=os.close)
        bridge = SimpleNamespace(open_osfhandle=lambda handle, flags: handle)
        with patch.dict(sys.modules, {'msvcrt': bridge}), \
                patch('modport.local_workspace_sandbox.os.name', 'nt'), \
                patch('modport.platform_files.pinned_windows_path'), \
                patch('modport.windows_process.kernel32', return_value=api):
            # os.O_BINARY is native-only; use zero in this construction probe.
            with patch.object(os, 'O_BINARY', 0, create=True):
                with _exclusive_output(self.source, Path('result'), delete=True) as descriptor:
                    self.assertEqual(os.read(descriptor, 8), b'original')
        self.assertEqual(calls[0][1], 1)  # FILE_SHARE_READ, no write/delete sharing.
        self.assertTrue(calls[0][0] & 0x10000)  # DELETE on the pinned object.
        self.assertTrue(calls[0][3] & 0x00200000)  # Open reparse object itself.


if __name__ == '__main__':
    unittest.main()
