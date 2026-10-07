"""Bounded observations of useful work; SDK authority and policy live in the driver.

Content snapshots are compared directly. Tool activity and model/log writes are
context, not proof of progress or acceptance. Only this module's two replaceable
snapshot slots are written; original execution evidence is never altered.
"""

from __future__ import annotations

import base64
import heapq
import json
from .platform_files import file_os as os
from pathlib import Path
import shlex
import stat
import time
from typing import Any, Mapping
from urllib.parse import quote
from uuid import uuid4

from .telemetry import _secrets, redact
from .workspace import is_project_workspace, project_path, project_relative
from .local_workspace_sandbox import is_sensitive_name


MAX_FILES = 1024
MAX_ENTRIES = 8192
MAX_FILE_BYTES = 256 * 1024
MAX_CONTENT_BYTES = 2 * 1024 * 1024
MAX_RECEIPTS = 128
MAX_RECEIPT_BYTES = 512 * 1024
MAX_RECEIPT_TOTAL_BYTES = 4 * 1024 * 1024
MAX_CASES = 2048
MAX_SNAPSHOT_BYTES = 4 * 1024 * 1024
MAX_EXCERPT_BYTES = 16 * 1024
MAX_EXCERPTS = 16
_IGNORED_DIRS = frozenset({
    '.git', '.gradle', '.idea', '__pycache__', 'node_modules', '.venv',
    'build', 'dist', 'target', 'out', 'logs', 'run', 'runs', 'cache', 'caches',
    'artifacts', 'test-results', 'reports',
})
_SOURCE_SUFFIXES = frozenset({
    '.java', '.kt', '.kts', '.scala', '.groovy', '.gradle', '.properties',
    '.toml', '.json', '.yaml', '.yml', '.xml', '.mcmeta', '.cfg', '.conf',
    '.py', '.js', '.ts', '.tsx', '.sh', '.bat', '.ps1',
})
_PROTOCOL_FILES = frozenset({
    'functional-contract.json', 'characterization.init.gradle',
    'artifact-verification.init.gradle', 'goal.md', 'goals.md', 'goal.json',
})


def _root(value: str | Path) -> Path:
    root = Path(value).absolute()
    if not root.is_dir() or root.is_symlink() or root.resolve() != root:
        raise ValueError('progress root must be a real contained directory')
    return root


def _contained(root: Path, relative: str | Path) -> Path:
    relative = Path(relative)
    if relative.is_absolute() or not relative.parts or '..' in relative.parts:
        raise ValueError('unsafe progress evidence path')
    path = root / relative
    current = root
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise ValueError('symlink in progress evidence path')
    if path.resolve().is_relative_to(root):
        return path
    raise ValueError('progress evidence path escapes root')


