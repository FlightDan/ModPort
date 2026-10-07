"""Freeze stopped coder work before a budget continuation starts new authors."""
from __future__ import annotations

from dataclasses import replace
from hashlib import sha256
import json
from pathlib import Path

from .contracts import OperationInput, OperationResult
from .development import _git, _head
from .evidence import atomic_json, file_digest, read_json, verified_path, workspace_lock
from .goal_runtime import _previous_process_alive
from .host_candidate import collect_host_candidate, export_host_candidate


def recover_interrupted_coders(root, state, group):
    """Return immutable patch refs for stopped coders, preserving old receipts.

    Every source command must be a settled SDK attempt. Recovery writes only
    host-owned artifacts under a distinct execution identity.
    """
    root = Path(root).resolve()
    with workspace_lock(root, blocking=False):
        return _recover_interrupted_coders_locked(root, state, group)


def _recover_interrupted_coders_locked(root, state, group):
    recovered = {}
    for item in group.get('execution_payload', {}).get('interrupted_development_work', []):
        name, source_id = item['task_id'], item['command_id']
        task_key = f"coder.g{group['generation']}.{name}"
        task = state['tasks'].get(task_key, {})
        attempts = task.get('attempts', [])
        matches = []
        for index, candidate_attempt in enumerate(attempts):
            payload = (candidate_attempt.get('command', {}).get('payload')
                       if isinstance(candidate_attempt.get('command'), dict) else None)
            if isinstance(payload, dict) and payload.get('command_id') == source_id:
                matches.append((index, candidate_attempt, payload))
        if len(matches) != 1:
            raise ValueError('interrupted coder must identify exactly one SDK attempt')
        attempt_index, attempt, payload = matches[0]
        if attempt.get('state') not in {'succeeded', 'failed', 'cancelled', 'timed_out', 'dead'}:
            raise ValueError('interrupted coder must have a settled SDK attempt')
        command = OperationInput.from_dict(payload)
        logical = state.get('input', {}).get('logical_run_id', state['run_id'])
        if (command.command_id != source_id or command.task_id != task_key
                or command.run_id != logical or command.stage_id != 'coder'
                or command.payload.get('development_task', {}).get('id') != name
                or command.options.get('workspace') != item['workspace']):
            raise ValueError('interrupted coder source identity mismatch')
        workspace = root / item['workspace']
        if (workspace.is_symlink() or not workspace.is_dir()
                or workspace.resolve() != workspace.absolute()
                or not workspace.is_relative_to(root)):
            raise ValueError('interrupted coder workspace is not contained')
        raw_result = (attempt.get('result') or {}).get('value')
        result = None
        if isinstance(raw_result, dict):
            result = OperationResult.from_dict(raw_result)
            result.validate_for(command)
        artifact_refs = result.outputs.get('artifact_refs', {}) if result else {}
        native_ref = (artifact_refs.get('native_goal_state')
                      if isinstance(artifact_refs, dict) else None)
        if not isinstance(native_ref, dict):
            raise ValueError('settled coder result has no native goal state artifact reference')
        native_path = verified_path(root, native_ref)
        if native_path.stat().st_size > 16 * 1024 * 1024:
            raise ValueError('interrupted coder native goal state exceeds the recovery size limit')
        native_bytes = native_path.read_bytes()
        if (not isinstance(native_ref.get('sha256'), str)
                or sha256(native_bytes).hexdigest() != native_ref['sha256']):
            raise ValueError('interrupted coder native goal state digest differs from SDK result')
        native = json.loads(native_bytes)
        if not isinstance(native, dict):
            raise ValueError('interrupted coder native goal state is not an object')
        if (native.get('command_id') != source_id or native.get('worktree') != str(workspace)
                or native.get('status') not in {'timed_out', 'failed', 'blocked', 'cancelled'}):
            raise ValueError('interrupted coder native goal identity or terminal status is invalid')
        model_ref = artifact_refs.get('native_goal_model_result') if isinstance(artifact_refs, dict) else None
        if model_ref is not None:
            model_path = verified_path(root, model_ref)
            if model_path.stat().st_size > 2 * 1024 * 1024:
                raise ValueError('interrupted coder model result exceeds the recovery size limit')
            model_bytes = model_path.read_bytes()
            native_model_ref = native.get('model_result_ref')
            if (not isinstance(model_ref.get('sha256'), str)
                    or sha256(model_bytes).hexdigest() != model_ref['sha256']
                    or not isinstance(native_model_ref, dict)
                    or native_model_ref.get('path') != model_ref.get('path')
                    or native_model_ref.get('sha256') != model_ref.get('sha256')):
                raise ValueError('interrupted coder model result differs from native goal state')
        setup_path = root / 'artifacts' / 'executions' / source_id / 'coder-setup.json'
        if (setup_path.is_symlink() or not setup_path.is_file()
                or setup_path.resolve() != setup_path.absolute()
                or setup_path.stat().st_size > 2 * 1024 * 1024):
            raise ValueError('interrupted coder setup record exceeds the recovery size limit')
        setup = read_json(setup_path)
        record = setup.get('record')
        if not isinstance(record, dict) or setup.get('sha256') != sha256(json.dumps(
                record, sort_keys=True, separators=(',', ':')).encode()).hexdigest():
            raise ValueError('interrupted coder setup digest mismatch')
        if (record.get('run_id') != command.run_id or record.get('command_id') != source_id
                or record.get('workspace') != item['workspace']
                or record.get('task_id') != task_key
                or record.get('generation') != group['generation']
                or record.get('base_commit') != group['base']
                or record.get('task') != command.payload['development_task']
                or record.get('plan_ref') != command.artifact_refs.get('development_plan')
                or record.get('dependency_refs') != [ref for ref in command.payload.get('dependency_patches', [])
                                                       if isinstance(ref, dict)]):
            raise ValueError('interrupted coder setup differs from SDK source command')
        cleanup_receipt = None
        source_evidence = None
        cleanup_unconfirmed = (native.get('cleanup_unconfirmed') is True
                               and native.get('producer_stopped') is not True)
        if cleanup_unconfirmed:
            from .opencode_recovery import (confirmed_cleanup_receipt,
                                            validate_cleanup_source_evidence)
            if (result is None or result.status == 'completed'
                    or not isinstance(raw_result, dict)
                    or not isinstance(result.outputs.get('native_goal'), dict)
                    or result.outputs['native_goal'].get('cleanup_unconfirmed') is not True):
                raise ValueError('unconfirmed cleanup has no matching failed SDK result')
            source_evidence = validate_cleanup_source_evidence(
                root, result=result, command=command, native=native, setup=record)
            cleanup_receipt = confirmed_cleanup_receipt(
                root, state=state, command=command, attempt_index=attempt_index,
                raw_result=raw_result, native=native, setup=record)
            if cleanup_receipt is None:
                raise ValueError('OpenCode cleanup has no confirmed receipt bound to this SDK attempt')
        elif (native.get('producer_stopped') is not True or _previous_process_alive(native)):
            raise ValueError('interrupted coder producer is not proven stopped')
        recovery_id = source_id + ':host-recovery'
        recovery = replace(command, command_id=recovery_id,
                           options={key: value for key, value in command.options.items()
                                    if key != 'deadline_epoch'})
        start = record.get('start_commit')
        if (not isinstance(start, str) or len(start) != 40
                or _git(recovery, workspace, 'rev-parse', start + '^{tree}').stdout.strip()
                != record.get('start_tree')):
            raise ValueError('interrupted coder start tree mismatch')
        _git(recovery, workspace, 'merge-base', '--is-ancestor', start, _head(recovery, workspace))
        candidate = collect_host_candidate(recovery, workspace, start, name,
            report_paths=(record.get('goal', {}).get('acceptance_report'),)
            if isinstance(record.get('goal'), dict)
            and isinstance(record['goal'].get('acceptance_report'), str) else ())
        destination = root / 'artifacts' / 'executions' / recovery_id / 'coder.patch'
        content = export_host_candidate(recovery, candidate, destination)
        metadata = {'task_id': name, 'base': group['base'], 'generation': group['generation'],
                    'start': start, 'head': candidate.head, 'paths': candidate.paths,
                    'source_run_id': state['run_id'], 'source_revision': state['revision'],
                    'source_command_id': source_id, 'source_workspace': item['workspace'],
                    'source_start_tree': record['start_tree'],
                    'safe_projection_sha256': candidate.record['safe_projection_sha256']}
        ref = {'path': destination.relative_to(root).as_posix(),
               'sha256': sha256(content).hexdigest(), 'metadata': metadata}
        verified_path(root, ref)
        capture_record = {
            'schema': 'modport.candidate-capture.v1', 'status': 'captured',
            'source_run_id': state['run_id'], 'source_revision': state['revision'],
            'source_attempt_index': attempt_index, 'source_command_id': source_id,
            'host_recovery_command_id': recovery_id,
            'workspace': item['workspace'], 'base_commit': group['base'],
            'generation': group['generation'], 'start_commit': start,
            'start_tree': record['start_tree'], 'head': candidate.head,
            'paths': candidate.paths, 'patch_ref': ref,
            'safe_projection_sha256': candidate.record['safe_projection_sha256'],
            'model_result_ref': (source_evidence.get('model_result_ref')
                                 if source_evidence else native.get('model_result_ref')),
            'source_candidate_capture_ref': (source_evidence.get('candidate_capture_ref')
                                             if source_evidence else None),
            'cleanup_receipt_ref': (cleanup_receipt.get('receipt_ref')
                                    if cleanup_receipt else None),
        }
        capture_path = root / 'artifacts' / 'executions' / recovery_id / 'candidate-capture.json'
        capture_envelope = {'record': capture_record, 'sha256': sha256(json.dumps(
            capture_record, sort_keys=True, separators=(',', ':')).encode()).hexdigest()}
        if capture_path.exists() or capture_path.is_symlink():
            if (capture_path.is_symlink() or not capture_path.is_file()
                    or capture_path.stat().st_size > 8 * 1024 * 1024
                    or json.loads(capture_path.read_text()) != capture_envelope):
                raise ValueError('existing host recovery candidate receipt differs')
        else:
            atomic_json(capture_path, capture_envelope)
        capture_ref = {'path': capture_path.relative_to(root).as_posix(),
                       'sha256': file_digest(capture_path),
                       'media_type': 'application/json',
                       'metadata': {'execution_id': recovery_id, 'status': 'captured'}}
        ref['metadata'] = {**metadata, 'candidate_capture_ref': capture_ref,
                           **({'cleanup_receipt_ref': cleanup_receipt['receipt_ref']}
                              if cleanup_receipt else {})}
        recovered[name] = ref
    return recovered


