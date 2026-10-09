"""Failure diagnosis continues when the independent liveness watchdog is paused."""

from .contracts import OperationInput
from .watchdog_events import enabled as watchdog_enabled


TERMINAL = frozenset({'succeeded', 'failed', 'cancelled', 'timed_out', 'dead'})
FAILURE_KINDS = frozenset({'task_failure', 'terminal', 'execution_failed',
                           'run_failure', 'run_failed'})


def enabled(header):
    return header.get('definition', {}).get('failure_supervision_policy', {}).get(
        'mode') == 'supervisor_first'


def recovery_enabled(header, app=None):
    return enabled(header) or watchdog_enabled(header, app)


def diagnosed(state, execution_id):
    return any(episode.get('request', {}).get('target_execution_id') == execution_id
               and episode.get('request', {}).get('kind') in FAILURE_KINDS
               for episode in state.get('episodes', {}).values())


def next_failure(owner, snapshot, header, app):
    """Observe a fresh failed attempt before its business consumer advances."""
    if not enabled(header):
        return None
    state = app.get('watchdog', {})
    consumed = set(app.get('processed', [])) | set(app.get('cancel_sent', []))
    for task_id, task in snapshot['tasks'].items():
        attempt = task['attempts'][-1]
        if (attempt['state'] not in TERMINAL
                or attempt.get('generation', snapshot.get('generation', 0))
                    != snapshot.get('generation', 0)):
            continue
        execution_id = attempt['command']['execution_id']
        if execution_id in consumed or diagnosed(state, execution_id):
            continue
        command = OperationInput.from_dict(attempt['command']['payload'])
        if command.stage_id == 'supervisor':
            continue
        _, result = owner._flowthrough_outcome(attempt)
        if (attempt['state'] == 'succeeded' and result.status == 'completed'
                and result.outputs.get('native_goal', {}).get('status')
                    not in {'failed', 'blocked', 'cancelled', 'timed_out'}):
            continue
        return {'incident_id': f'failure.g{snapshot.get("generation", 0)}:' + execution_id,
                'kind': 'task_failure',
                'reason': result.error_code or result.detail or 'execution_' + attempt['state'],
                'target_task_id': task_id, 'target_execution_id': execution_id}
    return None


def owns_failure(app, execution_id):
    """Keep a failed child bound to its caller while the supervisor diagnoses it."""
    state = app.get('watchdog', {})
    episode = state.get('episodes', {}).get(state.get('active')) or {}
    request = episode.get('request', {})
    return (request.get('target_execution_id') == execution_id
            and request.get('kind') in FAILURE_KINDS)
