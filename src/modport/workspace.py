"""Resolve the host-frozen developer workspace without relocating Run evidence.

Only the logical ``worktree`` root can refer to a local developer directory.
Baselines, task clones, credentials, scheduler stores and evidence remain in the
Run. Copy/direct workspaces use private engine Git metadata, leaving any user
repository, index and branch in the selected folder untouched.
"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
import json
import os
from pathlib import Path, PureWindowsPath
import subprocess
from typing import Mapping

_ACTIVE_ROOT = ContextVar('modport_workspace_root', default=None)
WORKSPACE_MODES = frozenset({'git_worktree', 'copy', 'direct'})


def validate_workspace_spec(value):
    """Validate value structure only; filesystem authority is checked on use."""
    if not isinstance(value, Mapping) or set(value) - {'mode', 'path', 'original_path', 'branch', 'git_directory'}:
        raise ValueError('invalid local workspace settings')
    if value.get('mode') not in WORKSPACE_MODES:
        raise ValueError('unsupported local workspace mode')
    for name in ('path', 'original_path'):
        path = value.get(name)
        if (not isinstance(path, str) or not path or len(path) > 4096
                or any(ord(character) < 32 for character in path)
                or not Path(path).is_absolute() or '..' in Path(path).parts):
            raise ValueError('local workspace requires absolute project paths')
    branch = value.get('branch')
    if value['mode'] == 'git_worktree':
        if not isinstance(branch, str) or not branch.strip():
            raise ValueError('Git worktree requires a new branch name')
        directory = value.get('git_directory')
        if not isinstance(directory, str) or not Path(directory).is_absolute() or '..' in Path(directory).parts:
            raise ValueError('Git worktree requires its host-registered Git directory')
    elif branch is not None:
        raise ValueError('only Git worktree mode accepts a branch')
    if value['mode'] == 'direct' and value['path'] != value['original_path']:
        raise ValueError('direct development must use the selected folder')


def workspace_spec(root):
    root = Path(root).absolute()
    header = root / 'run.json'
    if not header.exists():
        return None
    from .platform_files import safe_open
    descriptor = safe_open(root, 'run.json')
    with os.fdopen(descriptor, 'rb') as stream:
        raw = stream.read(4 * 1024 * 1024 + 1)
    if len(raw) > 4 * 1024 * 1024:
        raise ValueError('Run header exceeds workspace settings size limit')
    value = json.loads(raw).get('request', {}).get('local_workspace')
    if value is None:
        return None
    validate_workspace_spec(value)
    path = Path(value['path'])
    from .platform_files import assert_no_reparse
    assert_no_reparse(path)
    if path.resolve() != path.absolute():
        raise ValueError('local workspace cannot traverse links or junctions')
    if path == root or path in root.parents or root in path.parents:
        raise ValueError('developer workspace must be separate from Run state')
    return dict(value)


def project_path(root, relative='worktree'):
    """Resolve a logical Run path, including the exact bound project subtree."""
    root = Path(root)
    relative = Path(relative)
    # Host execution IDs contain colons on POSIX. They are ordinary filename
    # characters there, but must remain forbidden as alternate streams on Windows.
    if (relative.is_absolute() or any(PureWindowsPath(part).drive for part in relative.parts)
            or '..' in relative.parts
            or any('\\' in part for part in relative.parts)
            or (os.name == 'nt' and any(':' in part for part in relative.parts))):
        raise ValueError('project reference must be a contained relative path')
    if relative.parts and relative.parts[0] == 'worktree':
        specification = workspace_spec(root)
        if specification:
            base = Path(specification['path'])
            result = base.joinpath(*relative.parts[1:])
            if not result.is_relative_to(base):
                raise ValueError('project reference escapes the registered workspace')
            return result
    return root / relative


def project_relative(root, path):
    root, path = Path(root).absolute(), Path(path).absolute()
    if path.is_relative_to(root):
        return path.relative_to(root)
    specification = workspace_spec(root)
    if specification and path.is_relative_to(Path(specification['path'])):
        return Path('worktree') / path.relative_to(specification['path'])
    raise ValueError('path is outside this Run and its developer workspace')


def is_project_workspace(root, path):
    try:
        relative = project_relative(root, path)
    except ValueError:
        return False
    return bool(relative.parts and relative.parts[0] in {'baseline', 'worktree', 'workspaces'})


def is_run_path(root, path):
    try:
        project_relative(root, path)
        return True
    except ValueError:
        return False


@contextmanager
def workspace_context(root):
    token = _ACTIVE_ROOT.set(Path(root).absolute())
    try:
        yield
    finally:
        _ACTIVE_ROOT.reset(token)


def engine_repository(root):
    specification = workspace_spec(root)
    if specification and specification['mode'] in {'copy', 'direct'}:
        return Path(root) / 'workspace.git'
    return None


def _credential_excludes(root):
    """Case-independent named exclusions without changing the user's Git config."""
    from .local_workspace_sandbox import SENSITIVE_NAMES, ENV_EXAMPLES
    from .platform_files import atomic_write
    def pattern(value):
        return ''.join('[' + char.lower() + char.upper() + ']' if char.isascii() and char.isalpha()
                       else char for char in value)
    data = '\n'.join(pattern(name) for name in sorted(SENSITIVE_NAMES | {'.env', '.env.*'}))
    data += '\n' + '\n'.join('!' + pattern(name) for name in sorted(ENV_EXAMPLES)) + '\n'
    destination = Path(root) / 'workspace-git-excludes'
    if not destination.exists():
        atomic_write(destination, data.encode())
    return destination