def carry_overwritten_coder_patches(root, previous, state, group):
    """Keep a settled coder patch when a later failed rework replaced its result.

    Prefer the coder's own successful SDK attempt in this segment. Only use
    the predecessor segment when this one did not run that coder at all.
    """
    from .development import _check_ref
    from .contracts import OperationResult

    carried = {}
    previous_app = previous.get('application_state') or {}
    current_app = state.get('application_state') or {}
    for task in group['tasks']:
        name = task['id']
        if name in group['scheduled']:
            continue
        key = f"coder.g{group['generation']}.{name}"
        replaced = current_app.get('effective', {}).get(key)
        if not isinstance(replaced, dict) or replaced.get('error_code') != 'rework_execution_failed':
            continue
        prior = None
        source = previous
        attempts = state.get('tasks', {}).get(key, {}).get('attempts', [])
        if attempts:
            attempt = attempts[-1]
            if attempt.get('state') == 'succeeded':
                prior = (attempt.get('result') or {}).get('value')
                source = state
                command = (attempt.get('command') or {}).get('payload')
                if not isinstance(command, dict) or not isinstance(prior, dict):
                    raise ValueError('settled coder SDK result is missing')
                original = OperationInput.from_dict(command)
                if (original.command_id != prior.get('command_id') or original.task_id != key
                        or original.stage_id != 'coder'):
                    raise ValueError('settled coder SDK command differs from result')
        if prior is None:
            prior = previous_app.get('effective', {}).get(key)
        if not isinstance(prior, dict):
            continue
        result = OperationResult.from_dict(prior)
        logical = source.get('input', {}).get('logical_run_id', source['run_id'])
        if (result.task_id != key or result.stage_id != 'coder'
                or result.run_id != logical or result.status != 'completed'):
            raise ValueError('previous coder identity differs from settled SDK segment')
        ref = result.outputs.get('artifact_refs', {}).get('coder_patch')
        if source is state and (not isinstance(ref, dict)
                                or ref.get('metadata', {}).get('execution_id') != result.command_id):
            raise ValueError('settled coder patch provenance differs from SDK result')
        command = OperationInput(state['run_id'], key, 'coder',
            state['run_id'] + ':recover-prior:' + name, str(root),
            payload={'goal_scope': 'migration'}, options={'workflow_version': 17})
        _check_ref(command, ref, task, group['base'], group['generation'])
        carried[name] = ref
    return carried


def carry_failed_rework_instructions(state, group):
    """Keep the exact explicit repair request behind an overwritten coder."""
    current = state.get('application_state') or {}
    requests = current.get('review_rework', {}).get('requests', {})
    if not isinstance(requests, dict):
        return {}
    recovered = {}
    for task in group['tasks']:
        name = task['id']
        if name in group['scheduled']:
            continue
        target = f"coder.g{group['generation']}.{name}"
        effective = current.get('effective', {}).get(target, {})
        if not isinstance(effective, dict) or effective.get('error_code') != 'rework_execution_failed':
            continue
        candidates = [item for item in requests.values()
                      if isinstance(item, dict) and item.get('target_agent') == target
                      and item.get('state') == 'failed'
                      and isinstance(item.get('instructions'), str)
                      and item['instructions'].strip()
                      and isinstance(item.get('request_id'), str)]
        if candidates:
            latest = max(candidates, key=lambda row: row.get('sequence', 0))
            recovered[name] = {'request_id': latest['request_id'],
                               'instructions': latest['instructions'],
                               'failure': latest.get('error', '')}
    return recovered
