"""Isolated diagnostic source edits, published for matching future tasks.

The host supplies task and plan identities. File bytes are compared only to
avoid overwriting a changed baseline; this facility adds no artifact hashes or
business approval gates. Callers own workspace exclusion and Git integration.
"""
from __future__ import annotations

from dataclasses import replace
import json
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import re
import stat
from typing import Mapping

from .contracts import OperationInput, OperationResult, json_copy
from .local_workspace_sandbox import is_sensitive_name
from .platform_files import assert_no_reparse, atomic_write, safe_open
from .workspace import is_project_workspace, project_path, project_relative


MAX_FILE_BYTES = 1024 * 1024
MAX_TOTAL_BYTES = 16 * 1024 * 1024
MAX_FILES = 2000
MAX_DIRECTORIES = 10000
MAX_TARGETS = 64
MAX_DOCUMENT_BYTES = 64 * 1024 * 1024
_EXCLUDED = frozenset({
    '.git', '.modport', '.gradle', '.venv', 'venv', '.cache', '__pycache__',
    '.pytest_cache', '.mypy_cache', '.ruff_cache', 'build', 'dist', 'node_modules',
    'runs', 'workspaces', 'artifacts', 'logs', 'target', 'out', '.gnupg', 'secrets',
})
_SECRET_SUFFIXES = ('.key', '.pem', '.p12', '.pfx', '.jks')


def enabled(command: OperationInput) -> bool:
    return type(command.options.get('workflow_version')) is int and command.options['workflow_version'] >= 40


def _relative(value) -> str:
    if (not isinstance(value, str) or not value or '\\' in value
            or any(ord(character) < 32 for character in value)):
        raise ValueError('diagnostic repair path must be contained and relative')
    path = PurePosixPath(value)
    if (path.is_absolute() or any(part in {'', '.', '..'} for part in value.split('/'))
            or any(PureWindowsPath(part).drive for part in path.parts)):
        raise ValueError('diagnostic repair path must be contained and relative')
    return value


def _excluded(value: str) -> bool:
    return any(is_sensitive_name(part) or part.casefold() in _EXCLUDED
               or part.casefold().endswith(_SECRET_SUFFIXES)
               for part in PurePosixPath(value).parts)


def _root(command) -> Path:
    root = Path(command.run_dir).absolute()
    assert_no_reparse(root)
    if root.resolve() != root:
        raise ValueError('diagnostic repair Run root traverses a link')
    return root


def _path(root: Path, relative: str) -> Path:
    relative = _relative(relative)
    candidate = root
    for component in PurePosixPath(relative).parts:
        candidate = candidate / component
        if candidate.exists() or candidate.is_symlink():
            assert_no_reparse(candidate)
    if candidate.resolve() != candidate.absolute():
        raise ValueError('diagnostic repair path traverses a link')
    return candidate


def _mkdir(root: Path, relative: str) -> Path:
    current = root
    for component in PurePosixPath(_relative(relative)).parts:
        current = current / component
        if not current.exists() and not current.is_symlink():
            current.mkdir(mode=0o700)
        assert_no_reparse(current)
        if not current.is_dir():
            raise ValueError('diagnostic repair directory is not a directory')
    return current


def _read(root: Path, relative: str, limit=MAX_FILE_BYTES) -> bytes:
    descriptor = safe_open(root, _relative(relative), os.O_RDONLY | os.O_NONBLOCK)
    with os.fdopen(descriptor, 'rb') as stream:
        before = os.fstat(stream.fileno())
        if before.st_size > limit:
            raise ValueError('diagnostic repair file exceeds the size limit')
        data = stream.read(limit + 1)
        after = os.fstat(stream.fileno())
    if (len(data) > limit or len(data) != after.st_size
            or (before.st_ino, before.st_size, before.st_mtime_ns)
            != (after.st_ino, after.st_size, after.st_mtime_ns)):
        raise ValueError('diagnostic repair file changed during capture')
    return data


def _text(data: bytes) -> str:
    if b'\0' in data:
        raise ValueError('diagnostic repairs support UTF-8 text files only')
    return data.decode('utf-8')


