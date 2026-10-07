"""Mechanical settlement of a proved-stopped active watchdog diagnosis.

This supplies a failed diagnostic, never a supervisor decision or business
repair. The ordinary watchdog retry policy owns any subsequent diagnosis.
"""
from pathlib import Path

from .application_state_storage import hydrate_run_snapshot
from .contracts import OperationInput, OperationResult, json_copy
from .evidence import atomic_json, read_json, workspace_lock
from .interrupted_execution import stopped_execution_evidence
from .kernel_runtime import operation_lock, repository_facts, validate_stage_response
from .payload_storage import pack_result, unpack_input
from .watchdog_events import enabled


_CONTROL = ('request_committed', 'command_delivered', 'execution_authority_revoked')
_CLEANUP = ('local_process_tree_reaped', 'cleanup')


def _binding(root, header, snapshot, app, episode):
    state = app.get('watchdog') or {}
    incident = state.get('active')
    if (snapshot.get('run_id') != header['run_id'] or not isinstance(incident, str)
            or state.get('episodes', {}).get(incident) is not episode
            or episode.get('request', {}).get('incident_id') != incident):
        return None
    task_id = episode.get('supervisor_task_id')
    task = snapshot.get('tasks', {}).get(task_id) or {}
    attempts = task.get('attempts') or []
    if not attempts:
        return None
    attempt = attempts[-1]
    command = OperationInput.from_dict(unpack_input(root, attempt['command']['payload']))
    request = command.payload.get('watchdog_incident') or {}
    if (command.stage_id != 'supervisor' or command.task_id != task_id or Path(command.run_dir).resolve() != root
            or command.run_id != header.get('logical_run_id', header['run_id'])
            or command.command_id != attempt['command']['execution_id']
            or request.get('incident_id') != incident
            or any(request.get(field) != episode['request'].get(field)
                   for field in ('target_task_id', 'target_execution_id'))):
        return None
    return incident, command, attempt, len(attempts) - 1


def request_cancellation(owner, snapshot, header, app, sdk, episode):
    """Persist stopped-diagnosis authority and emit an exact normal SDK cancel."""
    if (not enabled(header, app) or app.get('user_cancelled') or app.get('stop_reason')
            or sdk is None or sdk.runtime is None):
        return []
    root = Path(header['run_dir']).resolve()
    bound = _binding(root, header, snapshot, app, episode)
    if bound is None:
        return []
    incident, command, attempt, application_attempt = bound
    # Startup reconciliation already parks proved-dead workers. Healthy or
    # merely quiet diagnoses must not cause per-tick observation/audit churn.
    if attempt['state'] != 'recovery_required':
        return []
    records = episode.setdefault('supervisor_interruptions', {})
    if command.command_id in records:
        return []
    execution = sdk.runtime.kernel.get(command.command_id)
    proof = stopped_execution_evidence(root, header, sdk.runtime, execution,
        task_id=command.task_id, snapshot=snapshot, timeout=.5)
    episode['interrupted_supervisor_diagnostic'] = json_copy(proof)
    if proof.get('confirmed') is not True:
        return []
    current = sdk.runtime.kernel.get(command.command_id)
    if (current.state != execution.state or current.revision != execution.revision
            or current.attempt != execution.attempt or current.fence != execution.fence):
        return []
    reason = 'interrupted watchdog diagnosis: ' + incident + ':' + command.command_id
    records[command.command_id] = {'status': 'cancelling', 'incident_id': incident,
        'task_id': command.task_id, 'execution_id': command.command_id,
        'application_attempt': application_attempt, 'kernel_attempt': execution.attempt,
        'fence': execution.fence, 'generation': attempt.get('generation', snapshot.get('generation', 0)),
        'reason': reason, 'stopped_execution': json_copy(proof)}
    if command.command_id not in app.setdefault('cancel_sent', []):
        app['cancel_sent'].append(command.command_id)
    return [{'kind': 'cancel', 'task_id': command.task_id, 'reason': reason}]


