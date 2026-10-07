"""Assemble portable desktop bundles from official downloaded runtimes.

Run from the current source checkout. No migration evidence or credentials are
included. Runtime downloads are prepared separately; this script does not add
checksum or fingerprint verification.
"""
from __future__ import annotations

import argparse
import ast
from email.parser import BytesParser
import json
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import tarfile
import zipfile

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10 with the documented setuptools build dependency.
    from setuptools._vendor import tomli as tomllib

from build_source import copy_file, copy_project_sources, copy_source_tree, require_local_path


SDK_SOURCE_FILES = (
    'pyproject.toml', 'LICENSE', 'NOTICE', 'README.md', 'MANIFEST.in',
)
SDK_RELEASE_FILES = (
    'setup.py', 'setup.cfg', 'README.zh-CN.md', 'CHANGELOG.md', 'CONTRIBUTING.md',
    'SECURITY.md', 'PROVENANCE.md', 'RELEASING.md', 'SOURCE_MANIFEST.json', 'PKG-INFO',
)
SDK_SOURCE_DIRECTORIES = ('docs', 'examples', 'tests', 'scripts', 'wiki', 'DocsforAgents')


def project_metadata(root: Path):
    path = root / 'pyproject.toml'
    require_local_path(path, root)
    with path.open('rb') as stream:
        return tomllib.load(stream)['project']


def workflow_version(root: Path):
    path = root / 'src' / 'modport' / 'workflow.py'
    require_local_path(path, root)
    for statement in ast.parse(path.read_text(encoding='utf-8')).body:
        if isinstance(statement, ast.Assign) and any(
                isinstance(target, ast.Name) and target.id == 'WORKFLOW_VERSION'
                for target in statement.targets):
            value = ast.literal_eval(statement.value)
            if type(value) is int:
                return value
    raise ValueError('Current source must declare an integer WORKFLOW_VERSION')


def extract_sdk_wheel(wheel: Path, runtime: Path, version: str):
    """Install the supplied official wheel as-is, including its release metadata."""
    require_local_path(wheel, wheel.parent)
    with zipfile.ZipFile(wheel) as archive:
        metadata_paths = [name for name in archive.namelist() if name.endswith('.dist-info/METADATA')]
        if len(metadata_paths) != 1:
            raise ValueError('SDK wheel must contain one distribution metadata record')
        metadata = BytesParser().parsebytes(archive.read(metadata_paths[0]))
        if metadata.get('Name') != 'dispatcher-sdk' or metadata.get('Version') != version:
            raise ValueError('SDK wheel release version must match the corresponding SDK source')
        distribution = PurePosixPath(metadata_paths[0]).parts[0]
        for entry in archive.infolist():
            path = PurePosixPath(entry.filename)
            if (not path.parts or path.is_absolute() or '..' in path.parts
                    or '\\' in entry.filename
                    or path.parts[0] not in {'dispatcher_sdk', distribution}
                    or stat.S_ISLNK(entry.external_attr >> 16)):
                raise ValueError(f'Unsafe SDK wheel entry: {entry.filename}')
        archive.extractall(runtime)


def copy_sdk_sources(root: Path, destination: Path):
    copy_source_tree(root, 'src/dispatcher_sdk', destination / 'src' / 'dispatcher_sdk')
    for name in SDK_SOURCE_FILES:
        copy_file(root / name, destination / name, root=root)
    for name in SDK_RELEASE_FILES:
        if (root / name).exists() or (root / name).is_symlink():
            copy_file(root / name, destination / name, root=root)
    for name in SDK_SOURCE_DIRECTORIES:
        if (root / name).exists() or (root / name).is_symlink():
            copy_source_tree(root, name, destination / name)
    workflows = root / '.github' / 'workflows'
    if workflows.exists() or workflows.is_symlink():
        require_local_path(workflows, root)
        for path in sorted(workflows.iterdir()):
            if path.suffix in {'.yml', '.yaml'}:
                copy_file(path, destination / path.relative_to(root), root=root)