def _json(root: Path, ref) -> dict:
    if not isinstance(ref, Mapping):
        raise ValueError('diagnostic repair artifact reference is missing')
    path = _relative(ref.get('path'))
    if not path.startswith('artifacts/diagnostic-repairs/'):
        raise ValueError('diagnostic repair reference is outside host repair artifacts')
    value = json.loads(_read(root, path, MAX_DOCUMENT_BYTES))
    if not isinstance(value, dict):
        raise ValueError('diagnostic repair document must be an object')
    return value


def _publish(root: Path, relative: str, value: dict, metadata: dict) -> dict:
    data = (json.dumps(value, ensure_ascii=False, sort_keys=True) + '\n').encode('utf-8')
    if len(data) > MAX_DOCUMENT_BYTES:
        raise ValueError('diagnostic repair document exceeds the size limit')
    _mkdir(root, PurePosixPath(relative).parent.as_posix())
    path = _path(root, relative)
    if path.exists():
        if _read(root, relative, MAX_DOCUMENT_BYTES) != data:
            raise ValueError('previously published diagnostic repair evidence differs')
    else:
        descriptor = safe_open(root, relative, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o444)
        with os.fdopen(descriptor, 'wb') as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
    return {'path': relative, 'media_type': 'application/json', 'metadata': json_copy(metadata)}


def _targets(value) -> list[dict]:
    if not isinstance(value, list) or len(value) > MAX_TARGETS:
        raise ValueError('diagnostic repair targets must be a bounded list')
    targets, seen = [], []
    for raw in value:
        if not isinstance(raw, Mapping):
            raise ValueError('diagnostic repair target must be an object')
        task_id, plan = raw.get('task_id'), raw.get('plan_ref')
        if (not isinstance(task_id, str) or not re.fullmatch(r'[A-Za-z0-9_.:-]+', task_id)
                or not isinstance(plan, Mapping)):
            raise ValueError('diagnostic repair target needs a task and plan reference')
        target = {'task_id': task_id, 'plan_ref': json_copy(dict(plan)),
                  'source_workspace': _relative(raw.get('source_workspace')),
                  'source_execution_id': raw.get('source_execution_id')}
        if target['source_execution_id'] is not None and not isinstance(target['source_execution_id'], str):
            raise ValueError('diagnostic repair source execution identity must be text or null')
        identity = (task_id, target['plan_ref'])
        if identity in seen:
            raise ValueError('duplicate diagnostic repair task and plan target')
        seen.append(identity)
        targets.append(target)
    return targets


def _source(root: Path, relative: str) -> Path:
    parts = PurePosixPath(relative).parts
    if (parts[0] not in {'worktree', 'workspaces'}
            or any(part.casefold().startswith('artifact') for part in parts)):
        raise ValueError('frozen source baselines and delivered artifacts are not repair targets')
    source = project_path(root, relative)
    assert_no_reparse(source)
    if not source.is_dir() or not is_project_workspace(root, source):
        raise ValueError('diagnostic repair source is outside the registered project')
    return source


def _inventory(source: Path):
    """Yield visible files and explicit skipped entries without following links."""
    directories_seen, files_seen = 0, 0
    errors = []
    for current, directories, files in os.walk(source, topdown=True, followlinks=False,
                                               onerror=errors.append):
        directories_seen += 1
        if directories_seen > MAX_DIRECTORIES:
            yield None, 'directory_limit'
            return
        folder = Path(current)
        assert_no_reparse(folder)
        kept = []
        for name in sorted(directories):
            relative = (folder / name).relative_to(source).as_posix()
            if _excluded(relative):
                continue
            try:
                _relative(relative)
                assert_no_reparse(folder / name)
            except (OSError, ValueError) as error:
                yield relative, str(error)
            else:
                kept.append(name)
        directories[:] = kept
        for name in sorted(files):
            relative = (folder / name).relative_to(source).as_posix()
            if not _excluded(relative):
                files_seen += 1
                if files_seen > MAX_FILES:
                    yield relative, 'file_count_limit'
                    return
                yield relative, None
    for error in errors:
        yield None, 'directory_read_error: ' + str(error)


