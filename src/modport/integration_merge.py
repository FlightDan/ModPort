"""Isolated Git merges with durable conflict inputs for the current coder route."""
import json
from pathlib import Path
import re
from uuid import uuid4

from . import handlers
from .evidence import atomic_json, verified_path
from .workspace import project_path


def _workspace(command, relative):
    from .development import _path
    path = project_path(Path(command.run_dir), _path(relative, shared=True))
    if path.resolve() != path.absolute() or not path.is_dir():
        raise ValueError('merge workspace must be a contained regular directory')
    return path


def clone(command, source):
    from .development import _git
    root = Path(command.run_dir)
    relative = 'workspaces/integration-merges/' + uuid4().hex
    destination = root / relative
    if destination.resolve() != destination.absolute():
        raise ValueError('merge destination must not traverse symlinks')
    destination.parent.mkdir(parents=True, exist_ok=True)
    _git(command, root, 'clone', '--no-hardlinks', '--', str(source), str(destination))
    return destination


def begin(command, destination, *, include_paths=()):
    from .development import _apply, _git, _head, _path, prepare_merge_workspace
    start = prepare_merge_workspace(command, destination, include_paths=include_paths)
    resolution = command.payload.get('integration_resolution')
    if not resolution:
        return clone(command, destination), start, None
    record = json.loads(verified_path(Path(command.run_dir), resolution['merge_ref']).read_text())
    if record['source_workspace'] != command.options.get('workspace', 'worktree'):
        raise ValueError('merge resolution belongs to another workspace')
    original = json.loads(verified_path(Path(command.run_dir), record['command_ref']).read_text())
    if original['run_id'] != command.run_id or original['task_id'] != command.task_id:
        raise ValueError('merge resolution belongs to another Run or task')
    saved = _workspace(command, record['workspace'])
    repaired = clone(command, saved)
    _git(command, repaired, 'checkout', '--detach', record['merge_head'])
    patch = verified_path(Path(command.run_dir), resolution['coder_patch'])
    _apply(command, repaired, patch, {'id': 'integration-resolution', 'owned_paths': []})
    for conflict in record['conflicts']:
        for relative in conflict['paths']:
            target = repaired / _path(relative, shared=True)
            if target.is_file() and re.search(rb'(?m)^(<<<<<<< |>>>>>>> )', target.read_bytes()):
                raise ValueError('integration coder left unresolved conflict in ' + relative)
    # Include all successfully merged original tasks, not just the coder's
    # final conflict edits. Apply this complete delta to the current candidate.
    delta = repaired.parent / (repaired.name + '.patch')
    _git(command, repaired, 'diff', '--binary', '--full-index', '--no-ext-diff',
         '--no-textconv', '--no-renames', '--output=' + str(delta), record['start'],
         _head(command, repaired), '--')
    aggregate = clone(command, destination)
    conflicts = []
    _apply(command, aggregate, delta, {'id': 'integration-resolution', 'owned_paths': []},
           conflict_handoff=conflicts)
    return aggregate, start, conflicts


def pending(command, workspace, start, conflicts, task_ids, patch_refs):
    from .development import _head
    root = Path(command.run_dir)
    directory = root / 'artifacts' / 'integration-merges' / uuid4().hex
    if directory.resolve() != directory.absolute():
        raise ValueError('merge artifacts must not traverse symlinks')
    command_path = directory / 'command.json'
    atomic_json(command_path, command.to_dict())
    record = {'source_workspace': command.options.get('workspace', 'worktree'),
              'workspace': workspace.relative_to(root).as_posix(), 'start': start,
              'merge_head': _head(command, workspace), 'conflicts': conflicts,
              'original_task_ids': task_ids, 'patch_refs': patch_refs,
              'plan_ref': command.artifact_refs.get('development_plan'),
              'command_ref': {'path': command_path.relative_to(root).as_posix()}}
    record_path = directory / 'merge.json'
    atomic_json(record_path, record)
    ref = {'path': record_path.relative_to(root).as_posix()}
    return handlers._result(command, 'failed', error_code='integration_merge_required',
        detail='Actual Git conflicts require a coder; the current candidate is preserved',
        outputs={'integration_merge_ref': ref, 'acceptance_status': 'unverified',
                 'artifact_refs': {'integration_merge': ref}})


def publish(command, destination, workspace, start, task_ids, patch_refs, *, _attempt=0):
    from .development import _apply, _git, _head, prepare_merge_workspace
    # The operation lock serializes host writers. Also merge any user edits
    # made while the isolated integration was being prepared.
    current = prepare_merge_workspace(command, destination)
    delta = workspace.parent / (workspace.name + '-publish.patch')
    _git(command, workspace, 'diff', '--binary', '--full-index', '--no-ext-diff',
         '--no-textconv', '--no-renames', '--output=' + str(delta), start,
         _head(command, workspace), '--')
    latest = clone(command, destination)
    conflicts = []
    _apply(command, latest, delta, {'id': 'integration-publish', 'owned_paths': []},
           conflict_handoff=conflicts)
    if conflicts:
        return pending(command, latest, current, conflicts, task_ids, patch_refs)
    # Fetching only imports Git objects; normal merge protects local edits and
    # reports an actual concurrency conflict without discarding user files.
    _git(command, destination, 'fetch', '--no-tags', '--', str(latest), _head(command, latest))
    merged = _git(command, destination, 'merge', '--ff-only', '--no-stat', 'FETCH_HEAD', check=False)
    if merged.returncode:
        # A concurrent writer can advance the destination after our clone.
        # Re-merge against that candidate in isolation; never reset its files.
        if _attempt < 2:
            return publish(command, destination, workspace, start, task_ids,
                           patch_refs, _attempt=_attempt + 1)
        raise ValueError('candidate publication could not settle concurrent edits: '
                         + merged.stdout[-2000:])
    return None
