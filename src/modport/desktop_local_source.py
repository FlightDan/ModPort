"""Local folder intake, frozen Git source snapshots and developer workspaces.

Exclusions are exact names, not secret-content detection. Directory exclusions
are counted once without reading their contents. Current tracked, ignored and
untracked files otherwise enter the snapshot; original Git metadata never does.
"""
from __future__ import annotations

from contextlib import contextmanager
import os
from pathlib import Path
import re
import secrets
import shutil
import stat
import tempfile
import time

from .platform_files import (_open_posix_directory, assert_no_reparse, file_os, make_private_directory,
                             safe_open)
from .platform_runtime import capture_process

MAX_FILES = 20_000
MAX_ENTRIES = 40_000
MAX_FILE_BYTES = 64 * 1024 * 1024
MAX_TOTAL_BYTES = 1024 * 1024 * 1024
MAX_DEPTH = 64
MAX_DOCUMENT_BYTES = 512 * 1024
MAX_DOCUMENT_TOTAL_BYTES = 2 * 1024 * 1024
COPY_TIMEOUT_SECONDS = 60
DOCUMENT_PATHS = ('gradle.properties', 'build.gradle', 'build.gradle.kts',
                  'gradle/libs.versions.toml', 'src/main/resources/META-INF/mods.toml',
                  'src/main/resources/META-INF/neoforge.mods.toml')
EXCLUDED_NAMES = frozenset({
    '.git', '.gradle', 'build', 'out', 'node_modules',
    '.aws', '.ssh', '.codex', '.modport', '.kube', '.docker', '.azure',
    '.git-credentials', '.gitconfig', '.netrc', '.npmrc', '.pypirc', '.dockercfg',
    'credentials', 'credentials.json', 'auth.json', 'id_rsa', 'id_ed25519',
    'host-runtime', 'model-settings', 'source-snapshots',
    'kernel.sqlite3', 'kernel.sqlite3-wal', 'kernel.sqlite3-shm',
    'orchestrator.sqlite3', 'orchestrator.sqlite3-wal', 'orchestrator.sqlite3-shm',
    'desktop.sqlite3', 'desktop.sqlite3-wal', 'desktop.sqlite3-shm',
})
ENV_EXAMPLES = frozenset({'.env.example', '.env.sample', '.env.template'})
EXCLUDED_DIRECTORY_NAMES = frozenset({'run', 'runs', 'logs', '.venv', 'venv', '__pycache__', '.cache', 'target', 'dist'})
_READ_FLAGS = os.O_RDONLY | (os.O_NONBLOCK if os.name != 'nt' else 0)
_SNAPSHOT_ID = re.compile(r'desktop-[a-f0-9]{32}\Z')


def _excluded(name):
    lowered = name.lower()
    return lowered in EXCLUDED_NAMES or (lowered not in ENV_EXAMPLES
        and (lowered == '.env' or lowered.startswith('.env.')))


def _source_path(path, application_root):
    if not isinstance(path, str) or not path or len(path) > 4096 or any(ord(char) < 32 for char in path):
        raise ValueError('请选择有效的本地源码目录。')
    source = Path(path).expanduser()
    if not source.is_absolute():
        raise ValueError('本地源码目录必须是绝对路径。')
    source = Path(os.path.abspath(source))
    application = Path(os.path.abspath(Path(application_root).expanduser()))
    assert_no_reparse(source)
    if not source.is_dir():
        raise ValueError('本地源码路径必须是目录。')
    canonical = source.resolve(strict=True)
    if canonical != source:
        raise ValueError('本地源码路径不能包含符号链接或目录联接。')
    source = canonical
    if source == application or source in application.parents or application in source.parents:
        raise ValueError('源码目录不能与 ModPort 应用数据目录重叠。')
    broad = {Path(source.anchor), Path.home().absolute()}
    if os.name != 'nt':
        broad.update(Path(value) for value in ('/root', '/home', '/tmp', '/var', '/usr', '/etc', '/opt', '/mnt', '/media'))
    if source in broad:
        raise ValueError('请选择具体项目目录，不要选择磁盘根目录、用户目录或系统目录。')
    return source


