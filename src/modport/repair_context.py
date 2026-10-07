"""Bounded host observations and file-backed context for explicit rework."""
from dataclasses import replace
from hashlib import sha256
from itertools import islice
import json
from .platform_files import file_os as os
from pathlib import Path
import re
import stat
import subprocess

from .evidence import atomic_json, file_digest, verified_path

MAX_LOG_BYTES = 2 * 1024 * 1024


def _bounded_harness_digest(harness, parts, max_bytes):
    """Hash one retained file through no-follow directory descriptors."""
    if not hasattr(os, 'O_NOFOLLOW'):
        return None
    directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    descriptor = os.open(harness, directory_flags)
    try:
        for part in parts[:-1]:
            child = os.open(part, directory_flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        source = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW, dir_fd=descriptor)
        try:
            before = os.fstat(source)
            if not stat.S_ISREG(before.st_mode) or before.st_size > max_bytes:
                return None
            digest = sha256()
            count = 0
            while chunk := os.read(source, min(1024 * 1024, max_bytes + 1 - count)):
                count += len(chunk)
                if count > max_bytes:
                    return None
                digest.update(chunk)
            after = os.fstat(source)
            identity = lambda item: (item.st_dev, item.st_ino, item.st_size,
                                     item.st_mtime_ns, item.st_ctime_ns)
            if count != before.st_size or identity(before) != identity(after):
                return None
            return digest.hexdigest(), count
        finally:
            os.close(source)
    finally:
        os.close(descriptor)


def enabled(command):
    return command.options.get('workflow_version', 0) >= 18


def inherited_harness_candidate_mode(command, workspace):
    root = Path(command.run_dir)
    return (command.options.get('workflow_version', 0) >= 25
            and Path(workspace) == root / 'baseline'
            and 'inherited_harness' in command.artifact_refs)


def observe_source_head(workspace):
    """Return only a Git-observed commit, without claiming clean candidate bytes."""
    workspace = Path(workspace)
    if not workspace.is_dir() or workspace.resolve() != workspace.absolute():
        return None
    from .telemetry import probe_process
    args = ['git', '-c', 'core.hooksPath=/dev/null', '-c', 'core.fsmonitor=false',
            '-c', 'core.untrackedCache=false', '-C', str(workspace)]
    git_env = {**os.environ, 'GIT_OPTIONAL_LOCKS': '0'}
    try:
        head = probe_process(args + ['rev-parse', 'HEAD'], text=True,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                             timeout=15, env=git_env)
        if head.returncode or not re.fullmatch(r'[0-9a-f]{40,64}', head.stdout.strip()):
            return None
        return head.stdout.strip()
    except (OSError, UnicodeError, subprocess.SubprocessError):
        return None


