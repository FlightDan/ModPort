"""Credential exclusions and isolated build staging for local workspaces.

Only names and metadata are inspected under excluded roots. Project code is
never executed on the host: the launcher delegates to bubblewrap/AppContainer.
"""
from __future__ import annotations

from contextlib import contextmanager
import json
import os
from pathlib import Path
import re
import shutil
import stat
import sys
import time
from uuid import uuid4

from .platform_files import (_open_posix_directory, assert_no_reparse, atomic_write, file_os,
                             make_private_directory, safe_open)

SENSITIVE_NAMES = frozenset({
    '.git', '.aws', '.ssh', '.codex', '.kube', '.docker', '.azure',
    '.git-credentials', '.gitconfig', '.netrc', '.npmrc', '.pypirc', '.dockercfg',
    'credentials', 'credentials.json', 'auth.json', 'id_rsa', 'id_ed25519',
    'host-runtime', 'model-settings', 'source-snapshots',
    'kernel.sqlite3', 'kernel.sqlite3-wal', 'kernel.sqlite3-shm',
    'orchestrator.sqlite3', 'orchestrator.sqlite3-wal', 'orchestrator.sqlite3-shm',
    'desktop.sqlite3', 'desktop.sqlite3-wal', 'desktop.sqlite3-shm',
})
ENV_EXAMPLES = frozenset({'.env.example', '.env.sample', '.env.template'})
MAX_ENTRIES = 100_000
MAX_DEPTH = 64
MAX_FILE_BYTES = 256 * 1024 * 1024
MAX_TOTAL_BYTES = 2 * 1024 * 1024 * 1024
INVENTORY_SECONDS = 60


def is_sensitive_name(name: str) -> bool:
    value = name.casefold()
    return value in SENSITIVE_NAMES or (value not in ENV_EXAMPLES
        and (value == '.env' or value.startswith('.env.')))


# Kept as a local shorthand for inventory and permission construction.
sensitive_name = is_sensitive_name


def _workspace_spec(root):
    from .workspace import workspace_spec
    return workspace_spec(root)


def external_workspace(root, worktree) -> bool:
    spec = _workspace_spec(root)
    return spec is not None and Path(spec['path']) == Path(worktree).absolute()


@contextmanager
def _directory(path):
    # The facade uses anchored O_NOFOLLOW on POSIX and pinned ancestor handles
    # on Windows, so recursive enumeration cannot follow a substituted link.
    descriptor = (_open_posix_directory(path) if os.name != 'nt' else
                  file_os.open(path, file_os.O_RDONLY | file_os.O_DIRECTORY | file_os.O_NOFOLLOW))
    try:
        yield descriptor
    finally:
        file_os.close(descriptor)


def workspace_inventory(workspace: Path) -> tuple[dict[Path, bool], dict[Path, bool]]:
    """Return visible/excluded relative paths with their directory flag.

    Reject links, special objects and visible hard links; never list excluded
    directories. Generated build trees and .modport protocol files stay visible.
    """
    workspace = Path(workspace).absolute()
    assert_no_reparse(workspace)
    visible, excluded = {}, {}
    count, total = 0, 0
    deadline = time.monotonic() + INVENTORY_SECONDS

    def walk(relative, depth):
        nonlocal count, total
        if depth > MAX_DEPTH:
            raise ValueError('workspace exceeds bounded sandbox directory depth')
        with _directory(workspace / relative) as directory:
            with file_os.scandir(directory) as entries:
                for entry in entries:
                    count += 1
                    if count > MAX_ENTRIES or time.monotonic() > deadline:
                        raise ValueError('workspace exceeds bounded sandbox inventory')
                    name = entry.name
                    if any(ord(char) < 32 for char in name):
                        raise ValueError('workspace filename contains control characters')
                    child = relative / name
                    info = file_os.stat(name, dir_fd=directory, follow_symlinks=False)
                    is_directory = stat.S_ISDIR(info.st_mode)
                    if (stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400
                            or not (is_directory or stat.S_ISREG(info.st_mode))):
                        raise ValueError(f'unsafe workspace object: {child.as_posix()}')
                    if sensitive_name(name):
                        excluded[child] = is_directory
                        continue
                    if not is_directory:
                        if info.st_nlink != 1:
                            raise ValueError(f'workspace file has hard-link aliases: {child.as_posix()}')
                        total += info.st_size
                        if info.st_size > MAX_FILE_BYTES or total > MAX_TOTAL_BYTES:
                            raise ValueError('workspace exceeds bounded sandbox file bytes')
                    visible[child] = is_directory
                    if is_directory:
                        walk(child, depth + 1)
    walk(Path('.'), 0)
    return visible, excluded