def _directory_fd(root: Path, relative: Path, *, create=False) -> int:
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    descriptor = os.open(root, flags)
    try:
        for part in relative.parts:
            if part in {'.', '..'}:
                raise ValueError('unsafe progress directory')
            if create:
                try:
                    os.mkdir(part, dir_fd=descriptor)
                except FileExistsError:
                    pass
            child = os.open(part, flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _read(path: Path, limit: int, *, root: Path) -> bytes:
    relative = path.relative_to(root)
    directory = _directory_fd(root, relative.parent)
    try:
        descriptor = os.open(relative.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                             dir_fd=directory)
    finally:
        os.close(directory)
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_size > limit:
            raise ValueError('file is not regular or exceeds byte limit')
        blocks = []
        size = 0
        while block := os.read(descriptor, min(65536, limit + 1 - size)):
            size += len(block)
            if size > limit:
                raise ValueError('file exceeds byte limit')
            blocks.append(block)
        after = os.fstat(descriptor)
        if (before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
                after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns):
            raise ValueError('file changed during observation')
        return b''.join(blocks)
    finally:
        os.close(descriptor)


def _body(command: Any) -> Mapping[str, Any]:
    if isinstance(command, Mapping):
        body = command.get('payload')
        # The driver normally supplies a hydrated OperationInput.
        if isinstance(body, Mapping) and 'command_id' in body:
            return body
        return command
    return {name: getattr(command, name, None) for name in (
        'run_id', 'task_id', 'stage_id', 'command_id', 'attempt',
        'options', 'artifact_refs', 'payload')}


def _workspace(body: Mapping[str, Any]) -> str:
    options = body.get('options') or {}
    workspace = options.get('workspace') if isinstance(options, Mapping) else None
    if isinstance(workspace, str) and workspace:
        return workspace
    stage = body.get('stage_id', '')
    return ('baseline' if str(stage).startswith('contract_')
            or stage == 'baseline_build' else 'worktree')


def _workspace_path(root: Path, relative: str) -> tuple[Path, str]:
    """Resolve a logical project workspace while keeping observations logical."""
    if (not isinstance(relative, str) or not relative or "\\" in relative
            or "\x00" in relative):
        raise ValueError('unsafe progress workspace path')
    logical = Path(relative)
    if logical.is_absolute() or '..' in logical.parts or '.' in logical.parts:
        raise ValueError('unsafe progress workspace path')
    path = project_path(root, logical)
    if path.resolve() != path.absolute() or not is_project_workspace(root, path):
        raise ValueError('progress workspace is outside the registered project')
    normalized = project_relative(root, path).as_posix()
    if normalized != logical.as_posix():
        raise ValueError('progress workspace does not match its logical path')
    return path, normalized


def _sensitive_name(name: str) -> bool:
    return is_sensitive_name(name)


def _is_content(relative: Path) -> bool:
    parts = relative.parts
    if '.modport' in parts:
        index = parts.index('.modport')
        tail = parts[index + 1:]
        return bool(tail and (tail[0] in {'harness', 'tests', 'goals'}
                             or len(tail) == 1 and tail[0] in _PROTOCOL_FILES))
    if relative.name in {'gradlew', 'gradlew.bat', 'settings.gradle', 'build.gradle'}:
        return True
    if relative.suffix.lower() in {'.json', '.xml', '.yaml', '.yml'}:
        return (any(part in {'src', 'resources', 'config', 'gradle', 'harness'} for part in parts)
                or relative.name in {'package.json', 'tsconfig.json', 'pom.xml'})
    return relative.suffix.lower() in _SOURCE_SUFFIXES or 'resources' in parts


def _walk(root: Path, directory: Path, limitations: list[str]):
    """Bound visited entries as well as selected files, without following links."""
    stack = [directory]
    visited = 0
    while stack:
        parent = stack.pop()
        try:
            relative_parent = parent.relative_to(root)
            if relative_parent.parts:
                _contained(root, relative_parent)
            elif parent.resolve() != root:
                raise ValueError('progress scan root changed during observation')
            with os.scandir(parent) as entries:
                selected = []
                for entry in entries:
                    visited += 1
                    if visited > MAX_ENTRIES:
                        limitations.append('directory entry limit reached')
                        return
                    selected.append(entry)
            for entry in sorted(selected, key=lambda item: item.name):
                path = Path(entry.path)
                if entry.is_symlink():
                    limitations.append('symlink omitted: ' + str(path.relative_to(root)))
                elif entry.is_dir(follow_symlinks=False):
                    protocol = '.modport' in path.parts
                    allowed_protocol = (not protocol or entry.name == '.modport'
                                        or path.parts[path.parts.index('.modport') + 1] in {'harness', 'tests', 'goals'})
                    if (entry.name.casefold() not in _IGNORED_DIRS
                            and not _sensitive_name(entry.name)
                            and (not protocol or allowed_protocol)):
                        stack.append(path)
                elif entry.is_file(follow_symlinks=False):
                    if _sensitive_name(entry.name):
                        limitations.append('sensitive entry omitted: ' + str(path.relative_to(root)))
                    else:
                        yield path
        except (OSError, ValueError) as exc:
            limitations.append('directory unavailable: ' + str(parent.relative_to(root))
                               + ': ' + str(exc)[:120])


def _case_rows(document: Mapping[str, Any]):
    bodies = [document]
    response = document.get('response')
    if isinstance(response, Mapping):
        bodies.append(response)
    for body in tuple(bodies):
        outputs = body.get('outputs')
        if isinstance(outputs, Mapping):
            bodies.append(outputs)
    for body in bodies:
        cases = body.get('case_results')
        if isinstance(cases, Mapping):
            for test_id, row in cases.items():
                if isinstance(row, Mapping):
                    yield str(test_id), row.get('status'), row.get('test_outcome')
        if body.get('receipt_type') == 'modport.characterization_verification.v1':
            results = body.get('test_results')
            if isinstance(results, list):
                for row in results:
                    if isinstance(row, Mapping) and isinstance(row.get('test_id'), str):
                        yield row['test_id'], row.get('outcome'), row.get('outcome')


def _redacted_text(text: str, secrets) -> str:
    lines = []
    for line in text.splitlines():
        try:
            value = json.loads(line)
        except ValueError:
            lines.append(redact(line, secrets))
        else:
            lines.append(json.dumps(redact(value, secrets), ensure_ascii=False))
    return '\n'.join(lines)


def _log_excerpt(root: Path, relative: str, secrets) -> dict[str, Any]:
    path = _contained(root, relative)
    directory = _directory_fd(root, path.relative_to(root).parent)
    try:
        descriptor = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                             dir_fd=directory)
    finally:
        os.close(directory)
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise ValueError('log is not a regular file')
        offset = max(0, before.st_size - MAX_EXCERPT_BYTES)
        os.lseek(descriptor, offset, os.SEEK_SET)
        raw = os.read(descriptor, MAX_EXCERPT_BYTES)
        # A partial JSON line might lose its private-reasoning type or secret
        # key. Keep complete lines when observing the tail of structured logs.
        if offset and relative.endswith('.log'):
            separator = raw.find(b'\n')
            if separator >= 0:
                offset += separator + 1
                raw = raw[separator + 1:]
            else:
                raw = b''
        after = os.fstat(descriptor)
        return {'origin_path': relative, 'origin_mtime': before.st_mtime,
                'origin_size': before.st_size, 'sampled_at': time.time(),
                'offset': offset, 'sample_bytes': len(raw),
                'truncated': offset > 0 or len(raw) < before.st_size,
                'context_only': True,
                'changed_during_capture': before.st_size != after.st_size
                    or before.st_mtime_ns != after.st_mtime_ns,
                'text': _redacted_text(raw.decode('utf-8', errors='replace'), secrets)}
    finally:
        os.close(descriptor)