def archive_bundle(bundle: Path, platform: str):
    destination = bundle.with_suffix('.zip' if platform == 'windows' else '.tar.gz')
    temporary = destination.with_name(destination.name + '.partial')
    if platform == 'windows':
        with zipfile.ZipFile(temporary, 'w', compression=zipfile.ZIP_DEFLATED, compresslevel=3) as archive:
            for file in sorted(bundle.rglob('*')):
                archive.write(file, file.relative_to(bundle.parent).as_posix())
    else:
        with tarfile.open(temporary, 'w:gz', compresslevel=3) as archive:
            archive.add(bundle, arcname=bundle.name)
    temporary.replace(destination)
    return str(destination)


def assemble(platform: str, root: Path, destination: Path, sdk_root: Path, sdk_wheel: Path | None = None):
    sdk_root = sdk_root.expanduser().absolute()
    if not (sdk_root / 'src' / 'dispatcher_sdk').is_dir():
        raise ValueError('SDK source is missing; place the finalized checkout in '
                         'dispatcher-sdk/ or pass --sdk-root PATH')
    modport_project = project_metadata(root)
    sdk_version = project_metadata(sdk_root)['version']
    pins = {match.group(1) for requirement in modport_project.get('dependencies', [])
            if (match := re.fullmatch(r'dispatcher[-_]sdk\s*==\s*([A-Za-z0-9.+_-]+)',
                                      requirement.strip(), re.IGNORECASE))}
    if pins != {sdk_version}:
        raise ValueError('SDK source release must match the exact ModPort dispatcher-sdk dependency pin')
    sdk_wheel = (sdk_wheel or root / 'build' / 'sdk-release' / sdk_version /
                 f'dispatcher_sdk-{sdk_version}-py3-none-any.whl').expanduser().absolute()
    package_path = root / 'desktop' / 'package.json'
    require_local_path(package_path, root)
    application_version = json.loads(package_path.read_text(encoding='utf-8'))['version']
    downloads = root / 'build' / 'desktop' / 'downloads'
    bundle = destination / ('ModPort-' + platform + '-x64')
    if bundle.exists():
        shutil.rmtree(bundle)
    bundle.mkdir(parents=True)
    with zipfile.ZipFile(downloads / ('electron-' + platform + '.zip')) as archive:
        archive.extractall(bundle)
    runtime = bundle / 'runtime'
    runtime.mkdir()
    copy_source_tree(root, 'src/modport', runtime / 'modport')
    extract_sdk_wheel(sdk_wheel, runtime, sdk_version)
    metadata = runtime / ('modport-' + modport_project['version'] + '.dist-info')
    metadata.mkdir()
    fields = ['Metadata-Version: 2.1', 'Name: ' + modport_project['name'],
              'Version: ' + modport_project['version']]
    if modport_project.get('requires-python'):
        fields.append('Requires-Python: ' + modport_project['requires-python'])
    fields.extend('Requires-Dist: ' + requirement for requirement in modport_project.get('dependencies', []))
    (metadata / 'METADATA').write_text('\n'.join(fields) + '\n', encoding='utf-8')
    native = bundle / 'resources' / 'app'
    native.mkdir(parents=True, exist_ok=True)
    for name in ('package.json', 'main.cjs', 'preload.cjs', 'i18n.cjs', 'updates.cjs'):
        copy_file(root / 'desktop' / name, native / name, root=root)
    tools = bundle / 'tools'
    tools.mkdir()
    if platform == 'windows':
        with zipfile.ZipFile(downloads / 'opencode-windows.zip') as archive:
            archive.extractall(tools)
    else:
        with tarfile.open(downloads / 'opencode-linux.tar.gz') as archive:
            archive.extractall(tools, filter='data')
        (tools / 'opencode').chmod(0o755)
    licenses = bundle / 'licenses'
    licenses.mkdir()
    # Electron extracts its own LICENSE at the bundle root. Preserve it before
    # placing ModPort's LICENSE there; Chromium's separate notices stay in place.
    shutil.copy2(bundle / 'LICENSE', licenses / 'Electron-LICENSE.txt')
    for name in ('LICENSE', 'NOTICE', 'ATTRIBUTION.md', 'THIRD_PARTY_NOTICES.md'):
        copy_file(root / name, bundle / name, root=root)
    for name in ('LICENSE', 'NOTICE'):
        copy_file(sdk_root / name, licenses / ('dispatcher-sdk-' + name + '.txt'), root=sdk_root)
    copy_file(downloads / 'opencode-LICENSE.txt', licenses / 'OpenCode-LICENSE.txt', root=root)
    if platform == 'windows':
        python = bundle / 'python'
        python.mkdir()
        with zipfile.ZipFile(downloads / 'python-windows.zip') as archive:
            archive.extractall(python)
        # Embedded Python enables only the bundled source root and stdlib.
        (python / 'python313._pth').write_text('python313.zip\n.\n../runtime\n', encoding='utf-8')
        (bundle / 'electron.exe').rename(bundle / 'ModPort.exe')
    else:
        with tarfile.open(downloads / 'python-linux.tar.gz') as archive:
            archive.extractall(bundle, filter='data')
        for name in ('electron', 'chrome-sandbox', 'chrome_crashpad_handler'):
            (bundle / name).chmod(0o755)
        (bundle / 'electron').rename(bundle / 'ModPort')
        # -I host MCP subprocesses need the bundled SDK without relying on env.
        site = next((bundle / 'python' / 'lib').glob('python*/site-packages'))
        (site / 'modport-runtime.pth').write_text('../../../../runtime\n', encoding='utf-8')
    copy_file(root / 'docs' / 'DESKTOP.md', bundle / '使用说明.md', root=root)
    # Supply the exact editable sources and build instructions with the binaries.
    # Do not package local validation records, Run data or an unrelated SDK tree.
    source = bundle / 'source'
    copy_project_sources(root, source / 'modport')
    sdk_source = source / 'dispatcher-sdk'
    copy_sdk_sources(sdk_root, sdk_source)
    (source / 'README.md').write_text(
        '# Corresponding source\n\n'
        'ModPort source is in `modport/`; the exact supplied SDK source is in '
        '`dispatcher-sdk/`. See `modport/docs/RELEASING.md` for build instructions.\n'
        'Install the SDK first, then ModPort. Rebuilding the desktop bundle also '
        'requires the official runtime archives documented there.\n'
        'The runtime Python modules in `../runtime/` are also supplied as source.\n',
        encoding='utf-8',
    )
    (bundle / 'build-info.json').write_text(json.dumps({
        'application_version': application_version, 'workflow_version': workflow_version(root),
        'modport_version': modport_project['version'], 'sdk_version': sdk_version,
        'electron_version': '44.5.1', 'opencode_version': '1.18.32', 'platform': platform,
        'native_windows_acceptance': 'unverified' if platform == 'windows' else None,
    }, indent=2) + '\n', encoding='utf-8')
    archive = archive_bundle(bundle, platform)
    print(archive)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--platform', choices=('linux', 'windows', 'both'), default='both')
    parser.add_argument('--sdk-root', type=Path,
                        help='official SDK release source (default: dispatcher-sdk/ in this checkout)')
    parser.add_argument('--sdk-wheel', type=Path,
                        help='official SDK wheel (default: build/sdk-release/VERSION/dispatcher_sdk-VERSION-py3-none-any.whl)')
    parser.add_argument('--destination', type=Path)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    destination = args.destination or root / 'dist' / 'desktop'
    destination.mkdir(parents=True, exist_ok=True)
    for platform in ('linux', 'windows') if args.platform == 'both' else (args.platform,):
        assemble(platform, root, destination, args.sdk_root or root / 'dispatcher-sdk', args.sdk_wheel)


if __name__ == '__main__':
    main()
