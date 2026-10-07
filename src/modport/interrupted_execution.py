"""Conservative reconciliation of a stopped driver's exact SDK executions.

This host repair never runs an author, resolves an Effect, or supplies a
cancellation cleanup receipt. Call after runtime.reap() and sdk.sync(), before
the first policy tick, including the explicit recovery entry point.
"""
from __future__ import annotations

import errno
import os
from pathlib import Path
import time
import warnings

from dispatcher_sdk.execution_kernel import ExecutionError

from .evidence import atomic_json
from .application_state_storage import hydrate_run_snapshot
from .opencode_recovery import _proc_mount_matches_namespace
from .payload_storage import unpack_input
from .run_monitor import pid_namespace


MAX_EXECUTIONS = 100
MAX_EFFECTS = 100
OBSERVATION_SECONDS = .5
RECONCILIATION_SECONDS = 5.0
UNFINISHED_EFFECTS = frozenset({'prepared', 'performing', 'indeterminate'})


def _driver_state(registration):
    """Prove death in the registered namespace, retaining PID birth binding."""
    birth = registration.get('birth_identity')
    namespace = registration.get('namespace')
    pid = registration.get('pid')
    if (namespace is None or namespace != pid_namespace()
            or not _proc_mount_matches_namespace(namespace)):
        return 'unknown', 'driver_pid_namespace_unverified'
    if (type(pid) is not int or pid <= 0 or not isinstance(birth, dict)
            or birth.get('pid') != pid or birth.get('namespace') != namespace
            or type(birth.get('start_ticks')) is not int or birth['start_ticks'] < 0):
        return 'unknown', 'driver_birth_identity_unavailable'
    if pid == os.getpid():
        return 'alive', 'current_driver_process'
    try:
        with open(f'/proc/{pid}/stat', 'rb') as stream:
            raw = stream.read(4097)
        if len(raw) > 4096:
            return 'unknown', 'driver_process_stat_unrecognized'
        fields = raw.rsplit(b')', 1)[1].split()
        ticks = int(fields[19])
        if ticks != birth['start_ticks']:
            return 'dead', 'registered_driver_birth_replaced'
        if fields[0] in {b'Z', b'X'}:
            return 'dead', 'registered_driver_exited'
        return 'alive', 'registered_driver_alive'
    except (IndexError, ValueError):
        return 'unknown', 'driver_process_stat_unrecognized'
    except OSError as error:
        # An inaccessible /proc record is not death. ESRCH in the confirmed
        # namespace is the only fallback proof when birth cannot be read.
        try:
            os.kill(pid, 0)
        except OSError as probe:
            if probe.errno == errno.ESRCH:
                return 'dead', 'registered_driver_not_found'
        return 'unknown', f'driver_process_inaccessible:{type(error).__name__}:{error}'


def _exact(identity, execution):
    return (isinstance(identity, dict)
            and identity.get('execution_id') == execution.execution_id
            and identity.get('attempt') == execution.attempt
            and identity.get('fence') == execution.fence)


def _application_binding(root, header, task_id, identity, execution, snapshot):
    if snapshot.get('run_id') != header['run_id']:
        return False
    task = snapshot.get('tasks', {}).get(task_id) or {}
    attempts = task.get('attempts') or []
    if not attempts:
        return False
    current = attempts[-1]
    command = current.get('command') or {}
    application_bound = identity.get('run_id') is not None or identity.get('task_id') is not None
    if (command.get('execution_id') != execution.execution_id
            or current.get('state') != execution.state
            or identity.get('task_attempt') not in {None, len(attempts) - 1}
            or (application_bound and identity.get('generation', 0)
                != current.get('generation', snapshot.get('generation', 0)))):
        return False
    kernel_command = execution.command.to_dict()
    if ({key: value for key, value in command.items() if key != 'payload'}
            != {key: value for key, value in kernel_command.items() if key != 'payload'}
            or unpack_input(root, command.get('payload')) != unpack_input(root, kernel_command['payload'])):
        return False
    if identity.get('run_id') not in {None, header['run_id']}:
        return False
    if identity.get('task_id') not in {None, task_id}:
        return False
    # SDK 0.7.1 may omit orchestration identity when its stores are separate.
    # The current SDK task membership binds the physical execution Run.
    # Its actual producer retains logical provenance in operation/correlation
    # IDs; it does not add an execution_run_id option to the frozen payload.
    payload = unpack_input(root, execution.command.payload)
    logical_run = header.get('logical_run_id', header['run_id'])
    return (payload.get('run_id') == logical_run
            and execution.command.correlation_id == logical_run
            and payload.get('task_id') == task_id
            and payload.get('command_id') == execution.execution_id)