def linux_workspace_masks(root: Path, worktree: Path) -> list[str]:
    """Append after the workspace bind; do not subsequently rebind Git metadata."""
    if _workspace_spec(root) is None:
        return []
    _, excluded = workspace_inventory(worktree)
    if not excluded:
        return []
    mask_root = make_private_directory(Path(root).absolute() / 'toolchains' / 'workspace-masks')
    folder = mask_root / 'empty-directory'
    if not folder.exists():
        folder.mkdir(mode=0o500)
    assert_no_reparse(folder)
    with _directory(folder) as directory:
        if file_os.listdir(directory):
            raise ValueError('sandbox empty directory mask has unexpected contents')
    empty = mask_root / 'empty-file'
    try:
        descriptor = safe_open(mask_root, empty.name, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o400)
    except FileExistsError:
        pass
    else:
        os.close(descriptor)
    descriptor = safe_open(mask_root, empty.name)
    with os.fdopen(descriptor, 'rb') as stream:
        if stream.read(1):
            raise ValueError('sandbox empty file mask has unexpected contents')
    result = []
    for relative, directory in sorted(excluded.items()):
        result.extend(['--ro-bind', str(folder if directory else empty),
                       '/workspace/' + relative.as_posix()])
    return result


def workspace_sensitive_permissions(worktree: Path) -> dict:
    """OpenCode file-tool denies; these supplement, never replace, OS isolation."""
    patterns = {}
    for name in sorted(SENSITIVE_NAMES | {'.env', '.env.*'}):
        for spelling in (name, name.upper()):
            for prefix in ('', '**/', str(Path(worktree).absolute()).replace('\\', '/') + '/**/'):
                patterns[prefix + spelling] = 'deny'
                patterns[prefix + spelling + '/**'] = 'deny'
    _, excluded = workspace_inventory(worktree)
    for relative in excluded:
        for value in (relative.as_posix(), (Path(worktree).absolute() / relative).as_posix()):
            patterns[value] = 'deny'
            patterns[value + '/**'] = 'deny'
    # OpenCode uses the last matching rule. Explicit harmless examples override
    # the broad .env.* rule; all credential/app-private entries remain denied.
    for name in sorted(ENV_EXAMPLES):
        for prefix in ('', '**/', str(Path(worktree).absolute()).replace('\\', '/') + '/**/'):
            patterns[prefix + name] = 'allow'
    # Grep permissions are query-based, not file-path rules. Route searches
    # through the filtered project-command sandbox instead of exposing secrets
    # via grep matches in the original directory.
    return {'read': dict(patterns), 'edit': dict(patterns), 'grep': 'deny'}


def _read_file(root, relative, *, deadline=None):
    descriptor = safe_open(root, relative, os.O_RDONLY | (os.O_NONBLOCK if os.name != 'nt' else 0))
    with os.fdopen(descriptor, 'rb') as stream:
        pieces, total = [], 0
        while True:
            if deadline is not None and time.monotonic() > deadline:
                raise TimeoutError('workspace copy exceeded staging time bound')
            data = stream.read(1024 * 1024)
            if not data:
                return b''.join(pieces)
            total += len(data)
            if total > MAX_FILE_BYTES:
                raise ValueError('workspace file exceeds staging byte bound')
            pieces.append(data)


def _same_files(left, right, relative):
    descriptors = []
    try:
        for root in (left, right):
            descriptors.append(safe_open(root, relative))
        total = 0
        while True:
            a, b = (os.read(descriptor, 1024 * 1024) for descriptor in descriptors)
            total += max(len(a), len(b))
            if total > MAX_FILE_BYTES:
                raise ValueError('workspace file exceeds synchronization byte bound')
            if a != b:
                return False
            if not a:
                return True
    finally:
        for descriptor in descriptors:
            os.close(descriptor)


def _matches_baseline(source, baseline, relative, existed):
    if existed:
        try:
            return _same_files(source, baseline, relative)
        except FileNotFoundError:
            return False
    try:
        descriptor = safe_open(source, relative)
    except FileNotFoundError:
        return True
    os.close(descriptor)
    return False


