"""Durable SDK notification intake, independent of the migration driver."""
from pathlib import Path
from urllib.parse import quote

from .evidence import atomic_json, read_json, workspace_lock


def enabled(header, app=None):
    return (header.get('watchdog_policy', {}).get('enabled') is True
            or (app or {}).get('watchdog', {}).get('authorized') is True)


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


def install_runtime_watches(runtime, sdk, header, snapshot):
    if not enabled(header, snapshot.get('application_state')):
        return
    from dispatcher_sdk.observability import StallPolicy
    from .contracts import OperationInput
    from .workflow import agent_stage
    runtime.set_stall_notification_bridge(sdk.enqueue_stall_notification)
    watched = getattr(runtime, '_modport_watchdog_attempts', set())
    for task_id, task in snapshot['tasks'].items():
        attempt = task['attempts'][-1]
        if attempt['state'] not in {'running', 'leased'}:
            continue
        command = OperationInput.from_dict(attempt['command']['payload'])
        if command.stage_id == 'supervisor' or not agent_stage(header, command.stage_id):
            continue
        execution = runtime.kernel.get(command.command_id)
        if execution.state not in {'running', 'leased'}:
            continue
        identity = (command.command_id, execution.attempt, execution.fence)
        if identity in watched:
            continue
        runtime.watch_stall(command.command_id, StallPolicy(
            policy_id='modport-agent-response', metrics=('model',),
            sample_interval=header.get('watchdog_policy', {}).get('inactivity_seconds', 600),
            consecutive_windows=1, wait_exemptions=('tool_response',)),
            target={'run_id': header['run_id'], 'task_id': task_id,
                    'generation': snapshot.get('generation', 0),
                    'task_attempt': len(task['attempts']) - 1})
        watched.add(identity)
    runtime._modport_watchdog_attempts = watched