def _tool_context(document, path, execution_id, workspace, compile_session):
    """Recognize specific host tools, never arbitrary successful shell output."""
    context = None
    if (path.name.startswith('artifact-compile-') and path.name != 'artifact-compile-session.json'
            and document.get('acceptance_evidence') is False
            and document.get('tasks') == ['compileJava', 'compileTestJava']
            and compile_session.get('command_id') == execution_id
            and compile_session.get('kind') == 'artifact_compile'):
        operation = compile_session.get('operation')
        options = operation.get('options') if isinstance(operation, Mapping) else None
        init_script = options.get('artifact_init_script') if isinstance(options, Mapping) else None
        if init_script is not None and (not isinstance(init_script, str) or len(init_script) > 512):
            return None
        context = {'kind': 'artifact_harness_compile', 'workspace': workspace,
                   'tasks': document['tasks'],
                   'input': init_script}
        key = 'artifact_harness_compile'
    elif (path.parent.name == 'opencode-shell'
          and document.get('request_command_id') == execution_id
          and document.get('workspace') == workspace
          and document.get('command_truncated') is False
          and isinstance(document.get('command_redacted'), str)):
        command = document['command_redacted']
        if len(command.encode('utf-8')) > 2048:
            return None
        try:
            arguments = shlex.split(command)
        except ValueError:
            return None
        if arguments and arguments[0] in {'bash', 'sh'}:
            arguments = arguments[1:]
        if not arguments or arguments[0] not in {'./gradlew', 'gradlew', 'gradle'}:
            return None
        tasks = [value for value in arguments[1:] if not value.startswith('-')]
        allowed = {'compileJava', 'compileTestJava', 'compileKotlin', 'compileTestKotlin'}
        if not tasks or any(value.rsplit(':', 1)[-1] not in allowed for value in tasks):
            return None
        context = {'kind': 'project_diagnostic_compile', 'workspace': workspace,
                   'tasks': tasks, 'input': command}
        key = 'project_compile:' + command
    else:
        return None
    exit_code = document.get('exit_code')
    state = ('passed' if type(exit_code) is int and exit_code == 0
             and document.get('timed_out') is not True else
             'failed' if type(exit_code) is int and exit_code != 0
             or document.get('timed_out') is True else 'unknown')
    return key, {'context': context, 'status': state, 'path': path.as_posix()}