def git_command(arguments, *, cwd=None, environment=None, root=None):
    """Bind host Git calls to private metadata only for the selected workspace.

    Task repositories retain their own Git state. In particular, a clone of
    the shared direct workspace must read the engine repository rather than
    the user's unrelated/absent .git directory.
    """
    arguments = list(arguments)
    root = Path(root) if root is not None else _ACTIVE_ROOT.get()
    if (root is None or not arguments
            or Path(arguments[0]).name.lower() not in {'git', 'git.exe'}):
        return arguments, environment
    specification = workspace_spec(root)
    if not specification:
        return arguments, environment
    workspace = Path(specification['path'])
    directory = Path(cwd or os.getcwd()).absolute()
    explicit_repository = any(arg == '--git-dir' or arg.startswith('--git-dir=') for arg in arguments[1:])
    for index, argument in enumerate(arguments[:-1]):
        if argument == '-C':
            selected = Path(arguments[index + 1])
            directory = selected.absolute() if selected.is_absolute() else directory / selected
    private = engine_repository(root)
    if private and 'clone' in arguments and str(workspace) in arguments:
        arguments[arguments.index(str(workspace))] = str(private)
    if directory != workspace or explicit_repository:
        return arguments, environment
    if specification['mode'] == 'direct' and 'reset' in arguments and '--hard' in arguments:
        raise ValueError('direct development preserves partial edits; automatic hard reset is disabled')
    environment = dict(os.environ if environment is None else environment)
    temporary_index = environment.get('GIT_INDEX_FILE')
    environment = {key: value for key, value in environment.items() if not key.upper().startswith('GIT_')}
    environment.update({'GIT_CONFIG_GLOBAL': os.devnull, 'GIT_CONFIG_SYSTEM': os.devnull,
                        'GIT_CONFIG_NOSYSTEM': '1', 'GIT_TERMINAL_PROMPT': '0',
                        'GIT_NO_REPLACE_OBJECTS': '1', 'GIT_ATTR_NOSYSTEM': '1'})
    options = ['-c', 'core.hooksPath=' + os.devnull, '-c', 'core.fsmonitor=false',
               '-c', 'core.untrackedCache=false', '-c', 'core.attributesFile=' + os.devnull,
               '-c', 'commit.gpgsign=false', '-c', 'tag.gpgsign=false',
               '-c', 'credential.helper=', '-c', 'gc.auto=0', '-c', 'maintenance.auto=false',
               '-c', 'user.name=ModPort', '-c', 'user.email=modport@localhost']
    options.extend(['-c', 'core.excludesFile=' + str(_credential_excludes(root))])
    if private:
        environment.update(GIT_DIR=str(private.absolute()), GIT_WORK_TREE=str(workspace))
    else:
        directory = Path(specification['git_directory'])
        from .platform_files import assert_no_reparse
        assert_no_reparse(directory)
        if directory.resolve() != directory.absolute() or not directory.is_dir():
            raise ValueError('registered developer Git directory is missing or unsafe')
        environment.update(GIT_DIR=str(directory), GIT_WORK_TREE=str(workspace))
        # Reading filter names does not execute filters. Disable every locally
        # configured driver before status/add/checkout can inspect attributes.
        configured = subprocess.run([arguments[0], '-C', str(workspace), 'config',
            '--name-only', '--get-regexp', r'^filter\..*\.(clean|smudge|process|required)$'],
            env=environment, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=10)
        if configured.returncode not in (0, 1) or len(configured.stdout) > 65536:
            raise ValueError('cannot safely inspect developer repository filter settings')
        for key in configured.stdout.decode('utf-8', errors='strict').splitlines():
            if not key.startswith('filter.') or any(ord(character) < 32 for character in key):
                raise ValueError('invalid repository filter setting')
            options.extend(['-c', key + ('=false' if key.endswith('.required') else '=')])
        branch = subprocess.run([arguments[0], 'symbolic-ref', '--quiet', 'HEAD'],
            cwd=workspace, env=environment, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=10)
        if branch.returncode or branch.stdout.decode().strip() != 'refs/heads/' + specification['branch']:
            raise ValueError('developer workspace switched away from its registered migration branch')
    if temporary_index:
        index = Path(temporary_index)
        if (index.is_absolute() and index.is_relative_to(root.absolute())
                and index.resolve() == index.absolute()):
            environment['GIT_INDEX_FILE'] = str(index)
    return [arguments[0], *options, *arguments[1:]], environment


