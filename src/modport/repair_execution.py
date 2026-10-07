"""Prepare isolated repair context and merge authored deltas into current products."""
from dataclasses import replace
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import tempfile
import time

from . import handlers
from .development import (_artifact, _git, _head, _path, _paths, _verified,
                          DevelopmentIntegrateHandler)
from .workspace import is_run_path, project_path, workspace_spec
from .local_workspace_sandbox import is_sensitive_name


def _sensitive_snapshot_name(name):
    return is_sensitive_name(name)


def _sensitive_snapshot_path(relative):
    return any(_sensitive_snapshot_name(part) for part in Path(relative).parts)


def _contained(root, relative, *, project=False):
    _path(relative, shared=True)
    if any(':' in part for part in Path(relative).parts):
        raise ValueError('repair paths cannot contain drive qualifiers or streams')
    path = project_path(root, relative) if project else root / relative
    contained = is_run_path(root, path) if project else path.is_relative_to(root)
    if path.resolve() != path.absolute() or not contained:
        raise ValueError('repair workspace must be contained without symlinks')
    return path


def _snapshot(workspace):
    """List contained regular repair inputs and their preserved file modes."""
    files = {}
    for directory, dirs, names in os.walk(workspace, followlinks=False):
        dirs[:] = [name for name in dirs if not _sensitive_snapshot_name(name)]
        names = [name for name in names if not _sensitive_snapshot_name(name)]
        for name in [*dirs, *names]:
            path = Path(directory) / name
            relative = path.relative_to(workspace).as_posix()
            _path(relative, shared=True)
            mode = path.lstat().st_mode
            if stat.S_ISLNK(mode) or not (stat.S_ISDIR(mode) or stat.S_ISREG(mode)):
                raise ValueError('repair snapshot rejects symlinks and special files: ' + relative)
            if stat.S_ISREG(mode):
                files[relative] = {'mode': stat.S_IMODE(mode)}
    return files


def _scope(command):
    scope = command.stage_id.split('_', 1)[0]
    if scope not in ('contract', 'target'):
        raise ValueError('unknown repair scope')
    return scope


def _authenticated_json(command, ref):
    data = _verified(command, ref).read_bytes()
    return json.loads(data)


def _new_workspace(command, relative):
    root = Path(command.run_dir)
    path = _contained(root, relative)
    if path.exists():
        raise ValueError('repair workspace already exists')
    path.mkdir(parents=True)
    return path


class RepairPrepareHandler:
    def __call__(self, command):
        try:
            from .planning import approved_repair_development_plan
            root = handlers._run_root(command)
            scope = _scope(command)
            source_relative = 'baseline' if scope == 'contract' else 'worktree'
            source = _contained(root, source_relative, project=True)
            plan = approved_repair_development_plan(command)
            original_head = _head(command, source)
            before = _snapshot(source)
            relative = 'workspaces/repair-bases/' + scope + '/' + command.command_id
            isolated = _new_workspace(command, relative)
            for name in before:
                target = isolated / name
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source / name, target, follow_symlinks=False)
                target.chmod(before[name]['mode'])
            _git(command, isolated, 'init')
            _git(command, isolated, 'add', '--force', '--all', '--', '.')
            _git(command, isolated, 'commit', '--allow-empty', '--no-gpg-sign', '-m', 'Freeze repair input snapshot')
            base = _head(command, isolated)
            frozen = {**plan, 'base_commit': base}
            plan_ref = _artifact(command, 'repair-development-plan.json', json.dumps(frozen, sort_keys=True).encode(),
                                 {'development_base': base, 'repair_scope': scope})
            record = {'schema_version': 1, 'run_id': command.run_id, 'scope': scope,
                      'source_workspace': source_relative, 'source_head': original_head,
                      'development_source_workspace': relative, 'development_base': base,
                      'files': before, 'development_plan': plan_ref}
            snapshot_ref = _artifact(command, 'repair-snapshot.json', json.dumps(record, sort_keys=True).encode())
            return handlers._result(command, 'completed', outputs={
                'development_tasks': frozen['tasks'], 'development_base': base,
                'development_source_workspace': relative, 'goal_scope': scope,
                'artifact_refs': {'development_plan': plan_ref, 'repair_snapshot': snapshot_ref}})
        except (OSError, ValueError, TypeError, KeyError, subprocess.TimeoutExpired) as exc:
            return handlers._result(command, 'blocked', detail=str(exc), error_code='repair_snapshot_invalid')


