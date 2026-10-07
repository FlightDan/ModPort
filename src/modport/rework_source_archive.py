"""Recover author descriptors through frozen continuation references."""
from collections import deque
from pathlib import Path

from .contracts import OperationInput
from .evidence import file_digest, verified_path
from .json_catalog import iter_catalog
from .payload_storage import unpack_input


SOURCE_KEY = 'continuation:rework_sources'
_SETTLED = frozenset({'succeeded', 'failed', 'cancelled', 'timed_out', 'dead'})


def _read_sources(root, ref, logical_run_id, expected_segment, wanted):
    metadata = ref.get('metadata')
    if (ref.get('media_type') != 'application/json' or not isinstance(metadata, dict)
            or metadata.get('continuation_rework_sources') is not True):
        raise ValueError('continuation source reference has no host identity')
    if (ref.get('path') != f'artifacts/continuations/{expected_segment}/rework-sources.json'
            or metadata.get('next_run_id') != expected_segment):
        raise ValueError('continuation source reference identifies another segment')
    if not (Path(root) / ref["path"]).exists():
        from .artifact_retention import restore_archived_artifact
        restore_archived_artifact(root, ref["path"])
    path = verified_path(root, ref)
    if ref.get('sha256') != file_digest(path):
        raise ValueError('continuation source reference digest differs')
    fields = ('schema_version', 'previous_run_id', 'previous_revision',
              'previous_generation', 'next_run_id')
    sources, ancestors = {}, {}
    needed = None if wanted is None else set(wanted)
    while True:
        before = None if needed is None else set(needed)
        document, seen = {}, set()
        for kind, name, value in iter_catalog(path):
            if kind == 'field':
                document[name] = value
                continue
            row = value
            if not isinstance(row, dict) or row.get('terminal_state') not in _SETTLED:
                raise ValueError('continuation source is not a settled operation')
            operation = OperationInput.from_dict(unpack_input(root, row['operation']))
            identity = {name: getattr(operation, name) for name in
                        ('run_id', 'task_id', 'stage_id', 'command_id')}
            if (operation.run_id != logical_run_id or operation.run_dir != str(root)
                    or row.get('execution_id') != operation.command_id
                    or row.get('task_id') != operation.task_id
                    or row.get('target_agent') != operation.task_id
                    or row.get('stage_id') != operation.stage_id
                    or row.get('result_identity') != identity
                    or operation.command_id in seen):
                raise ValueError('continuation source operation identity differs')
            seen.add(operation.command_id)
            ancestor = operation.artifact_refs.get(SOURCE_KEY)
            if isinstance(ancestor, dict):
                key = (ancestor.get('path'), ancestor.get('sha256'))
                if all(isinstance(part, str) for part in key):
                    ancestors[key] = ancestor
            if needed is None or operation.command_id in needed:
                sources[operation.command_id] = {'command': operation, 'row': row}
                request = operation.payload.get('reviewer_rework')
                original = request.get('source_execution_id') if isinstance(request, dict) else None
                if needed is not None and isinstance(original, str):
                    needed.add(original)
            # A catalog can be hundreds of MB. Release unselected rows before
            # asking the iterator to decode another complete operation.
            del operation, row, value
        if (document.get('schema_version') != 1 or 'sources' not in document
                or any(document.get(key) != metadata.get(key) for key in fields)):
            raise ValueError('continuation source document identity differs')
        if ref.get('sha256') != file_digest(path):
            raise ValueError('continuation source changed while being read')
        if needed is None or needed == before or needed <= sources.keys():
            break
        # A rework child's original author may precede it in this catalog.
        # Re-read rows only when that child discovers a new missing identity.
    return document, sources, list(ancestors.values())


def inherited_rework_sources(root, state, header, *, wanted=None):
    """Read the current catalog and only its explicitly sealed ancestor links.

    Earlier continuation publishers dropped descriptors from prior segments.
    An operation retained in such a catalog still carries its original catalog
    reference, allowing a new successor to recover the missing authors without
    rewriting old inputs or searching arbitrary historical files.

    The current catalog is the lookup root. Ancestors are visited only while
    requested identities are missing; conflicting rows encountered during that
    lookup are excluded. This is not a full audit of superseded catalogs.
    """
    root = Path(root)
    if header.get('run_id') != state.get('run_id'):
        raise ValueError('continuation source header does not match the SDK snapshot')
    logical_run_id = header.get('logical_run_id', header['run_id'])
    continuation = header.get('continuation', {})
    ref = continuation.get('support_refs', {}).get(SOURCE_KEY)
    if not isinstance(ref, dict):
        return {}, []
    pending = deque([(ref, header['run_id'], continuation.get('previous_run_id'))])
    wanted = None if wanted is None else set(wanted)
    seen = set()
    sources, diagnostics = {}, []
    conflicts = set()
    while pending:
        if wanted is not None and wanted <= sources.keys():
            break
        ref, segment, previous = pending.popleft()
        key = (ref.get('path'), ref.get('sha256'))
        if not all(isinstance(value, str) for value in key):
            diagnostics.append({'detail': 'malformed continuation source reference'})
            continue
        if key in seen:
            continue
        seen.add(key)
        try:
            document, entries, ancestors = _read_sources(root, ref, logical_run_id, segment, wanted)
            if previous is not None and document.get('previous_run_id') != previous:
                raise ValueError('continuation source parent differs from the frozen header')
        except (OSError, UnicodeError, ValueError, KeyError, TypeError) as exc:
            diagnostics.append({'path': ref.get('path'), 'detail': str(exc)})
            continue
        if wanted is not None:
            # A requested rework child also needs its original author. Resolve
            # that closure before discarding unrelated, potentially large rows.
            expanded = list(wanted)
            visited = set()
            while expanded:
                execution = expanded.pop()
                if execution in visited:
                    continue
                visited.add(execution)
                entry = entries.get(execution)
                if entry is None:
                    continue
                request = entry['command'].payload.get('reviewer_rework')
                original = request.get('source_execution_id') if isinstance(request, dict) else None
                if isinstance(original, str):
                    wanted.add(original)
                    expanded.append(original)
        for ancestor in ancestors:
            metadata = ancestor.get('metadata')
            ancestor_segment = metadata.get('next_run_id') if isinstance(metadata, dict) else None
            if isinstance(ancestor_segment, str):
                pending.append((ancestor, ancestor_segment, None))
        for execution, entry in entries.items():
            if wanted is not None and execution not in wanted:
                continue
            if execution in conflicts:
                continue
            if execution in sources and sources[execution]['row'] != entry['row']:
                conflicts.add(execution)
                sources.pop(execution)
                diagnostics.append({'execution_id': execution,
                                    'detail': 'conflicting frozen author descriptors'})
                continue
            sources[execution] = entry
        del document, entries
    return sources, diagnostics