def prepare(command: OperationInput) -> OperationInput:
    """Capture private editable text copies once; recovery keeps existing edits."""
    if not enabled(command):
        return command
    root = _root(command)
    targets = _targets(command.payload.get('diagnostic_repair_targets', []))
    relative = f'workspaces/diagnostic-repairs/{command.command_id}'
    manifest_path = f'artifacts/diagnostic-repairs/{command.command_id}/manifest.json'
    manifest_ref = {'path': manifest_path, 'media_type': 'application/json',
                    'metadata': {'execution_id': command.command_id, 'run_id': command.run_id}}
    if _path(root, manifest_path).exists():
        manifest = _json(root, manifest_ref)
        if (manifest.get('run_id') != command.run_id
                or manifest.get('producer_execution_id') != command.command_id
                or manifest.get('workspace') != relative or manifest.get('targets') != targets):
            raise ValueError('diagnostic repair recovery differs from the frozen host manifest')
        workspace = _path(root, relative)
        if not workspace.is_dir():
            raise ValueError('diagnostic repair recovery workspace is missing; refusing to reset edits')
    else:
        existing = _path(root, relative)
        if existing.is_dir() and any(existing.iterdir()):
            raise ValueError('unfinished diagnostic snapshot has no host manifest; refusing to reset edits')
        workspace = _mkdir(root, relative)
        records, total, count = [], 0, 0
        for index, target in enumerate(targets):
            record = {**target, 'snapshot_directory': f'source/task-{index}', 'files': [], 'skipped': []}
            records.append(record)
            _mkdir(workspace, record['snapshot_directory'])
            try:
                source = _source(root, target['source_workspace'])
                for path, error in _inventory(source):
                    if error:
                        record['skipped'].append({'path': path, 'reason': error})
                        continue
                    if count >= MAX_FILES:
                        record['skipped'].append({'path': path, 'reason': 'file_count_limit'})
                        break
                    try:
                        before = _read(source, path)
                        text = _text(before)
                        if total + len(before) > MAX_TOTAL_BYTES:
                            raise ValueError('total_size_limit')
                        snapshot_path = record['snapshot_directory'] + '/' + _relative(path)
                        _mkdir(workspace, PurePosixPath(snapshot_path).parent.as_posix())
                        destination = _path(workspace, snapshot_path)
                        if destination.exists():
                            raise ValueError('partial diagnostic snapshot exists; refusing to reset edits')
                        atomic_write(destination, before)
                        record['files'].append({'path': path, 'snapshot_path': snapshot_path, 'before': text})
                        total += len(before)
                        count += 1
                    except (OSError, ValueError) as error:
                        record['skipped'].append({'path': path, 'reason': str(error)})
            except (OSError, ValueError) as error:
                record['skipped'].append({'path': target['source_workspace'], 'reason': str(error)})
        manifest = {'schema_version': 1, 'run_id': command.run_id,
                    'producer_execution_id': command.command_id, 'workspace': relative,
                    'snapshot_consistency': 'per_file_best_effort_not_atomic',
                    'targets': targets, 'sources': records}
        manifest_ref = _publish(root, manifest_path, manifest, manifest_ref['metadata'])
    return replace(command, options={**command.options, 'workspace': relative},
                   payload={**command.payload, 'diagnostic_repair_manifest': manifest_ref})