def _cleanup_receipt(observation, execution):
    # Inspect the original receipt rather than the merged worker state: a
    # worker/supervisor exit status alone cannot prove descendant cleanup.
    for phase in observation.get('phases', []):
        details = phase.get('details') or {}
        # ActivityRecorder events preserve their phase/details envelope;
        # durable settlement notes expose the evidence directly.
        if details.get('phase') == 'process_cleanup' and isinstance(details.get('details'), dict):
            details = details['details']
        if (_exact(phase, execution) and phase.get('phase') == 'process_cleanup'
                and details.get('state') == 'confirmed'
                and details.get('source') == 'runtime_supervisor_reaped'):
            return {'source': details['source'], 'state': details['state'],
                    'captured_at': phase.get('captured_at')}
    for note in (observation.get('diagnostics') or {}).get('notes', []):
        evidence = note.get('evidence') or {}
        if (_exact(note.get('identity'), execution) and note.get('phase') == 'process_cleanup'
                and evidence.get('state') == 'confirmed'
                and evidence.get('source') == 'runtime_supervisor_reaped'):
            return {'source': evidence['source'], 'state': evidence['state'],
                    'created_at': note.get('created_at')}
    return None


def stopped_execution_evidence(root, header, runtime, execution, *, task_id, snapshot,
                               timeout=OBSERVATION_SECONDS):
    """Independently revalidate stopped-tree facts without changing SDK state.

    The returned host proof does not replace an SDK cancellation receipt or
    claim that remote cleanup succeeded. It can also observe a parked or
    cancelled execution after the public cancellation path revoked authority.
    Supply the current snapshot read through sdk.get_run; its task membership
    binds the physical Run independently from logical operation provenance.
    """
    proof = {'confirmed': False, 'execution_id': execution.execution_id,
             'attempt': execution.attempt, 'fence': execution.fence}
    try:
        observation = runtime.observe(execution.execution_id, attempt=execution.attempt,
                                      fence=execution.fence, timeout=timeout)
        identity = observation.get('identity')
        authority = observation.get('execution') or {}
        if observation.get('complete') is not True:
            proof['observation_diagnostic'] = {key: observation.get(key) for key in
                ('unknown_reason', 'error', 'settlement_obligations_error', 'truncated')}
        if (observation.get('current') is not True
                or not _exact(identity, execution)
                or observation.get('execution_id') != execution.execution_id
                or authority.get('attempt') != execution.attempt
                or authority.get('fence') != execution.fence
                or authority.get('state') != execution.state
                or not _application_binding(root, header, task_id, identity, execution, snapshot)):
            proof.update(reason='execution_observation_incomplete_or_stale',
                observation_error={key: observation.get(key) for key in
                    ('unknown_reason', 'error', 'settlement_obligations_error')})
            return proof
        # Separate unmanaged SDK stores omit application identity and leave
        # generation at its default zero even after same-Run recovery. Copy
        # the generation from the exact current SDK task membership verified
        # above; explicitly bound observation generations still must match.
        current_attempt = snapshot['tasks'][task_id]['attempts'][-1]
        proof['generation'] = current_attempt.get('generation', snapshot.get('generation', 0))
        drivers = [process for process in observation.get('processes', [])
                   if process.get('process_id') == 'driver'
                   and (process.get('registration') or {}).get('role') == 'driver'
                   and _exact(process, execution)]
        if len(drivers) != 1:
            proof['reason'] = 'registered_driver_unavailable'
            return proof
        registration = drivers[0]['registration']
        proof['driver'] = registration
        driver_state, reason = _driver_state(registration)
        proof.update(driver_state=driver_state, reason=reason)
        if driver_state != 'dead':
            return proof
        cleanup = _cleanup_receipt(observation, execution)
        if cleanup is None:
            proof['reason'] = 'worker_tree_cleanup_unknown'
            return proof
        proof.update(confirmed=True, cleanup=cleanup, reason='stopped_worker_tree_confirmed')
    except Exception as error:
        proof.update(reason='interrupted_execution_observation_error',
            error={'type': type(error).__name__, 'detail': str(error)[:3000],
                   'sqlite_errorcode': getattr(error, 'sqlite_errorcode', None),
                   'sqlite_errorname': getattr(error, 'sqlite_errorname', None)})
    return proof


def _publish(root, report):
    path = root / 'artifacts' / 'monitor' / 'interrupted-executions.json'
    try:
        if path.resolve() != path.absolute():
            raise ValueError('unsafe interrupted execution diagnostic path')
        atomic_json(path, report)
    except (OSError, ValueError) as error:
        report['diagnostic_write_error'] = {'type': type(error).__name__, 'detail': str(error)[:3000]}
        warnings.warn('Interrupted execution diagnostic unavailable: ' + str(error), RuntimeWarning)
    return report