def _descriptor_matches_baseline(descriptor, baseline, relative):
    other = safe_open(baseline, relative)
    try:
        total = 0
        while True:
            current, original = os.read(descriptor, 1024 * 1024), os.read(other, 1024 * 1024)
            total += max(len(current), len(original))
            if total > MAX_FILE_BYTES:
                raise ValueError('workspace file exceeds synchronization byte bound')
            if current != original:
                return False
            if not current:
                return True
    finally:
        os.close(other)


def _write_new(root, relative, data, *, mode=0o600):
    descriptor = safe_open(root, relative, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    with os.fdopen(descriptor, 'wb') as stream:
        stream.write(data)


def _ensure_directories(root, relative):
    current = root
    for component in relative.parts:
        with _directory(current) as directory:
            try:
                file_os.mkdir(component, mode=0o700, dir_fd=directory)
            except FileExistsError:
                pass
            current = current / component
            with _directory(current):
                pass


def create_build_stage(source: Path, private: Path) -> tuple[Path, Path]:
    """Copy eligible files into two host-owned trees without executing them."""
    source, private = Path(source).absolute(), make_private_directory(Path(private).absolute())
    stage, baseline = private / 'workspace', private / 'baseline'
    stage.mkdir(mode=0o700)
    baseline.mkdir(mode=0o700)
    visible, _ = workspace_inventory(source)
    deadline = time.monotonic() + INVENTORY_SECONDS
    total = 0
    for relative, directory in visible.items():
        if directory:
            (stage / relative).mkdir()
            (baseline / relative).mkdir()
        else:
            data = _read_file(source, relative, deadline=deadline)
            total += len(data)
            if total > MAX_TOTAL_BYTES:
                raise ValueError('workspace exceeds staging byte bound')
            descriptor = safe_open(source, relative)
            try:
                mode = stat.S_IMODE(os.fstat(descriptor).st_mode)
            finally:
                os.close(descriptor)
            _write_new(stage, relative, data, mode=mode & 0o777)
            _write_new(baseline, relative, data)
    return stage, baseline


@contextmanager
def _exclusive_output(source, relative, *, create=False, delete=False):
    """Deny native Windows writers; POSIX locks cooperate with flock users only."""
    if os.name != 'nt':
        import fcntl
        with _directory(source / relative.parent) as parent:
            descriptor = os.open(relative.name, os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK
                | (os.O_CREAT | os.O_EXCL if create else 0), 0o600, dir_fd=parent)
            try:
                info = os.fstat(descriptor)
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                    raise ValueError('unsafe workspace output')
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                yield descriptor
            finally:
                os.close(descriptor)
        return
    import ctypes
    import msvcrt
    from .platform_files import _FILE_INFORMATION, pinned_windows_path
    from .windows_process import kernel32, INVALID_HANDLE_VALUE, win_error
    api = kernel32()
    path = source / relative
    with pinned_windows_path(path.parent):
        # FILE_SHARE_READ only: existing writers/deleters prevent admission and
        # future ones cannot race the byte comparison and update.
        handle = api.CreateFileW(str(path), 0x80000000 | 0x40000000 | (0x10000 if delete else 0) | 0x80,
                                 1, None, 1 if create else 3, 0x00200000, None)
        if handle == INVALID_HANDLE_VALUE:
            raise win_error('CreateFileW(exclusive workspace output)')
        try:
            details = _FILE_INFORMATION()
            if not api.GetFileInformationByHandle(handle, ctypes.byref(details)):
                raise win_error('GetFileInformationByHandle(exclusive workspace output)')
            if details.attributes & (0x400 | 0x10) or details.links != 1:
                raise ValueError('unsafe workspace output')
            descriptor = msvcrt.open_osfhandle(handle, os.O_RDWR | os.O_BINARY)
        except BaseException:
            api.CloseHandle(handle)
            raise
        try:
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise ValueError('unsafe workspace output')
            yield descriptor
        finally:
            os.close(descriptor)


def _assert_posix_output_binding(source, relative, descriptor):
    with _directory(source / relative.parent) as parent:
        current = os.stat(relative.name, dir_fd=parent, follow_symlinks=False)
        opened = os.fstat(descriptor)
        if (not stat.S_ISREG(current.st_mode) or current.st_nlink != 1
                or (current.st_dev, current.st_ino) != (opened.st_dev, opened.st_ino)):
            raise ValueError(f'concurrent workspace replacement prevents synchronization: {relative}')


def _remove_output_directory(source, relative):
    with _directory(source / relative.parent) as parent:
        if os.name != 'nt':
            os.rmdir(relative.name, dir_fd=parent)
            return
        import ctypes
        from .platform_files import _FILE_INFORMATION
        from .windows_process import kernel32, INVALID_HANDLE_VALUE, win_error
        api = kernel32()
        # Keep this exact directory against replacement until deletion settles.
        handle = api.CreateFileW(str(source / relative), 0x10000 | 0x80, 1 | 2,
                                 None, 3, 0x02000000 | 0x00200000, None)
        if handle == INVALID_HANDLE_VALUE:
            raise win_error('CreateFileW(directory output deletion)')
        try:
            information = _FILE_INFORMATION()
            if not api.GetFileInformationByHandle(handle, ctypes.byref(information)):
                raise win_error('GetFileInformationByHandle(directory output deletion)')
            if information.attributes & 0x400 or not information.attributes & 0x10:
                raise ValueError('unsafe workspace output directory')
            disposition = ctypes.c_ubyte(1)
            api.SetFileInformationByHandle.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32]
            api.SetFileInformationByHandle.restype = ctypes.c_int
            if not api.SetFileInformationByHandle(handle, 4, ctypes.byref(disposition), 1):
                raise win_error('SetFileInformationByHandle(delete output directory)')
        finally:
            api.CloseHandle(handle)


