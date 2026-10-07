"""Exercise public source selection and actual desktop archive assembly."""
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import sys
import tarfile
import tempfile
import unittest
import zipfile

SCRIPTS = Path(__file__).resolve().parents[1] / 'scripts'
sys.path.insert(0, str(SCRIPTS))
from build_source import PROJECT_FILES, copy_project_sources, copy_source_tree
from build_desktop import assemble, extract_sdk_wheel


def write_zip(path, entries):
    with zipfile.ZipFile(path, 'w') as archive:
        for name, content in entries.items():
            archive.writestr(name, content)


def write_tar(path, entries):
    with tarfile.open(path, 'w:gz') as archive:
        for name, content in entries.items():
            data = content.encode()
            info = tarfile.TarInfo(name)
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))


class ReleasePackagingTests(unittest.TestCase):
    def test_source_selection_excludes_local_state_and_keeps_notices(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / 'checkout'
            for name in PROJECT_FILES:
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text('public file\n')
            files = {
                'src/modport/__init__.py': '# source\n',
                'src/modport/vendor_skills/example/LICENSE': 'vendor license\n',
                'src/modport/.env.json': '{"secret": "fixture"}',
                'src/modport/private.key': 'private fixture',
                'src/modport/.aws/credentials.json': '{}',
                'tests/test_example.py': '# test\n',
                'legacy/runs/private.json': '{}',
                'modport-models.json': '{}',
            }
            for name, content in files.items():
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(content)
            destination = Path(temporary) / 'export'
            copy_project_sources(root, destination)
            copied = {p.relative_to(destination).as_posix() for p in destination.rglob('*') if p.is_file()}
            self.assertIn('src/modport/vendor_skills/example/LICENSE', copied)
            self.assertIn('tests/test_example.py', copied)
            self.assertFalse(any('.env' in p or 'private' in p or '.aws' in p or 'legacy/' in p for p in copied))
            self.assertNotIn('modport-models.json', copied)

    def test_export_rejects_symlinked_source_ancestor(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / 'checkout'
            outside = Path(temporary) / 'outside'
            root.mkdir()
            (outside / 'modport').mkdir(parents=True)
            (outside / 'modport' / '__init__.py').write_text('# outside\n')
            try:
                (root / 'src').symlink_to(outside, target_is_directory=True)
            except OSError:
                self.skipTest('symlink creation unavailable on this platform')
            with self.assertRaisesRegex(ValueError, 'symlink'):
                copy_source_tree(root, 'src/modport', Path(temporary) / 'export')

    def test_desktop_archives_preserve_runtime_licenses_and_corresponding_source(self):
        # Tiny runtime/SDK fixtures exercise assembly only; no native runtime or
        # unfinished real SDK is launched or packaged by this test.
        for platform in ('linux', 'windows'):
            with self.subTest(platform=platform), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary) / 'checkout'
                for name in PROJECT_FILES:
                    path = root / name
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text('ModPort public source\n')
                (root / 'LICENSE').write_text('ModPort AGPL fixture\n')
                (root / 'pyproject.toml').write_text(
                    '[project]\nname = "modport"\nversion = "0.2.9"\n'
                    'requires-python = ">=3.10"\ndependencies = ["dispatcher-sdk==0.7.1"]\n')
                (root / 'desktop/package.json').write_text('{"version": "0.1.9"}')
                for name in ('src/modport', 'tests'):
                    (root / name).mkdir(parents=True)
                (root / 'src/modport/__init__.py').write_text('# ModPort source\n')
                (root / 'src/modport/workflow.py').write_text('WORKFLOW_VERSION = 42\n')
                (root / 'src/modport/.env.json').write_text('{"secret": "fixture"}')
                sdk = Path(temporary) / 'sdk'
                (sdk / 'src/dispatcher_sdk').mkdir(parents=True)
                (sdk / 'src/dispatcher_sdk/__init__.py').write_text('# SDK source\n')
                (sdk / 'src/dispatcher_sdk/py.typed').write_text('')
                (sdk / 'src/dispatcher_sdk/private.key').write_text('private fixture')
                for name in ('pyproject.toml', 'LICENSE', 'NOTICE', 'README.md', 'MANIFEST.in'):
                    (sdk / name).write_text('SDK ' + name)
                (sdk / 'pyproject.toml').write_text('[project]\nname = "dispatcher-sdk"\nversion = "0.7.1"\n')
                for name in ('setup.cfg', 'README.zh-CN.md', 'RELEASING.md', 'PROVENANCE.md', 'SOURCE_MANIFEST.json', 'PKG-INFO'):
                    (sdk / name).write_text('SDK public release ' + name)
                for name in ('docs/public.md', 'examples/use_sdk.py', 'tests/test_sdk.py',
                             'scripts/release.py', 'wiki/Home.md', 'DocsforAgents/README.md',
                             '.github/workflows/test.yml', 'tests/private.key', 'docs/.env.json'):
                    path = sdk / name
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text('SDK public source fixture')
                metadata = ('Metadata-Version: 2.4\nName: dispatcher-sdk\nVersion: 0.7.1\n'
                            'Requires-Python: >=3.10\nProvides-Extra: opensandbox\n'
                            'Requires-Dist: opensandbox==0.1.16; extra == "opensandbox"\n'
                            '\nOfficial release description fixture.\n')
                official_entries = {
                    'dispatcher_sdk/__init__.py': '# SDK official wheel source\n',
                    'dispatcher_sdk/py.typed': '',
                    'dispatcher_sdk-0.7.1.dist-info/METADATA': metadata,
                    'dispatcher_sdk-0.7.1.dist-info/WHEEL': 'Wheel-Version: 1.0\n',
                    'dispatcher_sdk-0.7.1.dist-info/RECORD': 'official release record preserved\n',
                    'dispatcher_sdk-0.7.1.dist-info/licenses/LICENSE': 'Official SDK license fixture',
                }
                staged = root / 'build/sdk-release/0.7.1'
                staged.mkdir(parents=True)
                wheel = staged / 'dispatcher_sdk-0.7.1-py3-none-any.whl'
                write_zip(wheel, official_entries)
                # Exercise both staged default selection and the explicit wheel override.
                supplied_wheel = None
                if platform == 'windows':
                    supplied_wheel = Path(temporary) / 'official-sdk.whl'
                    wheel.rename(supplied_wheel)
                downloads = root / 'build/desktop/downloads'
                downloads.mkdir(parents=True)
                electron = 'electron.exe' if platform == 'windows' else 'electron'
                write_zip(downloads / f'electron-{platform}.zip', {
                    electron: 'runtime fixture', 'LICENSE': 'Electron license fixture',
                    'LICENSES.chromium.html': 'Chromium notices fixture',
                    'chrome-sandbox': '', 'chrome_crashpad_handler': '',
                })
                (downloads / 'opencode-LICENSE.txt').write_text('OpenCode license fixture')
                if platform == 'windows':
                    write_zip(downloads / 'opencode-windows.zip', {'opencode.exe': ''})
                    write_zip(downloads / 'python-windows.zip', {'python.exe': '', 'LICENSE.txt': 'Python license fixture'})
                else:
                    write_tar(downloads / 'opencode-linux.tar.gz', {'opencode': ''})
                    write_tar(downloads / 'python-linux.tar.gz', {
                        'python/lib/python3.13/site-packages/placeholder.py': '',
                        'python/lib/python3.13/LICENSE.txt': 'Python license fixture',
                    })
                destination = Path(temporary) / 'dist'
                with redirect_stdout(io.StringIO()):
                    assemble(platform, root, destination, sdk, supplied_wheel)
                bundle = destination / f'ModPort-{platform}-x64'
                self.assertEqual((bundle / 'LICENSE').read_text(), 'ModPort AGPL fixture\n')
                self.assertEqual((bundle / 'licenses/Electron-LICENSE.txt').read_text(), 'Electron license fixture')
                self.assertTrue((bundle / 'LICENSES.chromium.html').is_file())
                self.assertTrue((bundle / 'source/modport/scripts/build_desktop.py').is_file())
                self.assertTrue((bundle / 'source/dispatcher-sdk/pyproject.toml').is_file())
                for name in ('setup.cfg', 'README.zh-CN.md', 'RELEASING.md', 'PROVENANCE.md',
                             'SOURCE_MANIFEST.json', 'PKG-INFO', 'docs/public.md', 'examples/use_sdk.py',
                             'tests/test_sdk.py', 'scripts/release.py', 'wiki/Home.md',
                             'DocsforAgents/README.md', '.github/workflows/test.yml'):
                    self.assertTrue((bundle / 'source/dispatcher-sdk' / name).is_file(), name)
                self.assertTrue((bundle / 'runtime/dispatcher_sdk/py.typed').is_file())
                for name, content in official_entries.items():
                    self.assertEqual((bundle / 'runtime' / name).read_text(), content)
                self.assertFalse((bundle / 'runtime/dispatcher_sdk-0.7.0.dev2.dist-info').exists())
                self.assertIn('Version: 0.2.9', (bundle / 'runtime/modport-0.2.9.dist-info/METADATA').read_text())
                info = json.loads((bundle / 'build-info.json').read_text())
                self.assertEqual({key: info[key] for key in ('application_version', 'workflow_version', 'modport_version', 'sdk_version')},
                                 {'application_version': '0.1.9', 'workflow_version': 42, 'modport_version': '0.2.9', 'sdk_version': '0.7.1'})
                self.assertEqual((bundle / 'resources/app/i18n.cjs').read_text(), (root / 'desktop/i18n.cjs').read_text())
                self.assertEqual((bundle / 'resources/app/updates.cjs').read_text(), (root / 'desktop/updates.cjs').read_text())
                self.assertFalse(any(p.name in {'.env.json', 'private.key'} for p in bundle.rglob('*')))
                archive = bundle.with_suffix('.zip' if platform == 'windows' else '.tar.gz')
                self.assertTrue(archive.is_file())

    def test_sdk_wheel_rejects_unsafe_entries_and_mismatched_release_metadata(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            wheel = root / 'sdk.whl'
            metadata = 'Metadata-Version: 2.4\nName: dispatcher-sdk\nVersion: 0.7.1\n'
            write_zip(wheel, {'dispatcher_sdk-0.7.1.dist-info/METADATA': metadata,
                              '../outside.py': '# unsafe\n'})
            with self.assertRaisesRegex(ValueError, 'Unsafe SDK wheel entry'):
                extract_sdk_wheel(wheel, root / 'runtime', '0.7.1')
            self.assertFalse((root / 'runtime').exists())
            write_zip(wheel, {'dispatcher_sdk-0.7.1.dist-info/METADATA': metadata})
            with self.assertRaisesRegex(ValueError, 'release version'):
                extract_sdk_wheel(wheel, root / 'runtime', '0.7.2')

    def test_custom_sdk_release_must_match_modport_dependency_pin_before_assembly(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            sdk = root / 'sdk'
            (sdk / 'src/dispatcher_sdk').mkdir(parents=True)
            (sdk / 'pyproject.toml').write_text('[project]\nversion = "0.7.2"\n')
            (root / 'pyproject.toml').write_text(
                '[project]\nname = "modport"\nversion = "0.2.0"\n'
                'dependencies = ["dispatcher-sdk==0.7.1"]\n')
            with self.assertRaisesRegex(ValueError, 'dependency pin'):
                assemble('linux', root, root / 'dist', sdk, root / 'custom-sdk.whl')
            self.assertFalse((root / 'dist').exists())


if __name__ == '__main__':
    unittest.main()