def observe_candidate(workspace, *, inherited_harness=False):
    """Bind clean commits, or v25 inherited source and harness bytes.

    Never use a filesystem path or an author-supplied revision as evidence of
    what the host verified. Unknown/dirty observations remain diagnostic.
    """
    workspace = Path(workspace)
    head = observe_source_head(workspace)
    if head is None:
        return None
    from .telemetry import probe_process
    args = ['git', '-c', 'core.hooksPath=/dev/null', '-c', 'core.fsmonitor=false',
            '-c', 'core.untrackedCache=false', '-C', str(workspace)]
    git_env = {**os.environ, 'GIT_OPTIONAL_LOCKS': '0'}
    try:
        status_args = ['status', '--porcelain', '--untracked-files=normal', '-z']
        if inherited_harness:
            status_args.append('--ignored=matching')
        status = probe_process(args + status_args,
                               text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               timeout=15, env=git_env)
        if status.returncode:
            return None
        if not inherited_harness:
            return head if not status.stdout else None
        tracked = probe_process(args + ['ls-files', '-v', '-z'], text=True,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                timeout=15, env=git_env)
        if tracked.returncode or any(
            len(row) < 3 or row[1] != ' ' or row[0] == 'S' or row[0].islower()
            for row in filter(None, tracked.stdout.split('\0'))
        ):
            return None
        records = status.stdout.split('\0')
        if records[-1] != '':
            return None
        position = 0
        while position < len(records) - 1:
            row = records[position]
            if len(row) < 4 or row[2] != ' ':
                return None
            code, path = row[:2], row[3:]
            if (code not in {'??', '!!'} and (code == '  ' or 'U' in code or code in {'AA', 'DD'}
                                  or any(mark not in ' MADTRC' for mark in code))):
                return None
            harness_path = path.startswith('.modport/') and '.git' not in Path(path).parts
            generated_path = (path == '.gradle/' or path.startswith('.gradle/')
                              or path == 'build/' or path.startswith('build/'))
            if not harness_path and not (generated_path and code in {'??', '!!'}):
                return None
            if 'R' in code or 'C' in code:
                position += 1
                if position >= len(records) - 1:
                    return None
                previous_path = records[position]
                if (not previous_path.startswith('.modport/')
                        or '.git' in Path(previous_path).parts):
                    return None
            position += 1
        harness = workspace / '.modport'
        contract = harness / 'functional-contract.json'
        if (harness.is_symlink() or not harness.is_dir() or contract.is_symlink()
                or not contract.is_file()):
            return None
        from .artifact_handoff import MAX_FILE_BYTES, MAX_HANDOFF_BYTES, MAX_TREE_ENTRIES
        from .retry_policy import is_harness_source
        sources = []
        walk_errors = []
        source_bytes = 0
        entries = 0
        for parent, names, filenames in os.walk(harness, followlinks=False,
                                                 onerror=walk_errors.append):
            entries += 1 + len(names) + len(filenames)
            if entries > MAX_TREE_ENTRIES:
                return None
            current = Path(parent)
            retained = []
            for name in sorted(names):
                path = current / name
                relative = path.relative_to(workspace).as_posix()
                if is_harness_source(relative):
                    if path.is_symlink() or not path.is_dir():
                        return None
                    if '.git' in path.relative_to(harness).parts:
                        return None
                    retained.append(name)
            names[:] = retained
            for name in sorted(filenames):
                path = current / name
                relative = path.relative_to(workspace).as_posix()
                if is_harness_source(relative):
                    if path.is_symlink() or not path.is_file():
                        return None
                    if '.git' in path.relative_to(harness).parts:
                        return None
                    measured = _bounded_harness_digest(
                        harness, path.relative_to(harness).parts, MAX_FILE_BYTES)
                    if measured is None:
                        return None
                    digest, size = measured
                    source_bytes += size
                    if (size > MAX_FILE_BYTES or source_bytes > MAX_HANDOFF_BYTES
                            or len(sources) >= MAX_TREE_ENTRIES):
                        return None
                    sources.append((relative, digest))
        if walk_errors or not sources:
            return None
        material = json.dumps({'source_head': head, 'harness_sources': sources},
                              separators=(',', ':'), sort_keys=True)
        return sha256(material.encode()).hexdigest()
    except (OSError, UnicodeError, subprocess.SubprocessError):
        return None


def load_inventory(root, ref):
    if not isinstance(ref, dict):
        return None
    try:
        path = verified_path(Path(root), ref)
        if path.stat().st_size > 16 * 1024 * 1024 or file_digest(path) != ref.get('sha256'):
            return None
        document = json.loads(path.read_text())
        return document if document.get('kind') == 'repair_inventory' else None
    except (OSError, ValueError, TypeError, AttributeError):
        return None


def publish_inventory(command, workspace, *, outputs=None, missing_inputs=None,
                      label='inventory', inherited_harness=False):
    from .repair_inventory import collect_inventory, MAX_SKIPPED
    root = Path(command.run_dir)
    directory = root / 'artifacts/repair-diagnostics' / command.command_id
    if directory.resolve() != directory.absolute() or not directory.is_relative_to(root):
        raise ValueError('unsafe repair diagnostic directory')
    logs = {}
    log_refs = {}
    incomplete_logs = []
    log_bytes = 0
    source_refs = (outputs or {}).get('artifact_refs', {})
    if len(source_refs) > 32:
        incomplete_logs.append({'path': '', 'reason': 'log_reference_limit'})
    for alias, ref in islice(source_refs.items(), 32):
        if not isinstance(ref, dict) or ref.get('media_type') != 'text/plain':
            continue
        if not isinstance(alias, str) or len(alias) > 128 or len(str(ref.get('path', ''))) > 4096:
            incomplete_logs.append({'path': '', 'reason': 'log_reference_too_long'})
            continue
        try:
            path = verified_path(root, ref)
            if path.stat().st_size > MAX_LOG_BYTES:
                incomplete_logs.append({'path': ref.get('path'), 'reason': 'log_truncated'})
                continue
            if len(logs) >= 8 or log_bytes + path.stat().st_size > 8 * 1024 * 1024:
                incomplete_logs.append({'path': ref.get('path'), 'reason': 'log_budget_exhausted'})
                continue
            if ref.get('sha256') != file_digest(path):
                incomplete_logs.append({'path': ref.get('path'), 'reason': 'log_digest_mismatch'})
                continue
            with path.open('rb') as stream:
                data = stream.read(MAX_LOG_BYTES + 1)
            if len(data) > MAX_LOG_BYTES:
                incomplete_logs.append({'path': ref['path'], 'reason': 'log_truncated'})
            logs[alias] = data[:MAX_LOG_BYTES].decode('utf-8', errors='replace')
            log_refs[alias] = {key: ref[key] for key in ('path', 'sha256', 'media_type') if key in ref}
            log_bytes += len(data)
        except (OSError, ValueError):
            incomplete_logs.append({'path': ref.get('path'), 'reason': 'log_unavailable'})
    candidate = observe_candidate(workspace, inherited_harness=inherited_harness)
    inventory = collect_inventory(Path(workspace), candidate_id=candidate,
        execution_id=command.command_id, log_texts=logs, missing_inputs=missing_inputs)
    inventory['workspace_scope'] = 'contract' if Path(workspace) == root / 'baseline' else 'migration'
    inventory['evidence_sources'] = log_refs
    if incomplete_logs:
        inventory['coverage']['complete'] = False
        inventory['coverage']['scan_complete'] = False
        inventory['coverage']['logs_complete'] = False
        inventory['coverage']['truncated'] = True
        skipped = inventory['coverage'].setdefault('skipped', [])
        skipped.extend(incomplete_logs[:max(0, MAX_SKIPPED - len(skipped))])
        inventory['coverage']['additional_log_skips'] = len(incomplete_logs)
    path = directory / (label + '.json')
    # The scanner caps its own report at 12 MiB. Bounded host additions must
    # also remain below the reader's limit; failure is diagnostic, never a gate.
    if len(json.dumps(inventory, ensure_ascii=True).encode()) > 16 * 1024 * 1024:
        raise ValueError('repair inventory exceeds the artifact read limit')
    atomic_json(path, inventory)
    ref = {'path': path.relative_to(root).as_posix(), 'sha256': file_digest(path),
           'media_type': 'application/json', 'metadata': {'candidate_id': candidate,
            'execution_id': command.command_id, 'acceptance_evidence': False}}
    return inventory, ref