def _install_file(source, destination, mode):
    destination.parent.mkdir(parents=True, exist_ok=True)
    source, destination = Path(source), Path(destination)
    if (source.is_symlink() or not source.is_file()
            or source.resolve() != source.absolute()
            or destination.parent.resolve() != destination.parent.absolute()
            or destination.is_symlink()):
        raise ValueError('repair publication source or destination is unsafe')
    flags = os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0)
    source_fd = os.open(source, flags)
    temporary_fd = None
    temporary_path = None
    try:
        source_stat = os.fstat(source_fd)
        if not stat.S_ISREG(source_stat.st_mode):
            raise ValueError('repair publication source is not a regular file')
        temporary_fd, temporary_name = tempfile.mkstemp(
            prefix='.' + destination.name + '.modport-', dir=destination.parent)
        temporary_path = Path(temporary_name)
        with os.fdopen(source_fd, 'rb') as incoming, os.fdopen(temporary_fd, 'wb') as outgoing:
            source_fd = temporary_fd = None
            while True:
                block = incoming.read(1024 * 1024)
                if not block:
                    break
                outgoing.write(block)
            outgoing.flush()
            os.fsync(outgoing.fileno())
            from .platform_files import file_os
            file_os.fchmod(outgoing.fileno(), mode)
        if destination.parent.resolve() != destination.parent.absolute() or destination.is_symlink():
            raise ValueError('repair publication destination changed during write')
        os.replace(temporary_path, destination)
        temporary_path = None
    finally:
        if source_fd is not None:
            os.close(source_fd)
        if temporary_fd is not None:
            os.close(temporary_fd)
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def _index_git(command, source, index, *args):
    result = handlers._exec(
        ['git', '--literal-pathspecs', '-c', 'core.hooksPath=/dev/null', '-c', 'core.fsmonitor=false',
         '-c', 'user.name=ModPort', '-c', 'user.email=modport@localhost', *args],
        cwd=source, log=Path(command.run_dir) / 'logs' / ('repair-index-' + command.command_id + '.log'),
        timeout=handlers._remaining_timeout(command, 120),
        env={'GIT_INDEX_FILE': str(index), 'GIT_CONFIG_NOSYSTEM': '1', 'GIT_CONFIG_GLOBAL': '/dev/null',
             'GIT_TERMINAL_PROMPT': '0', 'GIT_NO_REPLACE_OBJECTS': '1'})
    if result.returncode:
        raise ValueError('repair index git operation failed: ' + result.stdout[-1000:])
    return result.stdout.strip()


class _TargetRollbackFailed(ValueError):
    """HEAD/index rollback failed inside a partially committed transaction."""


def _rollback_target(command, source, transaction):
    # Cleanup must still run after the operation's work budget has expired.
    # This short, separate deadline authorizes rollback only, never more work.
    command = replace(command, options={**command.options, 'deadline_epoch': time.time() + 30})
    index = transaction['index']
    lock = index.with_name(index.name + '.lock')
    with lock.open('xb'):
        pass
    try:
        head = _head(command, source)
        if head == transaction['new_head']:
            _git(command, source, 'update-ref', 'HEAD', transaction['old_head'], head)
        elif head != transaction['old_head']:
            raise ValueError('target HEAD changed before rollback')
        if transaction['original_index'] is None:
            index.unlink(missing_ok=True)
        else:
            lock.write_bytes(transaction['original_index'])
            lock.chmod(transaction['index_mode'])
            os.replace(lock, index)
    finally:
        lock.unlink(missing_ok=True)


def _commit_target(command, source, changed, staging, old_head):
    """Commit owned paths using temporary indexes, preserving other staged work."""
    root = Path(command.run_dir).resolve()
    index = Path(_git(command, source, 'rev-parse', '--path-format=absolute', '--git-path', 'index').stdout.strip()).absolute()
    if index.resolve() != index.absolute():
        raise ValueError('target Git index must be contained without symlinks')
    if not index.is_relative_to(root):
        specification = workspace_spec(root)
        expected = (Path(specification['git_directory']).absolute() / 'index'
                    if specification and specification.get('mode') == 'git_worktree' else None)
        if (expected is None or expected.resolve() != expected.absolute()
                or index != expected):
            raise ValueError('target Git index is outside the frozen worktree index or Run state')
    lock = index.with_name(index.name + '.lock')
    with lock.open('xb'):
        pass
    transaction = None
    try:
        transaction = {'index': index, 'original_index': index.read_bytes() if index.exists() else None,
                       'index_mode': stat.S_IMODE(index.stat().st_mode) if index.exists() else 0o644,
                       'old_head': old_head, 'new_head': old_head}
        temporary = staging / 'commit-index'
        _index_git(command, source, temporary, 'read-tree', old_head)
        _index_git(command, source, temporary, 'add', '--force', '--all', '--', *changed)
        tree = _index_git(command, source, temporary, 'write-tree')
        new_head = _index_git(command, source, temporary, 'commit-tree', '--no-gpg-sign', tree, '-p', old_head,
                              '-m', 'Apply reviewed target repair')
        transaction['new_head'] = new_head
        replacement = staging / 'replacement-index'
        if transaction['original_index'] is not None:
            replacement.write_bytes(transaction['original_index'])
        else:
            _index_git(command, source, replacement, 'read-tree', old_head)
        _index_git(command, source, replacement, 'reset', new_head, '--', *changed)
        lock.write_bytes(replacement.read_bytes())
        lock.chmod(transaction['index_mode'])
        _git(command, source, 'update-ref', 'HEAD', new_head, old_head)
        os.replace(lock, index)
        return transaction
    except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
        try:
            lock.unlink(missing_ok=True)
            if transaction is not None:
                _rollback_target(command, source, transaction)
        except (OSError, ValueError, subprocess.TimeoutExpired) as rollback:
            raise _TargetRollbackFailed(f'{exc}; target HEAD/index rollback failed: {rollback}') from rollback
        raise
    finally:
        lock.unlink(missing_ok=True)


