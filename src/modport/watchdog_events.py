"""Durable SDK notification intake, independent of the migration driver."""
from pathlib import Path
import sqlite3
import time
from urllib.parse import quote
import warnings

from .evidence import atomic_json, read_json, workspace_lock


def enabled(header, app=None):
    if header.get('watchdog_policy', {}).get('enabled') is False:
        return False
    return (header.get('watchdog_policy', {}).get('enabled') is True
            or (app or {}).get('watchdog', {}).get('authorized') is True)


def _sqlite_contention(error):
    code = getattr(error, 'sqlite_errorcode', None)
    if isinstance(code, int):
        return code & 255 in {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED}
    # Python 3.10 has no SQLite result-code attributes. Only its exact standard
    # contention messages qualify; present permanent result codes take priority.
    return code is None and str(error).lower() in {
        'database is locked', 'database table is locked'}


def _publish_observation(header, operation, diagnostic):
    path = Path(header['run_dir']) / 'artifacts' / 'watchdog' / quote(
        header['run_id'], safe='') / 'observation-errors' / (quote(operation, safe='') + '.json')
    try:
        if path.resolve() != path.absolute():
            raise ValueError('unsafe watchdog observation diagnostic path')
        atomic_json(path, diagnostic)
    except (OSError, ValueError) as error:
        diagnostic['diagnostic_write_error'] = {'type': type(error).__name__, 'detail': str(error)}
        warnings.warn('Watchdog observation diagnostic unavailable: ' + str(error), RuntimeWarning)


def observe_optional(owner, header, operation, callback, *, monotonic=time.monotonic):
    """Defer only SQLite contention, with one bounded attempt per cooldown.

    The host retains the raw failure and retries on a later driver tick. This
    does not sleep, change an assignment deadline, or treat missing observation
    as progress. Other SDK failures retain their ordinary exception path.
    """
    observations = getattr(owner, '_modport_watchdog_observation', None)
    if observations is None:
        observations = {}
        owner._modport_watchdog_observation = observations
    previous = observations.get(operation)
    now = monotonic()
    if previous is not None and previous['status'] == 'deferred' and now < previous['retry_at']:
        return {'completed': False, 'diagnostic': previous}
    try:
        value = callback()
    except sqlite3.OperationalError as error:
        if not _sqlite_contention(error):
            raise
        failures = (previous or {}).get('failures', 0) + 1
        diagnostic = {'operation': operation, 'status': 'deferred', 'failures': failures,
                      'retry_at': monotonic() + min(30.0, 2.0 ** min(failures - 1, 5)),
                      'observed_at': time.time(), 'error_type': type(error).__name__,
                      'detail': str(error), 'sqlite_errorcode': getattr(error, 'sqlite_errorcode', None),
                      'sqlite_errorname': getattr(error, 'sqlite_errorname', None)}
        observations[operation] = diagnostic
        _publish_observation(header, operation, diagnostic)
        return {'completed': False, 'diagnostic': diagnostic}
    if previous is not None and previous['status'] == 'deferred':
        resolved = {**previous, 'status': 'resolved', 'resolved_at': time.time()}
        observations[operation] = resolved
        _publish_observation(header, operation, resolved)
    return {'completed': True, 'value': value}


def _initialization_contention(runtime, header):
    """Report the SDK's public unavailable state without reinitializing it."""
    storage = getattr(runtime, 'observation_storage', {})
    reason = storage.get('unknown_reason')
    if (storage.get('available') is not False or reason not in {
            'OperationalError: database is locked', 'OperationalError: database table is locked'}):
        return None
    observations = getattr(runtime, '_modport_watchdog_observation', None)
    if observations is None:
        observations = {}
        runtime._modport_watchdog_observation = observations
    operation = 'observation-initialization'
    diagnostic = observations.get(operation)
    if diagnostic is None:
        diagnostic = {'operation': operation, 'status': 'runtime_reopen_required',
                      'retry_mode': 'next_runtime_reopen', 'observed_at': time.time(),
                      'error_type': 'OperationalError', 'detail': reason,
                      'unknown_reason': reason, 'observation_available': False}
        observations[operation] = diagnostic
        _publish_observation(header, operation, diagnostic)
    return diagnostic