def _evidence(root, header, sdk, snapshot, recovery, episode, bound):
    incident, operation, attempt, application_attempt = bound
    execution, effect = recovery.execution, recovery.effect
    authority = episode.get('supervisor_interruptions', {}).get(operation.command_id)
    if (not isinstance(authority, dict) or authority.get('status') != 'cancelling'
            or authority.get('incident_id') != incident or authority.get('task_id') != operation.task_id
            or authority.get('execution_id') != operation.command_id
            or authority.get('application_attempt') != application_attempt
            or authority.get('generation') != attempt.get('generation', snapshot.get('generation', 0))
            or authority.get('kernel_attempt') != execution.attempt or authority.get('fence') != execution.fence
            or recovery.run_id != header['run_id'] or recovery.task_id != operation.task_id
            or recovery.attempt != application_attempt or attempt['state'] != 'recovery_required'
            or execution.command.execution_id != operation.command_id
            or execution.command.handler_id != 'modport.supervisor'
            or execution.recovery_target_state != 'cancelled' or execution.recovery_reason != authority.get('reason')
            or effect.execution_id != operation.command_id or effect.effect_id != 'modport:' + operation.command_id
            or effect.name != 'modport.stage' or effect.attempt != execution.attempt or effect.fence != execution.fence):
        return None
    report = sdk.inspect_cancellation(header['run_id'], execution_id=operation.command_id)
    if report.truncated or len(report.executions) != 1:
        return None
    entry = report.executions[0]
    if (entry.issues or entry.effects_truncated or entry.receipts_truncated
            or entry.task_id != operation.task_id or entry.application_attempt != application_attempt
            or entry.execution_id != operation.command_id or entry.execution_revision != execution.revision
            or entry.kernel_attempt != effect.attempt or entry.fence != effect.fence
            or entry.execution_state != 'recovery_required' or entry.task_state != 'recovery_required'
            or entry.execution_result_known
            or any(getattr(entry, field).status != 'confirmed' for field in _CONTROL)
            or any(getattr(entry, field).status not in {'confirmed', 'unknown'} for field in _CLEANUP)
            or not any(row.get('effect_id') == effect.effect_id and row.get('revision') == effect.revision
                and row.get('attempt') == effect.attempt and row.get('fence') == effect.fence
                and row.get('state') == 'indeterminate' for row in entry.effects)):
        return None
    proof = stopped_execution_evidence(root, header, sdk.runtime, execution,
        task_id=operation.task_id, snapshot=snapshot, timeout=.5)
    if proof.get('confirmed') is not True or proof.get('generation') != authority['generation']:
        return None
    current = sdk.runtime.kernel.get(operation.command_id)
    if (current.state != execution.state or current.revision != execution.revision
            or current.attempt != effect.attempt or current.fence != effect.fence):
        return None
    return {'schema': 'modport.interrupted-watchdog-diagnosis.v1', 'incident_id': incident,
        'run_id': operation.run_id, 'task_id': operation.task_id, 'execution_id': operation.command_id,
        'application_attempt': application_attempt, 'kernel_attempt': effect.attempt, 'fence': effect.fence,
        'effect_id': effect.effect_id, 'effect_revision': effect.revision,
        'reason': authority['reason'], 'stopped_execution': proof,
        'cancellation_receipt_ids': list(entry.receipt_ids),
        'proof': {field: getattr(entry, field).status for field in (*_CONTROL, *_CLEANUP)}}


def _receipt(root, effect, operation, evidence):
    directory = root / 'artifacts' / 'executions' / operation.command_id
    if directory.resolve() != directory.absolute() or not directory.resolve().is_relative_to(root):
        raise ValueError('interrupted watchdog receipt directory is not contained')
    expected = dict(effect.request)
    receipt = directory / 'receipt.json'
    if receipt.exists() or receipt.is_symlink():
        if receipt.is_symlink():
            raise ValueError('interrupted watchdog receipt is a symlink')
        return pack_result(root, validate_stage_response(root, operation, read_json(receipt).get('response'), expected))
    note = directory / 'interrupted-watchdog-diagnosis.json'
    if note.is_symlink():
        raise ValueError('interrupted watchdog note is a symlink')
    if note.exists():
        previous = read_json(note)
        # A stronger subsequent cleanup report does not invalidate an already
        # proved diagnostic note. Execution/effect authority remains exact.
        stable = lambda value: {key: item for key, item in value.items()
            if key not in {'proof', 'cancellation_receipt_ids', 'stopped_execution'}}
        if stable(previous) != stable(evidence):
            raise ValueError('interrupted watchdog note identifies another cancellation')
    else:
        atomic_json(note, evidence)
    response = OperationResult('failed', operation.run_id, operation.task_id, operation.stage_id,
        operation.command_id, outputs={'watchdog_diagnosis_interrupted': True,
            'external_outcome': 'unknown', 'artifacts_complete': False, 'acceptance_status': 'unverified',
            'partial_outputs_unaccepted': True,
            'artifact_refs': {'interrupted_watchdog_diagnosis': {'path': note.relative_to(root).as_posix()}}},
        error_code='watchdog_diagnosis_interrupted',
        detail='The exact watchdog diagnosis stopped after confirmed worker-tree cleanup; no decision was accepted').to_dict()
    atomic_json(receipt, {'execution_id': operation.command_id, 'effect_request': expected,
                         'response': response, 'after': repository_facts(root, operation)})
    return pack_result(root, validate_stage_response(root, operation, response, expected))


def settle(root, header, sdk, snapshot=None):
    """Settle only the mechanically authorized current interrupted diagnosis."""
    root = Path(root).resolve()
    if snapshot is None:
        sdk.sync()
        snapshot = hydrate_run_snapshot(root, sdk.get_run(header['run_id']))
    app = snapshot.get('application_state') or {}
    if (not enabled(header, app) or app.get('user_cancelled') or app.get('stop_reason')
            or sdk.runtime is None):
        return False
    state = app.get('watchdog') or {}
    episode = state.get('episodes', {}).get(state.get('active'))
    if not isinstance(episode, dict):
        return False
    bound = _binding(root, header, snapshot, app, episode)
    if bound is None:
        return False
    record = episode.get('supervisor_interruptions', {}).get(bound[1].command_id)
    if (bound[2]['state'] != 'recovery_required' or not isinstance(record, dict)
            or record.get('status') != 'cancelling'):
        return False
    for recovery in sdk.inspect_recoveries(header['run_id']):
        if recovery.task_id != bound[1].task_id:
            continue
        evidence = _evidence(root, header, sdk, snapshot, recovery, episode, bound)
        if evidence is None:
            continue
        try:
            with workspace_lock(operation_lock(root, bound[1]), blocking=False):
                response = _receipt(root, recovery.effect, bound[1], evidence)
                sdk.resolve_effect(recovery.effect.effect_id, decision='applied', response=response,
                    expected_revision=recovery.effect.revision,
                    recovery_id='interrupted-watchdog:' + bound[1].command_id + ':' + str(recovery.effect.revision))
        except BlockingIOError:
            continue
        sdk.sync()
        return True
    return False