def sync_build_stage(source: Path, stage: Path, baseline: Path) -> None:
    """Publish eligible changes with byte/state conflict detection.

    Windows file sharing denies other writers during updates. POSIX conflict
    detection is best effort; avoid concurrent editing while synchronization
    runs because editors may ignore advisory flock locks.
    """
    before, _ = workspace_inventory(baseline)
    after, _ = workspace_inventory(stage)
    changes, directories = [], []
    for relative in sorted(set(before) | set(after)):
        old_directory, new_directory = before.get(relative), after.get(relative)
        if old_directory is True or new_directory is True:
            if old_directory is not None and new_directory is not None and old_directory != new_directory:
                raise ValueError(f'workspace output changes object type: {relative}')
            if old_directory != new_directory:
                directories.append((relative, old_directory is True))
            continue
        existed, exists = old_directory is False, new_directory is False
        if existed != exists or (existed and not _same_files(baseline, stage, relative)):
            changes.append((relative, existed, exists))
    # Preflight every conflict before applying any change. Recheck under native
    # exclusive handles during each write so a later user edit is preserved.
    for relative, existed, _ in changes:
        if not _matches_baseline(source, baseline, relative, existed):
            raise ValueError(f'concurrent workspace edit prevents build output synchronization: {relative}')
    current, excluded = workspace_inventory(source)
    for relative, existed in directories:
        if not existed:
            if relative in current or relative in excluded:
                raise ValueError(f'concurrent directory creation prevents synchronization: {relative}')
        else:
            expected = {path for path in before if path == relative or path.is_relative_to(relative)}
            actual = {path for path in current.keys() | excluded.keys()
                      if path == relative or path.is_relative_to(relative)}
            if actual != expected or current.get(relative) is not True:
                raise ValueError(f'concurrent or retained directory entries prevent synchronization: {relative}')
    for relative, existed in sorted(directories, key=lambda item: len(item[0].parts)):
        if not existed:
            with _directory(source / relative.parent) as parent:
                # No exist_ok: a directory created since preflight is a user
                # change, so do not merge into or overwrite it.
                file_os.mkdir(relative.name, mode=0o700, dir_fd=parent)
    for relative, existed, exists in changes:
        _ensure_directories(source, relative.parent)
        # Read potentially large stage output first. Original comparison and
        # binding checks then sit immediately before its final write/delete.
        new = _read_file(stage, relative) if exists else None
        with _exclusive_output(source, relative, create=not existed, delete=not exists) as descriptor:
            if existed and not _descriptor_matches_baseline(descriptor, baseline, relative):
                raise ValueError(f'concurrent workspace edit prevents build output synchronization: {relative}')
            if os.name != 'nt':
                _assert_posix_output_binding(source, relative, descriptor)
            if not exists:
                if os.name == 'nt':
                    import ctypes
                    import msvcrt
                    from .windows_process import kernel32, win_error
                    delete = ctypes.c_ubyte(1)
                    api = kernel32()
                    api.SetFileInformationByHandle.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32]
                    api.SetFileInformationByHandle.restype = ctypes.c_int
                    if not api.SetFileInformationByHandle(msvcrt.get_osfhandle(descriptor), 4,
                                                          ctypes.byref(delete), 1):
                        raise win_error('SetFileInformationByHandle(delete build output)')
                else:
                    with _directory(source / relative.parent) as parent:
                        os.lseek(descriptor, 0, os.SEEK_SET)
                        if not _descriptor_matches_baseline(descriptor, baseline, relative):
                            raise ValueError(f'concurrent workspace edit prevents synchronization: {relative}')
                        _assert_posix_output_binding(source, relative, descriptor)
                        os.unlink(relative.name, dir_fd=parent)
            else:
                os.lseek(descriptor, 0, os.SEEK_SET)
                os.ftruncate(descriptor, 0)
                with os.fdopen(os.dup(descriptor), 'wb') as output:
                    output.write(new)
                    output.flush()
                    os.fsync(output.fileno())
    for relative, existed in sorted(directories, key=lambda item: len(item[0].parts), reverse=True):
        if existed:
            # rmdir cannot remove a newly added file; it fails while retaining
            # those entries. Ancestors are pinned/anchored during the operation.
            _remove_output_directory(source, relative)