@contextmanager
def _directory(path):
    # Keep Windows ancestors pinned while accessing children; POSIX opens every
    # component without following links. safe_open repeats anchored file checks.
    assert_no_reparse(path)
    descriptor = (_open_posix_directory(path) if os.name != 'nt' else
                  file_os.open(path, file_os.O_RDONLY | file_os.O_DIRECTORY | file_os.O_NOFOLLOW))
    try:
        yield descriptor
    finally:
        file_os.close(descriptor)


def inspect_local_source(path, *, application_root):
    source = _source_path(path, application_root)
    documents, warnings, total = {}, [], 0
    with _directory(source):
        for relative in DOCUMENT_PATHS:
            try:
                descriptor = safe_open(source, relative, _READ_FLAGS, allow_readonly_hardlinks=True)
            except FileNotFoundError:
                continue
            with os.fdopen(descriptor, 'rb') as stream:
                data = stream.read(MAX_DOCUMENT_BYTES + 1)
            if len(data) > MAX_DOCUMENT_BYTES or total + len(data) > MAX_DOCUMENT_TOTAL_BYTES:
                warnings.append(f'版本声明文件 {relative} 超出读取上限，请手动确认版本。')
                continue
            total += len(data)
            documents[relative] = data.decode('utf-8', errors='replace')
    warnings.append('启动时将保存当前文件的独立快照；实际工作目录由所选迁移模式决定，不会执行源码中的脚本。')
    return {'path': str(source), 'name': source.name, 'documents': documents,
            'warnings': warnings, 'git': _inspect_git(source)}


def _git_process(cwd, controls, arguments, *, deadline, overrides=()):
    essentials = {'PATH', 'SYSTEMROOT', 'WINDIR', 'TEMP', 'TMP', 'TMPDIR', 'LANG', 'LC_ALL'}
    environment = {key: value for key, value in os.environ.items() if key.upper() in essentials}
    environment.update({'HOME': str(controls), 'USERPROFILE': str(controls),
        'XDG_CONFIG_HOME': str(controls), 'GIT_CONFIG_GLOBAL': os.devnull,
        'GIT_CONFIG_SYSTEM': os.devnull, 'GIT_CONFIG_NOSYSTEM': '1',
        'GIT_ATTR_NOSYSTEM': '1', 'GIT_TERMINAL_PROMPT': '0', 'GIT_OPTIONAL_LOCKS': '0'})
    executable = shutil.which('git', path=environment.get('PATH', os.defpath))
    if not executable:
        raise ValueError('创建本地源码快照需要已安装的 Git。')
    # Resolve before changing cwd so a relative PATH entry cannot select a
    # different executable from the untrusted project folder.
    executable = str(Path(executable).absolute())
    settings = ('core.hooksPath=' + str(controls / 'no-hooks'), 'core.fsmonitor=false',
        'core.autocrlf=false', 'core.safecrlf=false', 'core.attributesFile=' + os.devnull,
        'core.untrackedCache=false', 'core.pager=', 'core.alternateRefsCommand=',
        'commit.gpgsign=false', 'tag.gpgsign=false', 'credential.helper=',
        'maintenance.auto=false', 'gc.auto=0', 'fetch.recurseSubmodules=false',
        'submodule.recurse=false', 'fetch.writeCommitGraph=false', 'user.name=ModPort',
        'user.email=modport@localhost', *overrides)
    prefix = [executable]
    for setting in settings:
        prefix.extend(('-c', setting))
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise ValueError('本地源码 Git 操作超过时间上限。')
    return capture_process([*prefix, *arguments], cwd=cwd, timeout=min(120, remaining),
                           environment=environment, max_output_bytes=4 * 1024 * 1024)