def instructions(command: OperationInput) -> str:
    if not enabled(command) or not command.payload.get('diagnostic_repair_manifest'):
        return ''
    return ("You may directly fix obvious, confirmed, small code errors in the supplied isolated "
            "source/task-N/ copies. The host manifest maps each source directory to one task: "
            + json.dumps(command.payload['diagnostic_repair_manifest'], ensure_ascii=False)
            + ". Read the manifest and original source before editing. Preserve behavior and the "
            "task's remaining work. Edit existing regular UTF-8 source/configuration files only; "
            "creation, deletion, symlinks and binary changes are unsupported. Explain the confirmed "
            "cause and exact edits in your normal report. Do not run project code, builds or tests, "
            "commit changes, edit frozen source contracts, delivered products, host artifacts or "
            "active author workspaces. Only isolated edits are published; the host safely applies "
            "matching repairs to future task baselines and retains conflicts for the coder. A repair "
            "does not authorize restart/cancellation or establish acceptance. Previous repairs in "
            "the host context are existing work; avoid repeating them. Add no hash, checksum or "
            "fingerprint checks. Continue your original diagnostic/planning task and leave broader "
            "or uncertain changes to the coder.")


def collect(command: OperationInput, result: OperationResult) -> OperationResult:
    if not enabled(command) or not command.payload.get('diagnostic_repair_manifest'):
        return result
    result.validate_for(command)
    root = _root(command)
    refs, diagnostics = [], []
    try:
        manifest = _json(root, command.payload['diagnostic_repair_manifest'])
        if (manifest.get('run_id') != command.run_id
                or manifest.get('producer_execution_id') != command.command_id
                or manifest.get('targets') != _targets(command.payload.get('diagnostic_repair_targets', []))
                or manifest.get('workspace') != command.options.get('workspace')):
            raise ValueError('diagnostic repair collection differs from its host manifest')
        workspace = _path(root, manifest['workspace'])
        for index, source in enumerate(manifest['sources']):
            files, problems = [], []
            known = {item['path'] for item in source['files']}
            snapshot = _path(workspace, source['snapshot_directory'])
            for item in source['files']:
                try:
                    after = _text(_read(workspace, item['snapshot_path']))
                    if after != item['before']:
                        files.append({'path': item['path'], 'before': item['before'], 'after': after})
                except (OSError, ValueError) as error:
                    problems.append({'path': item['path'], 'reason': 'unsupported deletion or unsafe edit: ' + str(error)})
            try:
                for path, error in _inventory(snapshot):
                    if error or path not in known:
                        problems.append({'path': path, 'reason': error or 'unsupported new file'})
            except (OSError, ValueError) as error:
                problems.append({'path': source['snapshot_directory'], 'reason': str(error)})
            diagnostics.extend({'task_id': source['task_id'], **problem} for problem in problems)
            if not files and not problems:
                continue
            metadata = {key: source[key] for key in ('task_id', 'plan_ref', 'source_workspace', 'source_execution_id')}
            metadata.update(execution_id=command.command_id, producer_execution_id=command.command_id,
                            run_id=command.run_id,
                            applicable=result.status == 'completed' and not problems and bool(files))
            document = {**metadata, 'producer_execution_id': command.command_id,
                        'schema_version': 1, 'files': files, 'diagnostics': problems,
                        'snapshot_consistency': manifest['snapshot_consistency']}
            ref = _publish(root, f'artifacts/diagnostic-repairs/{command.command_id}/task-{index}.json',
                           document, metadata)
            refs.append(ref)
    except (OSError, ValueError, KeyError, TypeError) as error:
        diagnostics.append({'reason': str(error)})
    outputs = {**result.outputs, 'diagnostic_repairs': refs}
    if diagnostics:
        outputs['diagnostic_repair_diagnostics'] = diagnostics
    return replace(result, outputs=outputs)


def _bindings(command):
    if command.payload.get('goal_scope') == 'contract':
        return []
    explicit = command.payload.get('diagnostic_repair_targets')
    if explicit is not None:
        # A receiver is authorized for plan tasks; it need not recreate the
        # producer's source workspace, which may no longer be active.
        if not isinstance(explicit, list) or len(explicit) > MAX_TARGETS:
            raise ValueError('diagnostic repair consumer targets must be a bounded list')
        bindings = []
        for target in explicit:
            if (not isinstance(target, Mapping) or not isinstance(target.get('task_id'), str)
                    or not target['task_id'] or not isinstance(target.get('plan_ref'), Mapping)):
                raise ValueError('diagnostic repair consumer needs a host task and plan')
            bindings.append((target['task_id'], dict(target['plan_ref'])))
        return bindings
    task = command.payload.get('development_task')
    plan = command.artifact_refs.get('development_plan')
    if isinstance(task, Mapping) and isinstance(plan, Mapping):
        return [(task.get('id'), dict(plan))]
    return []


