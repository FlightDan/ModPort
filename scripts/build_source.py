"""Build a source distribution and wheel from an explicit public source set.

Local runs, credentials, deployment archives and downloaded runtimes are never
inputs. This script uses the current interpreter's setuptools; it does not
download dependencies or copy the independently developed Dispatcher SDK.
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile


PROJECT_FILES = (
    'pyproject.toml', 'setup.py', 'MANIFEST.in', 'README.md', 'README.en.md', 'LICENSE', 'NOTICE',
    'ATTRIBUTION.md', 'THIRD_PARTY_NOTICES.md', 'CONTRIBUTING.md', 'SECURITY.md',
    'desktop/package.json', 'desktop/main.cjs', 'desktop/preload.cjs', 'desktop/i18n.cjs', 'desktop/updates.cjs',
    'scripts/build_source.py', 'scripts/build_desktop.py',
    'docs/README.md', 'docs/WORKFLOW.md', 'docs/AGENT_RULES.md', 'docs/EVIDENCE_PROTOCOL.md',
    'docs/DESKTOP.md', 'docs/DESKTOP_API.md', 'docs/DRIVER_LEASE.md',
    'docs/RELEASING.md', 'docs/WIKI_KNOWLEDGE.md',
)
SOURCE_SUFFIXES = {'.py', '.md', '.json', '.js', '.cjs', '.css', '.html', '.ts', '.sh', '.java', '.typed'}
EXCLUDED_DIRECTORIES = {'__pycache__', '.git', '.aws', '.ssh', '.codex', 'node_modules'}


def require_local_path(source: Path, root: Path):
    relative = source.relative_to(root)
    current = root
    for component in ('', *relative.parts):
        current = current / component
        if current.is_symlink():
            raise ValueError(f'release input must not be a symlink: {current}')


def copy_file(source: Path, destination: Path, *, root: Path):
    require_local_path(source, root)
    if source.is_symlink() or not source.is_file():
        raise ValueError(f'release input must be a regular file: {source}')
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination)


def copy_source_tree(root: Path, relative: str, destination: Path):
    """Copy only source assets, preserving notices and excluding local secrets."""
    base = root / relative
    require_local_path(base, root)
    if not base.is_dir():
        raise ValueError(f'release source directory is unavailable: {base}')
    for directory, subdirs, files in os.walk(base, followlinks=False):
        parent = Path(directory)
        subdirs[:] = sorted(name for name in subdirs if name not in EXCLUDED_DIRECTORIES)
        for name in subdirs:
            require_local_path(parent / name, root)
        for name in sorted(files):
            source = parent / name
            if name == '.env' or name.startswith('.env.') or source.suffix in {'.pem', '.key'}:
                continue
            if source.suffix in SOURCE_SUFFIXES or name in {'LICENSE', 'NOTICE'}:
                copy_file(source, destination / source.relative_to(base), root=root)


def copy_project_sources(root: Path, destination: Path):
    """Copy current editable source, tests and reusable docs, not local state."""
    for name in PROJECT_FILES:
        copy_file(root / name, destination / name, root=root)
    for name in ('src/modport', 'tests'):
        copy_source_tree(root, name, destination / name)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--destination', type=Path)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    destination = (args.destination or root / 'dist' / 'opensource').expanduser().absolute()
    destination.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='modport-source-') as temporary:
        staging = Path(temporary) / 'modport'
        copy_project_sources(root, staging)
        subprocess.run(
            [sys.executable, '-c',
             'import sys; from setuptools.build_meta import build_sdist, build_wheel; '
             'destination = sys.argv[1]; '
             'build_sdist(destination); build_wheel(destination)', str(destination)],
            cwd=staging, check=True,
        )
    print(f'Source distribution and wheel: {destination}')


if __name__ == '__main__':
    main()