def _git(cwd, controls, *arguments, deadline, overrides=(), check=True):
    result = _git_process(cwd, controls, arguments, deadline=deadline, overrides=overrides)
    if result.timed_out or result.drain_incomplete or result.stdout_truncated:
        raise ValueError('本地源码 Git 操作超过时间上限或输出未能完整收集。')
    if check and result.returncode:
        detail = result.stderr.decode('utf-8', errors='replace').strip()[:1000]
        raise ValueError('本地源码 Git 操作失败：' + (detail or str(result.returncode)))
    return result


def _git_text(cwd, controls, *arguments, **options):
    return _git(cwd, controls, *arguments, **options).stdout.decode('utf-8', errors='replace').strip()


def _repository_overrides(source, controls, *, deadline):
    # Config enumeration reads values only. Disable every configured filter,
    # including values from include files, before any file-content inspection.
    data = _git_text(source, controls, 'config', '--null', '--list', deadline=deadline)
    filters = set()
    checkout = {}
    for record in data.split('\0'):
        key, _, value = record.partition('\n')
        if key.startswith('filter.') and key.rsplit('.', 1)[-1] in {'clean', 'smudge', 'process', 'required'}:
            filters.add(key.rsplit('.', 1)[0])
        if key in {'core.autocrlf', 'core.safecrlf'}:
            checkout[key] = value
    return (*tuple(setting for name in sorted(filters) for setting in (
        name + '.clean=', name + '.smudge=', name + '.process=', name + '.required=false')),
        *(key + '=' + value for key, value in sorted(checkout.items())))


def _tracked_paths(source, controls, *, deadline, overrides=()):
    paths = []
    data = _git_text(source, controls, 'ls-tree', '-r', '-z', 'HEAD', deadline=deadline, overrides=overrides)
    for entry in data.split('\0'):
        if not entry:
            continue
        description, relative = entry.split('\t', 1)
        mode, kind, _ = description.split()
        parts = Path(relative).parts
        if (mode not in {'100644', '100755'} or kind != 'blob' or not parts
                or Path(relative).is_absolute() or '..' in parts
                or any(ord(char) < 32 for char in relative)):
            raise ValueError('Git 提交包含子模块、符号链接或不支持的文件路径，请整理源码后重试。')
        if (any(_excluded(name) for name in parts)
                or any(name.lower() in EXCLUDED_DIRECTORY_NAMES for name in parts[:-1])):
            raise ValueError('Git 提交包含需排除的凭据、缓存或应用状态路径 ' + relative
                             + '；请选择复制/直接模式，或先从版本控制中移除该路径。')
        paths.append(relative)
    if not paths:
        raise ValueError('Git 提交中没有可用于迁移的源码文件。')
    return paths


def _has_nested_repository(source, *, deadline):
    count = 0
    def walk(folder, depth):
        nonlocal count
        if depth > MAX_DEPTH:
            raise ValueError('本地源码目录层级超过检查上限。')
        with _directory(folder) as directory, file_os.scandir(directory) as entries:
            for entry in entries:
                count += 1
                if count > MAX_ENTRIES or time.monotonic() >= deadline:
                    raise ValueError('本地源码目录超过 Git 检查上限。')
                if folder != source and entry.name.lower() == '.git':
                    return True
                if _excluded(entry.name) or entry.name.lower() in EXCLUDED_DIRECTORY_NAMES:
                    continue
                info = file_os.stat(entry.name, dir_fd=directory, follow_symlinks=False)
                if stat.S_ISDIR(info.st_mode) and walk(folder / entry.name, depth + 1):
                    return True
        return False
    return walk(source, 0)