def apply(command: OperationInput, workspace: Path, refs) -> list[dict]:
    """Apply each matching repair as a whole or preserve every current file.

    Call only with a quiescent future task or integration workspace under the
    caller's existing workspace lock. This function never stages or commits.
    """
    if not enabled(command):
        return []
    receipts = []
    for ref in refs:
        receipt = {'repair_ref': ref, 'task_id': None, 'status': 'invalid', 'paths': [], 'files': []}
        receipts.append(receipt)
        try:
            receipt['repair_ref'] = json_copy(ref)
            root = _root(command)
            workspace = Path(workspace).absolute()
            assert_no_reparse(workspace)
            if not workspace.is_dir() or not is_project_workspace(root, workspace):
                raise ValueError('diagnostic repair consumer workspace is outside the registered project')
            logical = project_relative(root, workspace)
            if (logical.parts[0] == 'baseline'
                    or any(part.casefold().startswith('artifact') for part in logical.parts)):
                raise ValueError('frozen source baselines and delivered artifacts cannot consume repairs')
            repair = _json(root, ref)
            task_id, plan = repair.get('task_id'), repair.get('plan_ref')
            receipt['task_id'] = task_id
            if (repair.get('schema_version') != 1 or repair.get('run_id') != command.run_id
                    or (task_id, plan) not in _bindings(command)
                    or repair.get('applicable') is not True
                    or ref.get('metadata', {}).get('producer_execution_id') != repair.get('producer_execution_id')
                    or ref.get('metadata', {}).get('task_id') != task_id
                    or ref.get('metadata', {}).get('plan_ref') != plan):
                raise ValueError('diagnostic repair does not match the host task and plan binding')
            files = repair.get('files')
            if not isinstance(files, list) or not files or len(files) > MAX_FILES:
                raise ValueError('diagnostic repair has no bounded text file changes')
            pending, total, seen = [], 0, set()
            for item in files:
                path = _relative(item['path'])
                if path in seen or _excluded(path):
                    raise ValueError('diagnostic repair repeats a path or changes excluded data')
                seen.add(path)
                before, after = item['before'].encode('utf-8'), item['after'].encode('utf-8')
                total += len(before) + len(after)
                if max(len(before), len(after)) > MAX_FILE_BYTES or total > 2 * MAX_TOTAL_BYTES:
                    raise ValueError('diagnostic repair changes exceed the size limits')
                _text(before)
                _text(after)
                try:
                    current = _read(workspace, path)
                    mode = stat.S_IMODE(_path(workspace, path).stat().st_mode)
                except FileNotFoundError:
                    current, mode = None, None
                status = 'already_applied' if current == after else 'applied' if current == before else 'conflict'
                receipt['paths'].append(path)
                receipt['files'].append({'path': path, 'status': status})
                pending.append((path, current, after, mode))
            if any(item['status'] == 'conflict' for item in receipt['files']):
                receipt['status'] = 'conflict'
                for item in receipt['files']:
                    if item['status'] == 'applied':
                        item['status'] = 'conflict'
                continue
            changed = []
            try:
                for path, current, after, mode in pending:
                    if current == after:
                        continue
                    atomic_write(_path(workspace, path), after, mode)
                    changed.append((path, current, mode))
            except (OSError, ValueError):
                for path, current, mode in reversed(changed):
                    atomic_write(_path(workspace, path), current, mode)
                raise
            receipt['status'] = 'applied' if changed else 'already_applied'
        except (OSError, ValueError, KeyError, TypeError, AttributeError) as error:
            receipt['status'] = 'invalid'
            receipt['detail'] = str(error)
            for item in receipt['files']:
                if item['status'] == 'applied':
                    item['status'] = 'invalid'
    return receipts