def collect_runtime_notifications(sdk, header, app=None, *, owner=None, limit=100,
                                  monotonic=time.monotonic):
    """Driver startup intake; durable SDK notifications remain queued on busy."""
    if not enabled(header, app):
        return {'enabled': False, 'completed': True}
    collected = observe_optional(sdk, header, 'notification-collect',
        lambda: sdk.collect_notifications(limit=limit), monotonic=monotonic)
    if not collected['completed']:
        return {'enabled': True, 'completed': False, 'collect': collected}
    delivered = observe_optional(sdk, header, 'notification-deliver',
        lambda: sdk.deliver_notifications(lambda notice: accept_notification(header['run_dir'], notice),
            owner=owner or 'modport-driver-start:' + header['run_id'], limit=limit), monotonic=monotonic)
    return {'enabled': True, 'completed': delivered['completed'],
            'collect': collected, 'deliver': delivered}


def accept_notification(root, notification):
    root = Path(root).resolve()
    identity = notification.get('notification_id')
    run_id = notification.get('run_id') or notification.get('target', {}).get('run_id')
    if not isinstance(identity, str) or not identity or not isinstance(run_id, str) or not run_id:
        raise ValueError('watchdog notification requires SDK notification and Run identity')
    # Encoding is a path representation, never an extra artifact identity check.
    directory = root / 'artifacts' / 'watchdog' / quote(run_id, safe='') / 'events'
    path = directory / (quote(identity, safe='') + '.json')
    if path.resolve() != path.absolute():
        raise ValueError('unsafe watchdog notification path')
    with workspace_lock(root / '.locks' / 'watchdog-events'):
        if not path.exists():
            atomic_json(path, notification)
    return path.relative_to(root).as_posix()


def notifications(root, run_id):
    root = Path(root).resolve()
    directory = root / 'artifacts' / 'watchdog' / quote(run_id, safe='') / 'events'
    if directory.resolve() != directory.absolute():
        raise ValueError('unsafe watchdog mailbox')
    for path in sorted(directory.glob('*.json')):
        if path.is_symlink():
            raise ValueError('unsafe watchdog notification')
        yield read_json(path), path.relative_to(root).as_posix()


def install_runtime_watches(runtime, sdk, header, snapshot, *, monotonic=time.monotonic):
    if not enabled(header, snapshot.get('application_state')):
        return {'enabled': False, 'completed': True, 'installed': []}
    from dispatcher_sdk.observability import StallPolicy
    from .contracts import OperationInput
    from .workflow import agent_stage
    watched = getattr(runtime, '_modport_watchdog_attempts', set())
    # Publish the live set before installing anything. A later identity or SDK
    # error must not discard subscriptions that were already installed.
    runtime._modport_watchdog_attempts = watched
    initialization = _initialization_contention(runtime, header)
    if initialization is not None:
        return {'enabled': True, 'completed': False, 'installed': [], 'deferred': [initialization]}
    bridge = observe_optional(runtime, header, 'notification-bridge',
        lambda: runtime.set_stall_notification_bridge(sdk.enqueue_stall_notification), monotonic=monotonic)
    report = {'enabled': True, 'completed': bridge['completed'], 'installed': [], 'deferred': []}
    if not bridge['completed']:
        report['deferred'].append(bridge['diagnostic'])
        return report
    for task_id, task in snapshot['tasks'].items():
        attempt = task['attempts'][-1]
        if attempt['state'] not in {'running', 'leased'}:
            continue
        command = OperationInput.from_dict(attempt['command']['payload'])
        if command.stage_id == 'supervisor' or not agent_stage(header, command.stage_id):
            continue
        observed = observe_optional(runtime, header, 'execution:' + command.command_id,
            lambda: runtime.kernel.get(command.command_id), monotonic=monotonic)
        if not observed['completed']:
            report['deferred'].append(observed['diagnostic'])
            continue
        execution = observed['value']
        if execution.state not in {'running', 'leased'}:
            continue
        identity = (command.command_id, execution.attempt, execution.fence)
        if identity in watched:
            continue
        installed = observe_optional(runtime, header, 'watch:' + ':'.join(map(str, identity)),
            lambda: runtime.watch_stall(command.command_id, StallPolicy(
            policy_id='modport-agent-response', metrics=('model',),
            sample_interval=header.get('watchdog_policy', {}).get('inactivity_seconds', 600),
            consecutive_windows=1, wait_exemptions=('tool_response',)),
            target={'run_id': header['run_id'], 'task_id': task_id,
                    'generation': snapshot.get('generation', 0),
                    'task_attempt': len(task['attempts']) - 1}), monotonic=monotonic)
        if installed['completed']:
            watched.add(identity)
            report['installed'].append(identity)
        else:
            report['deferred'].append(installed['diagnostic'])
    report['completed'] = not report['deferred']
    return report