def _inspect_git(source):
    value = {'available': bool(shutil.which('git')), 'is_repository': False,
             'root': None, 'branch': None, 'dirty': False, 'has_commits': False,
             'can_branch': False, 'reason': ''}
    if not value['available']:
        value['reason'] = '未找到 Git；安装 Git 后可创建源码快照和独立工作区。'
        return value
    deadline = time.monotonic() + COPY_TIMEOUT_SECONDS
    try:
        with tempfile.TemporaryDirectory(prefix='modport-git-inspect-') as temporary:
            controls = Path(temporary)
            marker = source / '.git'
            if marker.exists() or marker.is_symlink():
                assert_no_reparse(marker)
                if not marker.is_dir() and not marker.is_file():
                    raise ValueError('源码目录的 .git 不是有效的目录或文件。')
            probe = _git(source, controls, 'rev-parse', '--show-toplevel',
                         deadline=deadline, check=False)
            if probe.returncode:
                value['reason'] = ('所选目录的 Git 元数据无效；请修复仓库或选择复制/直接模式。'
                                   if marker.exists() else '所选目录不是 Git 仓库，可选择复制或直接模式。')
                return value
            root = Path(probe.stdout.decode('utf-8', errors='strict').strip()).absolute()
            value.update(is_repository=True, root=str(root))
            if root != source:
                value['reason'] = '请选择 Git 仓库根目录：' + str(root)
                return value
            for argument in ('--absolute-git-dir', '--git-common-dir'):
                metadata = Path(_git_text(source, controls, 'rev-parse', argument, deadline=deadline))
                if not metadata.is_absolute():
                    metadata = source / metadata
                assert_no_reparse(metadata.absolute())
            overrides = _repository_overrides(source, controls, deadline=deadline)
            head = _git(source, controls, 'rev-parse', '--verify', 'HEAD^{commit}',
                        deadline=deadline, overrides=overrides, check=False)
            value['has_commits'] = head.returncode == 0
            branch = _git(source, controls, 'symbolic-ref', '--quiet', '--short', 'HEAD',
                          deadline=deadline, overrides=overrides, check=False)
            value['branch'] = branch.stdout.decode('utf-8', errors='replace').strip() or None
            status = _git(source, controls, 'status', '--porcelain=v1', '-z', '--untracked-files=all',
                          '--ignore-submodules=all', deadline=deadline, overrides=overrides)
            value['dirty'] = bool(status.stdout)
            if not value['has_commits']:
                value['reason'] = 'Git 仓库尚无提交；请先提交源码，或选择复制/直接模式。'
            elif _has_nested_repository(source, deadline=deadline):
                value['reason'] = '项目包含嵌套 Git 仓库或子模块；请整理为单一源码目录，或选择复制/直接模式。'
            elif value['dirty']:
                value['reason'] = 'Git 工作区包含未提交修改或未跟踪文件；请先保存并提交，或选择复制/直接模式。'
            else:
                _tracked_paths(source, controls, deadline=deadline, overrides=overrides)
                value.update(can_branch=True, reason='可创建新分支和独立 Git 工作区。')
    except (OSError, ValueError, UnicodeError) as error:
        value['can_branch'] = False
        value['reason'] = '无法安全检查 Git 仓库：' + str(error)
    return value