# Platform-neutral staging is also shared with the direct native launcher.
create_windows_stage = create_build_stage
sync_windows_stage = sync_build_stage


def convert_workspace_paths(value: str, source, destination, *, windows=False) -> str:
    """Replace complete project path prefixes inside argument/assignment tokens.

    Windows accepts either slash spelling and case. A sibling path or text
    suffix is never rewritten; conversion does not parse or execute shell code.
    """
    original = str(source).rstrip('/\\')
    if windows:
        parts = re.split(r'[/\\]', original)
        pattern = r'[/\\]'.join(re.escape(part) for part in parts)
    else:
        pattern = re.escape(original)
    # Allow standalone paths, quoted paths, -Dkey=PATH and path lists. Exclude
    # embedded prefixes such as /unrelated/root/project or root/project-other.
    start = r'(?<![A-Za-z0-9_./\\-])'
    end = r'(?=$|[/\\\s\"\';,:)=])' if windows else r'(?=$|[/\s\"\';,:)=])'
    return re.sub(start + pattern + end, lambda match: str(destination), value,
                  flags=re.IGNORECASE if windows else 0)


def _build_staged_command(root, stage, args, options, *, windows):
    if windows:
        from .windows_build import build_command
        options = dict(options)
        options.pop('wrapper_cache_info', None)
        return build_command(root, stage, args, _workspace_staged=True, **options)
    from .handlers import _sandboxed_build_command
    return _sandboxed_build_command(root, stage, args, **options)