def git_probe(arguments, **options):
    """subprocess.run-compatible Git probe for helpers outside audited calls."""
    arguments, environment = git_command(arguments, cwd=options.get('cwd'),
                                          environment=options.get('env'))
    if environment is not None:
        options['env'] = environment
    return subprocess.run(arguments, **options)


def initialize_engine_repository(root, source_commit):
    """Create an index over existing files; never check out or reset originals."""
    root = Path(root).absolute()
    private = engine_repository(root)
    if private is None:
        return
    workspace = project_path(root)
    from .platform_runtime import capture_process
    environment = {key: value for key, value in os.environ.items()
                   if key.upper() in {'PATH', 'SYSTEMROOT', 'WINDIR', 'TEMP', 'TMP', 'TMPDIR', 'LANG', 'LC_ALL'}}
    environment.update(GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_SYSTEM=os.devnull,
                       GIT_CONFIG_NOSYSTEM='1', GIT_TERMINAL_PROMPT='0')
    def git(*arguments):
        result = capture_process(['git', '-c', 'core.hooksPath=' + os.devnull, *arguments],
            cwd=root, environment=environment, timeout=60, max_output_bytes=65536)
        if result.returncode or result.timed_out or result.drain_incomplete:
            raise ValueError('cannot initialize private developer workspace Git state: '
                             + result.stderr.decode('utf-8', errors='replace')[-1500:])
    if not private.exists():
        git('clone', '--bare', '--no-local', '--template=', str(root / 'repository.git'), str(private))
    marker = private / 'modport-workspace-ready'
    if marker.exists():
        return
    git('--git-dir', str(private), 'config', 'core.bare', 'false')
    git('--git-dir', str(private), 'config', 'core.worktree', str(workspace))
    git('--git-dir', str(private), 'symbolic-ref', 'HEAD', 'refs/heads/modport-development')
    git('--git-dir', str(private), 'update-ref', 'HEAD', source_commit)
    information = private / 'info'
    information.mkdir(exist_ok=True)
    (information / 'attributes').write_text('* -text -filter -ident -working-tree-encoding\n', encoding='utf-8')
    from .desktop_local_source import EXCLUDED_NAMES, EXCLUDED_DIRECTORY_NAMES
    excluded = sorted((EXCLUDED_NAMES - {'.modport'}) | EXCLUDED_DIRECTORY_NAMES | {'.env', '.env.*'})
    (information / 'exclude').write_text('\n'.join(excluded) + '\n!.env.example\n!.env.sample\n!.env.template\n', encoding='utf-8')
    git('--git-dir', str(private), 'read-tree', source_commit)
    marker.write_text('initialized\n', encoding='utf-8')