class RepairIntegrateHandler:
    def __call__(self, command):
        try:
            root = handlers._run_root(command)
            scope = _scope(command)
            record = _authenticated_json(command, command.artifact_refs['repair_snapshot'])
            expected_source = 'baseline' if scope == 'contract' else 'worktree'
            plan_ref = command.artifact_refs['development_plan']
            recorded_plan = record.get('development_plan')
            if (record.get('run_id') != command.run_id or record.get('scope') != scope
                    or record.get('source_workspace') != expected_source
                    or record.get('development_source_workspace') != command.payload.get('development_source_workspace')
                    or not isinstance(recorded_plan, dict)
                    or recorded_plan.get('path') != plan_ref.get('path')
                    or command.payload.get('goal_scope') != scope):
                raise ValueError('repair snapshot scope or author binding mismatch')
            # The snapshot remains original author context. It is not a lease
            # on the mutable source tree or a file publication template.
            repair_source = _contained(root, record['development_source_workspace'])
            source = _contained(root, expected_source, project=True)
            observed_head = _head(command, source)
            # Import the snapshot's base objects so Git can three-way merge
            # ignored inputs that were never tracked in the product repository.
            # Fetching objects does not restore snapshot files over current edits.
            _git(command, source, 'fetch', '--no-tags', '--', str(repair_source),
                 record['development_base'])
            integrated = DevelopmentIntegrateHandler()(replace(command,
                options={**command.options, 'workspace': expected_source},
                payload={**command.payload, 'repair_input_paths': list(record['files'])}))
            if integrated.status != 'completed':
                return integrated
            head = integrated.outputs['head']
            integration_ref = integrated.outputs['artifact_refs']['development_integration']
            integration_record = _authenticated_json(command, integration_ref)
            actual_base = integration_record.get('integration_start', observed_head)
            changed = integrated.outputs.get('changed_paths')
            if not isinstance(changed, list):
                changed = _paths(command, source, actual_base, head)
            baseline_project_changes = ([name for name in changed
                if name != '.modport' and not name.startswith('.modport/')]
                if scope == 'contract' else [])
            provenance = {
                'original_source_head': record.get('source_head'),
                'observed_source_head': observed_head,
                'baseline_project_changes': baseline_project_changes,
                'represents_original_source': not baseline_project_changes,
            }
            business_diagnostics = integrated.outputs.get('business_diagnostics', [])
            ref = _artifact(command, 'repair-integration.json', json.dumps({
                'scope': scope, 'source_head': record.get('source_head'),
                'observed_source_head': observed_head, 'head': head,
                'changed_paths': changed, 'baseline_provenance': provenance,
                'business_diagnostics': business_diagnostics,
                'repair_snapshot': command.artifact_refs['repair_snapshot'],
                'development_integration': integration_ref}, sort_keys=True).encode())
            return handlers._result(command, 'completed', outputs={**integrated.outputs,
                'goal_scope': scope, 'source_workspace': expected_source,
                'changed_paths': changed,
                'baseline_project_changes': baseline_project_changes,
                'represents_original_source': provenance['represents_original_source'],
                'artifact_refs': {**integrated.outputs.get('artifact_refs', {}),
                                  'repair_integration': ref}})
        except (OSError, ValueError, TypeError, KeyError, subprocess.TimeoutExpired) as exc:
            return handlers._result(command, 'blocked', detail=str(exc),
                                    error_code='repair_integration_invalid')