def _copy_tree(source, destination, *, deadline, selected_paths=None):
    counts = {'files': 0, 'excluded': 0, 'bytes': 0, 'entries': 0}
    selected = set(selected_paths) if selected_paths is not None else None
    selected_directories = ({parent.as_posix() for value in selected for parent in Path(value).parents}
                            if selected is not None else None)

    def walk(relative, depth):
        if depth > MAX_DEPTH:
            raise ValueError('本地源码目录层级超过快照上限。')
        folder = source / relative
        with _directory(folder) as directory:
            with file_os.scandir(directory) as entries:
                for entry in entries:
                    if time.monotonic() >= deadline:
                        raise ValueError('本地源码快照超过时间上限，请减少不必要的大文件。')
                    counts['entries'] += 1
                    if counts['entries'] > MAX_ENTRIES:
                        raise ValueError('本地源码目录条目超过快照上限。')
                    name = entry.name
                    if _excluded(name):
                        counts['excluded'] += 1
                        continue
                    if any(ord(char) < 32 for char in name):
                        raise ValueError('源码文件名不能包含控制字符。')
                    child = relative / name
                    information = file_os.stat(name, dir_fd=directory, follow_symlinks=False)
                    if stat.S_ISDIR(information.st_mode):
                        if selected is not None and child.as_posix() not in selected_directories:
                            continue
                        if name.lower() in EXCLUDED_DIRECTORY_NAMES:
                            counts['excluded'] += 1
                            continue
                        target = destination / child
                        target.mkdir(mode=0o700)
                        walk(child, depth + 1)
                    elif stat.S_ISREG(information.st_mode):
                        if selected is not None and child.as_posix() not in selected:
                            continue
                        if counts['files'] >= MAX_FILES:
                            raise ValueError('本地源码文件数超过快照上限。')
                        descriptor = safe_open(source, child, _READ_FLAGS, allow_readonly_hardlinks=True)
                        with os.fdopen(descriptor, 'rb') as incoming:
                            before = os.fstat(incoming.fileno())
                            if before.st_size > MAX_FILE_BYTES or counts['bytes'] + before.st_size > MAX_TOTAL_BYTES:
                                raise ValueError('本地源码文件大小超过快照上限。')
                            mode = 0o755 if before.st_mode & 0o111 else 0o644
                            output = safe_open(destination, child, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
                            with os.fdopen(output, 'wb') as outgoing:
                                copied = 0
                                while True:
                                    data = incoming.read(1024 * 1024)
                                    if not data:
                                        break
                                    copied += len(data)
                                    if copied > MAX_FILE_BYTES or counts['bytes'] + copied > MAX_TOTAL_BYTES:
                                        raise ValueError('本地源码文件大小超过快照上限。')
                                    if time.monotonic() >= deadline:
                                        raise ValueError('本地源码快照超过时间上限。')
                                    outgoing.write(data)
                                outgoing.flush()
                                os.fsync(outgoing.fileno())
                            after = os.fstat(incoming.fileno())
                            if (before.st_size != copied or before.st_size != after.st_size
                                    or before.st_mtime_ns != after.st_mtime_ns):
                                raise ValueError('源码文件在复制期间发生变化，请保存文件后重新启动。')
                        counts['files'] += 1
                        counts['bytes'] += copied
                    else:
                        raise ValueError('源码目录包含符号链接、目录联接或非常规文件，请移除后重试。')

    walk(Path(), 0)
    if not counts['files']:
        raise ValueError('源码目录中没有可用于快照的文件。')
    return counts


def _git_snapshot(stage, controls, *, deadline, history_source=None, history_commit=None, committed_head=False):
    def git(*arguments):
        return _git_text(stage, controls, *arguments, deadline=deadline)

    template = controls / 'empty-template'
    template.mkdir()
    initialization = ['init', '--quiet', '--template=' + str(template), '--initial-branch=modport-source']
    if history_source is not None:
        overrides = _repository_overrides(history_source, controls, deadline=deadline)
        object_format = _git_text(history_source, controls, 'rev-parse', '--show-object-format',
                                  deadline=deadline, overrides=overrides)
        if object_format not in {'sha1', 'sha256'}:
            raise ValueError('Git 仓库对象格式不受支持。')
        initialization.append('--object-format=' + object_format)
        observed = _git_text(history_source, controls, 'rev-parse', '--verify', 'HEAD^{commit}',
                             deadline=deadline, overrides=overrides)
        if observed != history_commit:
            raise ValueError('原仓库提交在准备期间发生变化，请重新启动。')
        # Bundle creation is a builtin Git operation; unlike a local fetch it
        # does not launch an upload-pack configured by the source repository.
        bundle = controls / 'parent-history.bundle'
        _git(history_source, controls, 'bundle', 'create', str(bundle), 'HEAD',
             deadline=deadline, overrides=overrides)
    git(*initialization, '.')
    # The host override prevents .gitattributes clean filters, encoding, ident
    # expansion and EOL normalization from altering the selected current bytes.
    (stage / '.git' / 'info').mkdir(exist_ok=True)
    (stage / '.git' / 'info' / 'attributes').write_text('* -text -filter -ident -working-tree-encoding\n', encoding='utf-8')
    if history_source is not None:
        git('bundle', 'unbundle', str(bundle))
        git('update-ref', 'refs/heads/modport-source', history_commit)
        git('read-tree', history_commit)
    if not committed_head:
        git('add', '--force', '--all', '--', '.')
        git('commit', '--quiet', '--allow-empty', '--no-verify', '-m', 'Snapshot current local source files')
    commit = git('rev-parse', '--verify', 'HEAD')
    if not re.fullmatch(r'[0-9a-f]{40,64}', commit):
        raise ValueError('Git 未返回有效的源码提交标识。')
    return commit


def prepare_local_source(path, *, application_root, snapshot_id, history_source=None, history_commit=None,
                         committed_head=False):
    source = _source_path(path, application_root)
    if (history_source is None) != (history_commit is None):
        raise ValueError('源码快照的 Git 历史与父提交必须同时提供。')
    if committed_head and history_source is None:
        raise ValueError('基于当前提交的源码快照需要提供原仓库历史。')
    if history_source is not None:
        history_source = Path(history_source).absolute()
        if history_source != source or not re.fullmatch(r'[0-9a-f]{40,64}', str(history_commit)):
            raise ValueError('源码快照只能保留所选原仓库的有效父提交。')
    if not isinstance(snapshot_id, str) or not _SNAPSHOT_ID.fullmatch(snapshot_id):
        raise ValueError('无效的本地源码快照标识。')
    snapshots = make_private_directory(Path(application_root).expanduser().absolute() / 'source-snapshots')
    final = snapshots / snapshot_id
    if final.exists() or final.is_symlink():
        raise ValueError('该源码快照已经存在，不能覆盖或重新复制原目录。')
    stage_name = '.staging-' + snapshot_id + '-' + secrets.token_hex(6)
    stage = snapshots / stage_name
    controls = snapshots / (stage_name + '-controls')
    deadline = time.monotonic() + COPY_TIMEOUT_SECONDS
    with _directory(snapshots) as parent:
        file_os.mkdir(stage_name, mode=0o700, dir_fd=parent)
        file_os.mkdir(controls.name, mode=0o700, dir_fd=parent)
        try:
            counts = _copy_tree(source, stage, deadline=deadline)
            commit = _git_snapshot(stage, controls, deadline=deadline,
                                   history_source=history_source, history_commit=history_commit,
                                   committed_head=committed_head)
            file_os.rename(stage_name, snapshot_id, src_dir_fd=parent, dst_dir_fd=parent)
            file_os.fsync(parent)
        except BaseException:
            shutil.rmtree(stage, ignore_errors=True)
            raise
        finally:
            shutil.rmtree(controls, ignore_errors=True)
    warnings = ['已保存当前文件的独立源码快照，原目录及其 Git 历史未被修改。',
                '仅按明确文件名排除缓存、凭据和应用状态；没有检测其余文件是否含有敏感信息。']
    if counts['excluded']:
        warnings.append(f"已排除 {counts['excluded']} 个命名文件或目录；目录内容未读取，按入口计数。")
    return {'source_repository': final.as_uri(), 'source_revision': commit,
            'original_path': str(source), 'snapshot_path': str(final),
            'file_count': counts['files'], 'excluded_count': counts['excluded'], 'warnings': warnings}


def _new_workspace_path(source, snapshot_id):
    return source.with_name(source.name + '-modport-' + snapshot_id[8:20] + '-' + secrets.token_hex(4))


def prepare_local_workspace(path, *, application_root, snapshot_id, mode, branch=None):
    """Freeze source and prepare the actual developer folder, without running project code.

    Git bookkeeping additions belong only to ``git_worktree``. Direct mode never
    writes the selected folder. Its later migration edits are owned by the host.
    Published source snapshots survive workspace preparation failures as evidence.
    """
    source = _source_path(path, application_root)
    if mode not in {'git_worktree', 'copy', 'direct'}:
        raise ValueError('请选择独立 Git 工作区、复制目录或直接修改模式。')
    if mode != 'git_worktree' and branch is not None:
        raise ValueError('只有独立 Git 工作区模式可以指定新分支。')
    if not isinstance(snapshot_id, str) or not _SNAPSHOT_ID.fullmatch(snapshot_id):
        raise ValueError('无效的本地源码快照标识。')
    if mode == 'git_worktree':
        inspection = _inspect_git(source)
        if not inspection['can_branch']:
            raise ValueError(inspection['reason'])
        if (not isinstance(branch, str) or not branch or len(branch) > 240
                or branch != branch.strip() or any(ord(char) < 32 for char in branch)
                or branch.startswith('-') or branch == 'HEAD'):
            raise ValueError('请输入有效的新 Git 分支名称。')
    destination = source if mode == 'direct' else _new_workspace_path(source, snapshot_id)
    # Validate before any output: the sibling may coincide with an application
    # root supplied by a caller, even though the selected project does not.
    application = Path(application_root).expanduser().absolute()
    if destination == application or application in destination.parents or destination in application.parents:
        raise ValueError('迁移工作目录不能与 ModPort 应用数据目录重叠。')
    if destination != source and (destination.exists() or destination.is_symlink()):
        raise ValueError('迁移工作目录已经存在；不会覆盖，请重新选择。')
    with tempfile.TemporaryDirectory(prefix='modport-workspace-') as temporary:
        controls = Path(temporary)
        deadline = time.monotonic() + COPY_TIMEOUT_SECONDS
        overrides = ()
        parent_commit = None
        tracked = None
        if mode == 'git_worktree':
            overrides = _repository_overrides(source, controls, deadline=deadline)
            normalized = _git_text(source, controls, 'check-ref-format', '--branch', branch,
                                   deadline=deadline, overrides=overrides)
            if normalized != branch:
                raise ValueError('请输入明确的新分支名称，不支持分支切换表达式。')
            existing = _git(source, controls, 'show-ref', '--verify', '--quiet', 'refs/heads/' + branch,
                            deadline=deadline, overrides=overrides, check=False)
            if existing.returncode == 0:
                raise ValueError('该 Git 分支已经存在；请输入尚未使用的新分支名称。')
            if existing.returncode != 1:
                raise ValueError('无法检查新 Git 分支是否可用。')
            parent_commit = _git_text(source, controls, 'rev-parse', '--verify', 'HEAD^{commit}',
                                      deadline=deadline, overrides=overrides)
            tracked = _tracked_paths(source, controls, deadline=deadline, overrides=overrides)
        details = prepare_local_source(str(source), application_root=application_root,
            snapshot_id=snapshot_id, history_source=source if parent_commit else None,
            history_commit=parent_commit, committed_head=mode == 'git_worktree')
        details['source_snapshot'] = mode != 'git_worktree'
        if mode == 'direct':
            details['workspace'] = {'mode': mode, 'path': str(source), 'original_path': str(source)}
            details['warnings'].append('直接修改模式将在迁移期间修改原目录文件；源码快照独立保留。')
            return details
        snapshot = Path(details['snapshot_path'])
        deadline = time.monotonic() + COPY_TIMEOUT_SECONDS
        owned_folder = False
        worktree_added = False
        branch_created = False
        try:
            if mode == 'copy':
                destination.mkdir(mode=0o700)
                owned_folder = True
            else:
                latest = _inspect_git(source)
                observed = _git_text(source, controls, 'rev-parse', '--verify', 'HEAD^{commit}',
                                     deadline=deadline, overrides=overrides)
                if not latest['can_branch'] or observed != parent_commit:
                    raise ValueError('原仓库在准备期间发生变化，请检查并重新启动。')
                # Bundle import uses no transports or URL rewriting and does
                # not change FETCH_HEAD, the original branch, index or files.
                bundle = controls / 'workspace-history.bundle'
                _git(snapshot, controls, 'bundle', 'create', str(bundle), 'HEAD', deadline=deadline)
                _git(source, controls, 'bundle', 'unbundle', str(bundle),
                     deadline=deadline, overrides=overrides)
                destination.mkdir(mode=0o700)
                owned_folder = True
                # The empty expected old value atomically requires a NEW ref.
                # Separate ownership also permits safe cleanup if add fails.
                _git(source, controls, 'update-ref', 'refs/heads/' + branch,
                     details['source_revision'], '', deadline=deadline, overrides=overrides)
                branch_created = True
                _git(source, controls, 'worktree', 'add', '--no-checkout',
                     str(destination), branch, deadline=deadline, overrides=overrides)
                worktree_added = True
            _copy_tree(snapshot, destination, deadline=time.monotonic() + COPY_TIMEOUT_SECONDS,
                       selected_paths=tracked)
            if mode == 'git_worktree':
                _git(destination, controls, 'read-tree', '--reset', details['source_revision'],
                     deadline=time.monotonic() + COPY_TIMEOUT_SECONDS, overrides=overrides)
            details['workspace'] = {'mode': mode, 'path': str(destination), 'original_path': str(source)}
            if mode == 'git_worktree':
                details['workspace']['branch'] = branch
                details['workspace']['git_directory'] = _git_text(destination, controls,
                    'rev-parse', '--absolute-git-dir', deadline=time.monotonic() + 15,
                    overrides=overrides)
                details['warnings'].append('已基于原仓库当前提交创建独立 Git 工作区和新分支，仅包含受版本控制的源码；原目录文件、当前分支和索引保持不变。')
            else:
                details['warnings'].append('迁移将在独立复制目录中进行；原目录保持不变。')
            return details
        except BaseException as error:
            cleanup = []
            if branch_created:
                try:
                    listing = _git_text(source, controls, 'worktree', 'list', '--porcelain',
                                        deadline=time.monotonic() + 15, overrides=overrides)
                    worktree_added = 'worktree ' + str(destination) in listing.splitlines()
                    if worktree_added:
                        _git(source, controls, 'worktree', 'remove', '--force', str(destination),
                             deadline=time.monotonic() + 15, overrides=overrides)
                        owned_folder = False
                    listing = _git_text(source, controls, 'worktree', 'list', '--porcelain',
                                        deadline=time.monotonic() + 15, overrides=overrides)
                    current = _git_text(source, controls, 'rev-parse', '--verify', 'refs/heads/' + branch,
                                        deadline=time.monotonic() + 15, overrides=overrides)
                    if (current == details['source_revision']
                            and 'branch refs/heads/' + branch not in listing.splitlines()):
                        _git(source, controls, 'update-ref', '-d', 'refs/heads/' + branch, current,
                             deadline=time.monotonic() + 15, overrides=overrides)
                    else:
                        cleanup.append('新分支已被其他操作修改或使用，已保留：' + branch)
                except (OSError, ValueError) as failure:
                    cleanup.append(str(failure))
            if owned_folder and not worktree_added:
                shutil.rmtree(destination)
            if cleanup:
                raise ValueError(f'{error}；新工作区清理未确认：' + '; '.join(cleanup)
                                 + '；源码快照保留于 ' + str(snapshot)) from error
            raise