def capture_progress(root, command, previous_snapshot=None) -> dict[str, Any]:
    """Capture content and existing receipts; never execute or authenticate tests.

    ``previous_snapshot`` is accepted for driver convenience; comparisons remain
    explicit through ``compare_progress``. An incomplete scan never proves a
    deletion. Receipt outcomes are observations, not new acceptance evidence.
    """
    root = _root(root)
    body = _body(command)
    identity = {name: body.get(name) for name in (
        'run_id', 'task_id', 'stage_id', 'command_id', 'attempt')}
    execution_id = identity.get('command_id')
    if not isinstance(execution_id, str) or not execution_id or len(execution_id) > 240:
        raise ValueError('progress capture requires an execution identity')
    limitations: list[str] = []
    workspace = _workspace(body)
    content: dict[str, str] = {}
    total = 0
    try:
        source, workspace = _workspace_path(root, workspace)
        if not source.is_dir():
            raise ValueError('workspace is missing')
        for path in _walk(source, source, limitations):
            relative = path.relative_to(source)
            if not _is_content(relative):
                continue
            if len(content) >= MAX_FILES:
                limitations.append('content file limit reached')
                break
            logical_path = project_relative(root, path).as_posix()
            try:
                raw = _read(path, MAX_FILE_BYTES, root=source)
                if total + len(raw) > MAX_CONTENT_BYTES:
                    limitations.append('content byte limit reached')
                    break
                content[logical_path] = base64.b64encode(raw).decode('ascii')
                total += len(raw)
            except (OSError, ValueError) as exc:
                limitations.append('content omitted: ' + logical_path + ': ' + str(exc)[:120])
    except (OSError, ValueError) as exc:
        limitations.append('workspace unavailable: ' + str(exc)[:160])
    evidence_paths: list[str] = []
    # Explicit goal documents may be stored outside the assigned workspace.
    refs = body.get('artifact_refs') or {}
    if isinstance(refs, Mapping):
        for alias, ref in list(refs.items())[:64]:
            relative = ref.get('path') if isinstance(ref, Mapping) else None
            if isinstance(relative, str):
                try:
                    path = _contained(root, relative)
                    if Path(relative).parts[0] == 'artifacts' and path.is_file():
                        evidence_paths.append(relative)
                    if 'goal' in str(alias).lower() and relative not in content:
                        raw = _read(path, MAX_FILE_BYTES, root=root)
                        if len(content) >= MAX_FILES or total + len(raw) > MAX_CONTENT_BYTES:
                            limitations.append('goal content limit reached')
                        else:
                            content[relative] = base64.b64encode(raw).decode('ascii')
                            total += len(raw)
                except (OSError, ValueError):
                    limitations.append('artifact reference unavailable: ' + relative[:240])
    content_complete = not limitations
    activity: list[dict[str, Any]] = []
    cases: dict[str, dict[str, Any]] = {}
    diagnostic_excerpts: list[dict[str, Any]] = []
    tool_outcomes: dict[str, dict[str, Any]] = {}
    secrets = _secrets()
    directory_relative = 'artifacts/executions/' + execution_id
    options = body.get('options') or {}
    assignment = options.get('agent_assignment') if isinstance(options, Mapping) else None
    task_id = body.get('task_id')
    if (type(assignment) is int and assignment > 0 and isinstance(task_id, str)
            and '/' not in task_id and '\\' not in task_id and len(task_id) <= 200):
        for suffix in ('.log', '.txt', '.plan.log'):
            relative = f'logs/agent-{task_id}-{assignment}{suffix}'
            try:
                path = _contained(root, relative)
                if path.exists():
                    diagnostic_excerpts.append(_log_excerpt(root, relative, secrets))
            except (OSError, ValueError) as exc:
                limitations.append('assignment log excerpt unavailable: ' + relative + ': ' + str(exc)[:120])
        if not diagnostic_excerpts:
            limitations.append('assignment log output not observed; live transport output may be unavailable')
    compile_session = {}
    for name in ('input.json', 'artifact-compile-session.json'):
        relative = directory_relative + '/' + name
        try:
            path = _contained(root, relative)
            if path.is_file():
                evidence_paths.append(relative)
                if name == 'artifact-compile-session.json':
                    document = json.loads(_read(path, MAX_RECEIPT_BYTES, root=root))
                    if (isinstance(document, Mapping)
                            and document.get('workspace') == str(source)):
                        compile_session = document
        except (OSError, ValueError, TypeError) as exc:
            limitations.append('execution input context unavailable: ' + str(exc)[:120])
    try:
        directory = _contained(root, directory_relative)
        receipt_paths = []
        receipt_count = 0
        if directory.is_dir():
            for path in _walk(root, directory, limitations):
                if path.suffix == '.json' and path.name not in {'input.json', 'session.json'}:
                    receipt_count += 1
                    try:
                        entry = (path.stat().st_mtime_ns, path.as_posix(), path)
                        if len(receipt_paths) < MAX_RECEIPTS:
                            heapq.heappush(receipt_paths, entry)
                        elif entry > receipt_paths[0]:
                            heapq.heapreplace(receipt_paths, entry)
                    except OSError:
                        limitations.append('receipt disappeared during observation')
        if receipt_count > MAX_RECEIPTS:
            limitations.append('receipt file limit reached; newest receipts selected')
        receipt_bytes = 0
        for _, _, path in sorted(receipt_paths, reverse=True):
            relative = path.relative_to(root).as_posix()
            try:
                raw = _read(_contained(root, relative), MAX_RECEIPT_BYTES, root=root)
                receipt_bytes += len(raw)
                if receipt_bytes > MAX_RECEIPT_TOTAL_BYTES:
                    limitations.append('receipt byte limit reached')
                    break
                document = json.loads(raw)
                if not isinstance(document, Mapping):
                    continue
                host_report = path.name in {
                    'receipt.json', 'baseline-contract-tests.json', 'verification-report.json'}
                host_tool = (document.get('receipt_type') == 'modport.characterization_verification.v1'
                             and document.get('command_id') == execution_id
                             and path.parent.name == 'opencode-characterization')
                observed = list(_case_rows(document)) if host_report or host_tool else []
                kind = document.get('receipt_type') or document.get('tool')
                tool = _tool_context(redact(document, secrets), Path(relative),
                                     execution_id, workspace, compile_session)
                if tool is not None and tool[0] not in tool_outcomes and len(tool_outcomes) < 16:
                    tool_outcomes[tool[0]] = tool[1]
                if observed or kind or 'exit_code' in document or 'returncode' in document:
                    evidence_paths.append(relative)
                    if len(activity) < 16:
                        activity.append({'path': relative, 'kind': str(kind or 'tool_result')[:120],
                                         'status': str(document.get('outcome', document.get('status', 'observed')))[:80],
                                         'exit_code': document.get('exit_code'),
                                         'useful_work_classification': ('registered_compile_transition'
                                                                      if tool else 'context_only_unknown')})
                    if len(diagnostic_excerpts) < MAX_EXCERPTS:
                        streams = {name: document[name] for name in ('stdout', 'stderr', 'error', 'detail')
                                   if isinstance(document.get(name), str)}
                        if streams:
                            text = '\n'.join(name + ': ' + value.encode('utf-8')[-MAX_EXCERPT_BYTES // 4:]
                                             .decode('utf-8', errors='replace')
                                             for name, value in streams.items())
                            metadata = path.stat()
                            diagnostic_excerpts.append({
                                'origin_path': relative, 'origin_mtime': metadata.st_mtime,
                                'origin_size': len(raw), 'sampled_at': time.time(),
                                'truncated': any(len(value.encode('utf-8')) > MAX_EXCERPT_BYTES // 4
                                                 for value in streams.values())
                                    or document.get('stdout_truncated') is True
                                    or document.get('stderr_truncated') is True,
                                'text': _redacted_text(text, secrets),
                                'fields': list(streams), 'context_only': True})
                for test_id, status, outcome in observed:
                    if test_id in cases:
                        continue
                    if test_id not in cases and len(cases) >= MAX_CASES:
                        limitations.append('verification case limit reached')
                        break
                    if (len(test_id) <= 256 and isinstance(status, str)
                            and isinstance(outcome, str) and len(status) <= 80 and len(outcome) <= 80):
                        cases[test_id] = {'status': status, 'outcome': outcome, 'path': relative}
            except (OSError, ValueError, TypeError) as exc:
                limitations.append('receipt omitted: ' + relative + ': ' + str(exc)[:120])
    except (OSError, ValueError) as exc:
        limitations.append('execution receipts unavailable: ' + str(exc)[:160])
    return {'schema': 'modport.progress-observation.v1', 'identity': identity,
            'captured_at': time.time(), 'workspace': workspace, 'content': content,
            'content_complete': content_complete, 'verification': cases,
            'tool_outcomes': tool_outcomes, 'diagnostic_excerpts': diagnostic_excerpts,
            'activity': activity, 'evidence_paths': list(dict.fromkeys(evidence_paths))[:128],
            'limitations': list(dict.fromkeys(limitations))[:64]}


def compare_progress(before, after) -> dict[str, Any]:
    """Count changed retained content or improved passing cases, never log activity."""
    limitations = list(after.get('limitations', [])) if isinstance(after, Mapping) else []
    changed: list[str] = []
    improvements: list[dict[str, Any]] = []
    tool_improvements: list[dict[str, Any]] = []
    if not isinstance(before, Mapping):
        limitations.append('first observation establishes a baseline')
    elif not isinstance(after, Mapping) or before.get('identity') != after.get('identity'):
        limitations.append('observations belong to different executions')
    elif before.get('workspace') != after.get('workspace'):
        limitations.append('workspace changed; establish a new baseline')
    else:
        old = before.get('content', {})
        new = after.get('content', {})
        changed = [path for path, value in new.items() if path in old and old[path] != value]
        if before.get('content_complete'):
            changed.extend(path for path in new if path not in old)
        if after.get('content_complete'):
            changed.extend(path for path in old if path not in new)
        for test_id, row in after.get('verification', {}).items():
            previous = before.get('verification', {}).get(test_id, {})
            passed = row.get('status') == row.get('outcome') == 'passed'
            prior_passed = previous.get('status') == previous.get('outcome') == 'passed'
            if passed and not prior_passed:
                improvements.append({'test_id': test_id, 'before': previous.get('status'),
                                     'after': 'passed', 'path': row.get('path')})
        for key, row in after.get('tool_outcomes', {}).items():
            previous = before.get('tool_outcomes', {}).get(key, {})
            if (previous.get('status') == 'failed' and row.get('status') == 'passed'
                    and previous.get('context') == row.get('context')):
                tool_improvements.append({'before': 'failed', 'after': 'passed',
                                          'context': row['context'], 'path': row.get('path')})
    return {'useful_progress': bool(changed or improvements or tool_improvements),
            'changed_paths': sorted(set(changed)), 'verification_improvements': improvements,
            'tool_improvements': tool_improvements,
            'activity': after.get('activity', []) if isinstance(after, Mapping) else [],
            'limitations': list(dict.fromkeys(limitations))[:64]}


def write_snapshot(root, execution_id: str, snapshot: Mapping[str, Any], *, slot='latest') -> str:
    """Atomically replace only this observer's latest/previous slots."""
    root = _root(root)
    if (slot not in {'latest', 'previous'} or not isinstance(execution_id, str)
            or not execution_id or len(execution_id) > 240
            or snapshot.get('identity', {}).get('command_id') != execution_id):
        raise ValueError('invalid progress snapshot slot or execution identity')
    token = quote(execution_id, safe='')
    if token in {'.', '..'} or len(token.encode()) > 240:
        raise ValueError('execution identity cannot form a snapshot directory')
    relative = Path('artifacts/progress-supervision') / token / (slot + '.json')
    path = _contained(root, relative)
    raw = json.dumps(snapshot, ensure_ascii=True, allow_nan=False, separators=(',', ':')).encode()
    if len(raw) > MAX_SNAPSHOT_BYTES:
        raise ValueError('progress snapshot exceeds byte limit')
    directory = _directory_fd(root, relative.parent, create=True)
    temporary = '.' + slot + '-' + uuid4().hex
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                             0o600, dir_fd=directory)
        with os.fdopen(descriptor, 'wb') as output:
            output.write(raw)
        _contained(root, relative)
        os.replace(temporary, path.name, src_dir_fd=directory, dst_dir_fd=directory)
    finally:
        try:
            os.unlink(temporary, dir_fd=directory)
        except FileNotFoundError:
            pass
        os.close(directory)
    return relative.as_posix()


def read_snapshot(root, relative_path: str) -> dict[str, Any] | None:
    """Read one bounded observer slot, rejecting escapes and symbolic links."""
    try:
        root = _root(root)
        relative = Path(relative_path)
        if (len(relative.parts) != 4 or relative.parts[:2] != ('artifacts', 'progress-supervision')
                or relative.name not in {'latest.json', 'previous.json'}):
            return None
        snapshot = json.loads(_read(_contained(root, relative), MAX_SNAPSHOT_BYTES, root=root))
        if (not isinstance(snapshot, dict)
                or snapshot.get('schema') != 'modport.progress-observation.v1'
                or not isinstance(snapshot.get('identity'), Mapping)
                or not isinstance(snapshot['identity'].get('command_id'), str)
                or quote(snapshot['identity']['command_id'], safe='') != relative.parts[2]):
            return None
        return snapshot
    except (OSError, ValueError, TypeError):
        return None


__all__ = ['capture_progress', 'compare_progress', 'read_snapshot', 'write_snapshot']