def attach_inventory(command, result, workspace, *, before_candidate=None,
                     bind_verification=False, inherited_harness=False):
    """Enrich a real host result without changing its status or scheduling."""
    missing = []
    if result.error_code in {'contract_missing', 'locked_artifact_invalid'}:
        missing.append('functional_contract_lock: missing or invalid authenticated input')
    try:
        # Stage log names are reused by later builds. Keep the small host logs
        # referenced by this receipt at execution-specific paths before returning.
        refs = dict(result.outputs.get('artifact_refs', {}))
        archived_paths = {}
        root = Path(command.run_dir)
        directory = root / 'artifacts/repair-diagnostics' / command.command_id
        if directory.resolve() != directory.absolute() or not directory.is_relative_to(root):
            raise ValueError('unsafe repair diagnostic directory')
        archived_bytes = 0
        for alias, ref in list(islice(refs.items(), 32)):
            if not isinstance(ref, dict) or ref.get('media_type') != 'text/plain':
                continue
            path = verified_path(root, ref)
            size = path.stat().st_size
            if (size > MAX_LOG_BYTES or archived_bytes + size > 8 * 1024 * 1024
                    or file_digest(path) != ref.get('sha256')):
                continue
            data = path.read_bytes()
            destination = directory / ('log-' + sha256(alias.encode()).hexdigest()[:16] + '.txt')
            destination.parent.mkdir(parents=True, exist_ok=True)
            if destination.is_symlink():
                raise ValueError('unsafe repair log path')
            destination.write_bytes(data)
            refs[alias] = {**ref, 'path': destination.relative_to(root).as_posix()}
            archived_paths[ref['path']] = refs[alias]['path']
            archived_bytes += len(data)
        outputs = {**result.outputs, 'artifact_refs': refs}
        if outputs.get('log') in archived_paths:
            outputs['log'] = archived_paths[outputs['log']]
        result = replace(result, outputs=outputs)
        inventory, ref = publish_inventory(command, workspace, outputs=result.outputs,
                                           missing_inputs=missing,
                                           inherited_harness=inherited_harness)
    except (OSError, ValueError) as exc:
        return replace(result, outputs={**result.outputs, 'repair_inventory_error': str(exc),
                                       'verification_candidate_id': None})
    outputs = {**result.outputs, 'repair_inventory_ref': ref,
               'artifact_refs': {**result.outputs.get('artifact_refs', {}), 'repair_inventory': ref}}
    if bind_verification:
        candidate = inventory.get('candidate_id')
        outputs['verification_candidate_id'] = candidate if candidate == before_candidate else None
        outputs['verification_binding'] = ('host_observed_harness_candidate'
            if outputs['verification_candidate_id'] and inherited_harness else
            'host_observed_clean_candidate'
            if outputs['verification_candidate_id'] else 'unknown_or_changed_candidate')
    return replace(result, outputs=outputs)