def _synthetic_build_git(root, source, stage, private):
    """Create fresh objects from allowed inputs; original Git history stays host-only.

    Branch and nearest version tag are navigation hints. The resulting commit
    is an isolated build snapshot, never the original migration/source HEAD.
    """
    from .platform_runtime import capture_process
    from .workspace import engine_repository, git_command
    engine = engine_repository(root)
    if not (source / '.git').exists() and not (engine is not None and engine.exists()):
        return None
    environment = {key: value for key, value in os.environ.items()
                   if key.upper() in {'PATH', 'SYSTEMROOT', 'WINDIR', 'TEMP', 'TMP', 'TMPDIR', 'LANG', 'LC_ALL'}}
    environment.update(GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_SYSTEM=os.devnull,
                       GIT_CONFIG_NOSYSTEM='1', GIT_TERMINAL_PROMPT='0', GIT_ATTR_NOSYSTEM='1')
    deadline = time.monotonic() + INVENTORY_SECONDS
    flags = ['-c', 'core.hooksPath=' + os.devnull, '-c', 'core.fsmonitor=false',
             '-c', 'core.untrackedCache=false', '-c', 'credential.helper=',
             '-c', 'commit.gpgsign=false', '-c', 'user.name=ModPort',
             '-c', 'user.email=modport@localhost']
    def call(arguments, *, cwd, env, required=False):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError('isolated build Git preparation exceeded time bound')
        result = capture_process(arguments, cwd=cwd, environment=env,
                                 timeout=remaining, max_output_bytes=65536)
        if result.timed_out or result.drain_incomplete or (required and result.returncode):
            raise RuntimeError('isolated build Git preparation failed: '
                               + result.stderr.decode('utf-8', errors='replace')[-2000:])
        return result.stdout.decode('utf-8', errors='strict').strip() if result.returncode == 0 else None
    def original(*arguments):
        command, env = git_command(['git', *flags, *arguments], cwd=source,
                                    environment=environment, root=root)
        return call(command, cwd=source, env=env)
    branch = original('symbolic-ref', '--short', 'HEAD')
    tag = original('describe', '--tags', '--abbrev=0', 'HEAD')
    def valid_ref(value):
        return (value is not None and len(value) <= 255 and not any(ord(char) < 32 for char in value)
                and not value.startswith('-') and call(['git', 'check-ref-format', 'refs/heads/' + value],
                    cwd=private, env=environment) is not None)
    branch = branch if valid_ref(branch) else 'modport-build'
    tag = tag if valid_ref(tag) else None
    repository = private / 'project-git'
    call(['git', *flags, 'init', '--bare', '--template=', str(repository)],
         cwd=private, env=environment, required=True)
    environment.update(GIT_DIR=str(repository), GIT_WORK_TREE=str(stage), GIT_LITERAL_PATHSPECS='1')
    def synthetic(*arguments):
        return call(['git', *flags, *arguments], cwd=stage, env=environment, required=True)
    synthetic('config', 'core.bare', 'false')
    synthetic('symbolic-ref', 'HEAD', 'refs/heads/' + branch)
    (repository / 'info').mkdir(exist_ok=True)
    # Global info attributes outrank untrusted project filters/encoding drivers.
    atomic_write(repository / 'info' / 'attributes', b'* -text -filter -ident -working-tree-encoding\n')
    generated = {'.modport', '.gradle', 'build', 'out', 'node_modules', '.cache',
                 '.venv', 'venv', 'dist', 'target', '__pycache__'}
    ignored = sorted(SENSITIVE_NAMES | generated | {'.env', '.env.*'})
    atomic_write(repository / 'info' / 'exclude', ('\n'.join(ignored)
                 + '\n!.env.example\n!.env.sample\n!.env.template\n').encode())
    visible, _ = workspace_inventory(stage)
    batch, length = [], 0
    for relative, directory in visible.items():
        if directory or any(part.casefold() in generated for part in relative.parts):
            continue
        path = relative.as_posix()
        if batch and length + len(path) > 8000:
            synthetic('add', '-f', '--', *batch)
            batch, length = [], 0
        batch.append(path)
        length += len(path) + 1
    if batch:
        synthetic('add', '-f', '--', *batch)
    synthetic('commit', '--allow-empty', '--no-verify', '-m', 'Isolated ModPort build inputs')
    if tag:
        synthetic('tag', '--', tag)
    return repository