def reconcile_interrupted_executions(root, header, runtime, sdk):
    """Park proved-stopped attempts using public, fenced SDK control APIs.

    Incomplete observations remain diagnostics. Uncertain Effects are parked
    for the existing supervisor-first recovery policy; none are marked applied.
    Cancellation remains with the SDK and the application's cancellation path.
    """
    root = Path(root).resolve()
    started = time.monotonic()
    report = {'run_id': header['run_id'], 'observed_at': time.time(),
              'complete': True, 'executions': [], 'changed': 0}
    try:
        snapshot = hydrate_run_snapshot(root, sdk.get_run(header['run_id']))
        if snapshot['run_id'] != header['run_id']:
            raise ValueError('interrupted execution snapshot belongs to another Run')
        report['generation'] = snapshot.get('generation', 0)
        app = snapshot.get('application_state') or {}
        cancelled = app.get('user_cancelled') is True or app.get('stop_reason') == 'user_cancelled'
        active = [(task_id, task['attempts'][-1]) for task_id, task in snapshot['tasks'].items()
                  if task.get('attempts') and task['attempts'][-1]['state'] in {'running', 'leased'}]
        if len(active) > MAX_EXECUTIONS:
            report.update(complete=False, unknown_reason='execution_scan_limit')
        for task_id, attempt in active[:MAX_EXECUTIONS]:
            remaining = RECONCILIATION_SECONDS - (time.monotonic() - started)
            if remaining <= 0:
                report.update(complete=False, unknown_reason='reconciliation_time_limit')
                break
            execution_id = attempt['command']['execution_id']
            item = {'task_id': task_id, 'execution_id': execution_id, 'status': 'unknown'}
            report['executions'].append(item)
            try:
                execution = runtime.kernel.get(execution_id)
                item.update(attempt=execution.attempt, fence=execution.fence)
                if execution.state not in {'running', 'leased'}:
                    item.update(status='unchanged', reason='execution_already_settled')
                    continue
                proof = stopped_execution_evidence(root, header, runtime, execution,
                    task_id=task_id, snapshot=snapshot, timeout=min(OBSERVATION_SECONDS, remaining))
                item.update(proof)
                if not proof['confirmed']:
                    continue
                if proof['generation'] != attempt.get('generation', snapshot.get('generation', 0)):
                    item.update(confirmed=False, reason='execution_observation_incomplete_or_stale')
                    continue
                registration = proof['driver']
                cleanup = proof['cleanup']
                if cancelled or 'cancel_reason' in attempt:
                    item['reason'] = 'cancellation_requires_existing_sdk_settlement'
                    continue
                current = runtime.kernel.get(execution_id)
                if (current.state != execution.state or current.lease != execution.lease
                        or current.revision != execution.revision):
                    item['reason'] = 'execution_changed_before_reconciliation'
                    continue
                effects = runtime.kernel.effect_ids_for_attempt(execution_id,
                    execution.attempt, execution.fence, states=UNFINISHED_EFFECTS)
                if len(effects) > MAX_EFFECTS:
                    item['reason'] = 'effect_scan_limit'
                    continue
                if effects:
                    if execution.state != 'running':
                        item['reason'] = 'leased_execution_effect_requires_sdk_recovery'
                        continue
                    updated = runtime.kernel.require_effect_recovery(execution.lease, effects[0],
                        timeout_seconds=max(.001, RECONCILIATION_SECONDS - (time.monotonic() - started)))
                    item.update(status='recovery_required', effects=effects)
                else:
                    updated = runtime.kernel.dead_letter(execution.lease, ExecutionError(
                        code='worker_interrupted', message='Registered driver exited after confirmed worker-tree cleanup',
                        retryable=False, details={'task_id': task_id, 'attempt': execution.attempt,
                            'fence': execution.fence, 'driver': registration, 'cleanup': cleanup}))
                    item['status'] = updated.state
                report['changed'] += 1
                sdk.sync()
            except Exception as error:
                item.update(status='unknown', reason='interrupted_execution_control_error',
                    error={'type': type(error).__name__, 'detail': str(error)[:3000],
                           'sqlite_errorcode': getattr(error, 'sqlite_errorcode', None),
                           'sqlite_errorname': getattr(error, 'sqlite_errorname', None)})
    except Exception as error:
        report.update(complete=False, unknown_reason='interrupted_execution_scan_unavailable',
                      error={'type': type(error).__name__, 'detail': str(error)[:3000]})
    return _publish(root, report)
