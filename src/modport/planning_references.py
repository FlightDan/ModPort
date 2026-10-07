"""Store full host planning context once and hand it off by explicit reference."""
from hashlib import sha256
import json
from pathlib import Path

from .evidence import verified_path
from .input_preparation import preparation_checkpoint, preparation_step
from .repair_evidence import is_repair_artifact_ref

DOCUMENT_KIND = 'modport-planning-context-v1'


@preparation_step('context_archive')
def archive_context(root, value):
    root = Path(root)
    raw = (json.dumps(value, ensure_ascii=False, sort_keys=True) + '\n').encode()
    checksum = sha256(raw).hexdigest()
    relative = Path('artifacts/planning-context') / (checksum + '.json')
    path = root / relative
    if any(parent.is_symlink() for parent in path.parents if parent != root):
        raise ValueError('planning context directory must not contain symlinks')
    path.parent.mkdir(parents=True, exist_ok=True)
    ref = {'path': relative.as_posix(), 'sha256': checksum,
           'media_type': 'application/json', 'metadata': {'document_kind': DOCUMENT_KIND}}
    if path.exists() or path.is_symlink():
        if verified_path(root, ref).read_bytes() != raw:
            raise ValueError('planning context archive changed')
        preparation_checkpoint(count='context_archive_reuses')
    else:
        # Readers only receive this reference after the complete atomic write.
        import os
        import tempfile
        descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix='.context-')
        try:
            with os.fdopen(descriptor, 'wb') as stream:
                stream.write(raw)
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        preparation_checkpoint(count='context_archive_bytes', amount=len(raw))
    # Keep the byte-copy closure outside the JSON document. Retry remapping can
    # therefore preserve nested source files without opening/re-embedding each
    # historical document or changing its authenticated bytes.
    dependencies = {}
    for dependency in context_closure(root, value):
        dependencies.setdefault((dependency['path'], dependency.get('sha256')), dependency)
    return {'source_ref': ref, 'json_pointer': '',
            'referenced_artifacts': list(dependencies.values())}


@preparation_step('context_reference_read')
def read_context(root, binding):
    if (not isinstance(binding, dict)
            or set(binding) != {'source_ref', 'json_pointer', 'referenced_artifacts'}
            or not isinstance(binding['referenced_artifacts'], list)
            or binding['json_pointer'] != ''):
        raise ValueError('invalid planning context reference')
    ref = binding['source_ref']
    if not is_context_ref(ref):
        raise ValueError('planning context reference has no host document kind')
    raw = verified_path(Path(root), ref).read_bytes()
    if sha256(raw).hexdigest() != ref.get('sha256'):
        raise ValueError('planning context archive changed')
    preparation_checkpoint(count='context_reference_bytes', amount=len(raw))
    return json.loads(raw)


def is_context_ref(ref):
    return (isinstance(ref, dict) and is_repair_artifact_ref(ref)
            and isinstance(ref.get('metadata'), dict)
            and ref['metadata'].get('document_kind') == DOCUMENT_KIND)


def reference_history(root, context):
    """Keep the current failure inline and historical records in one index."""
    fields = ('prior_attempts', 'prior_findings', 'failure_input', 'upstream_results', 'parent_context')
    history = {key: context.get(key) for key in fields}
    binding = archive_context(root, history)
    compact = dict(context)
    compact['history_source'] = binding
    compact['prior_attempts'] = [
        {**{key: row[key] for key in ('run_id', 'failure_execution_id', 'repair_generation', 'repair_scope')
            if key in row}, 'history_pointer': '/prior_attempts/' + str(index)}
        for index, row in enumerate(history['prior_attempts'])]
    compact['prior_findings'] = ([{'history_pointer': '/prior_findings'}]
                                 if history['prior_findings'] else [])
    for key in ('failure_input', 'upstream_results', 'parent_context'):
        compact[key] = {'history_pointer': '/' + key} if history[key] is not None else None
    return compact


def context_closure(root, value):
    """Expose referenced documents' nested refs for same-byte retry copying.

    JSON archives retain their original paths. The retry packet's existing
    evidence_path_map maps those paths to the copied child artifacts.
    """
    def visit(item):
        if isinstance(item, list):
            for child in item:
                yield from visit(child)
        elif isinstance(item, dict):
            if is_repair_artifact_ref(item):
                yield item
            else:
                # Match the archive's canonical JSON ordering, including which
                # metadata record wins when identical file identities recur.
                for key in sorted(item):
                    yield from visit(item[key])
    yield from visit(value)