def prepare_external_build(root, worktree, args, **sandbox_kwargs):
    """Return a trusted isolated-build/sync launcher for the exact bound folder.

    Preparation stays in the caller so its original SDK budget authority and
    mutable Wrapper diagnostic handoff remain intact. Inner construction uses a
    Run-contained stage; it cannot recursively select the external workspace.
    """
    if not external_workspace(root, worktree):
        return None
    started = time.monotonic()
    windows = sandbox_kwargs.pop('_native_windows', os.name == 'nt')
    private = make_private_directory(Path(root).absolute() / 'toolchains' / 'local-build-stages' / uuid4().hex)
    stage, baseline = create_build_stage(Path(worktree), private)
    git_directory = _synthetic_build_git(root, Path(worktree), stage, private)
    destination = stage if windows else '/workspace'
    options = dict(sandbox_kwargs)
    if windows and git_directory is not None:
        options['_project_git'] = git_directory
    values = [convert_workspace_paths(value, worktree, destination, windows=windows) for value in args]
    options['environment'] = {key: convert_workspace_paths(value, worktree, destination, windows=windows)
                              for key, value in (options.get('environment') or {}).items()}
    if options.get('timeout_seconds') is not None:
        options['timeout_seconds'] -= time.monotonic() - started
        if options['timeout_seconds'] <= 0:
            raise TimeoutError('build deadline exhausted while preparing isolated workspace')
    native = _build_staged_command(root, stage, values, options, windows=windows)
    if not windows and git_directory is not None:
        boundary = native.index('--chdir') if '--chdir' in native else native.index('--')
        native[boundary:boundary] = ['--ro-bind', str(git_directory), '/project-git',
            '--setenv', 'GIT_DIR', '/project-git', '--setenv', 'GIT_WORK_TREE', '/workspace',
            '--setenv', 'GIT_CONFIG_GLOBAL', '/dev/null', '--setenv', 'GIT_CONFIG_SYSTEM', '/dev/null',
            '--setenv', 'GIT_CONFIG_NOSYSTEM', '1', '--setenv', 'GIT_ATTR_NOSYSTEM', '1',
            '--setenv', 'GIT_OPTIONAL_LOCKS', '0']
    timeout = options.get('timeout_seconds')
    if windows:
        timeout = float(native[native.index('--timeout') + 1])
    elif timeout is None:
        operation = options.get('operation')
        if operation is None:
            raise ValueError('external workspace build requires an active deadline')
        from .execution_budget import remaining_timeout
        deadline = operation.options.get('deadline_epoch')
        if not isinstance(deadline, (float, int)):
            raise ValueError('external workspace build requires a finite overall deadline')
        timeout = remaining_timeout(operation, max(0, deadline - time.time()))
    if timeout <= 0:
        raise TimeoutError('build deadline exhausted before isolated execution')
    request = {'root': str(Path(root).absolute()), 'source': str(Path(worktree).absolute()),
               'stage': str(stage), 'baseline': str(baseline), 'argv': native,
               'timeout_seconds': timeout, 'deadline_epoch': time.time() + timeout,
               'windows': windows}
    path = private / 'request.json'
    atomic_write(path, (json.dumps(request) + '\n').encode())
    source_root = Path(__file__).absolute().parents[1]
    launcher = (f'import sys; sys.path.insert(0, {str(source_root)!r}); '
                'from modport.local_workspace_sandbox import main; raise SystemExit(main())')
    return [sys.executable, '-I', '-c', launcher, '--request', str(path)]


def windows_stage_command(root, worktree, args, **options):
    return prepare_external_build(root, worktree, args, _native_windows=True, **options)


def main(argv=None):
    import argparse
    from .platform_runtime import capture_process
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--request', type=Path, required=True)
    arguments = parser.parse_args(argv)
    path = arguments.request.absolute()
    try:
        request = json.loads(_read_file(path.parent, path.name))
        root, source = Path(request['root']), Path(request['source'])
        stage, baseline = Path(request['stage']), Path(request['baseline'])
        if (not external_workspace(root, source)
                or path.parent.parent != root / 'toolchains' / 'local-build-stages'
                or stage != path.parent / 'workspace' or baseline != path.parent / 'baseline'):
            raise ValueError('build staging request does not match frozen local workspace')
        timeout = min(request['timeout_seconds'], request['deadline_epoch'] - time.time())
        if timeout <= 0:
            raise TimeoutError('build deadline exhausted before isolated execution')
        def output(name, data):
            stream = sys.stdout.buffer if name == 'stdout' else sys.stderr.buffer
            stream.write(data)
            stream.flush()
        result = capture_process(request['argv'], cwd=stage, timeout=timeout, on_chunk=output)
        if request['windows']:
            native = request['argv']
            spec_path = Path(native[native.index('--spec') + 1])
            lifecycle = json.loads((spec_path.parent / 'sandbox-lifecycle.json').read_text())
            if not lifecycle.get('process_cleanup_confirmed') or not lifecycle.get('permissions_cleanup_confirmed'):
                raise RuntimeError('native build cleanup unconfirmed; staged outputs retained without synchronization')
        if result.timed_out or result.drain_incomplete:
            raise RuntimeError('build output or cleanup incomplete; staged outputs retained without synchronization')
        sync_build_stage(source, stage, baseline)
        # Source outputs and caller-owned logs are now durable. Retain the
        # small invocation record, not two full project copies per command.
        for directory in (stage, baseline, path.parent / 'project-git'):
            if directory.exists():
                try:
                    shutil.rmtree(directory)
                except OSError as error:
                    print(f'local build temporary directory retained: {directory}: {error}', file=sys.stderr)
        return result.returncode or 0
    except (OSError, ValueError, RuntimeError) as exc:
        print(f'local workspace build isolation failed: {exc}', file=sys.stderr)
        return 69
