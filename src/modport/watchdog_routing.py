"""Supervisor decisions under the Run's existing SDK authority and budgets."""
from pathlib import Path

from .contracts import OperationInput, json_copy
from .watchdog_events import enabled, notifications
from .watchdog_supervisor import validate_watchdog_decision

TERMINAL = {'succeeded', 'failed', 'cancelled', 'timed_out', 'dead'}


def successful(owner, attempt):
    if attempt['state'] != 'succeeded':
        return False
    _, outcome = owner._flowthrough_outcome(attempt)
    return (outcome.status == 'completed' and outcome.outputs.get('native_goal', {}).get('status')
            not in {'failed', 'blocked', 'cancelled', 'timed_out'})


def exhausted(owner, header, app):
    from .token_budget import read_token_budget
    deadline = owner._effective_deadline(header, app)
    if deadline is None or owner.clock() >= deadline:
        return 'wall_clock_budget_exhausted'
    limit = header['request']['budget'].get('max_agent_assignments')
    if limit is not None and app.get('agent_assignments', 0) >= limit:
        return 'agent_assignment_budget_exhausted'
    if read_token_budget(header['run_dir'])['exhausted']:
        return 'token_budget_exhausted'
    return None


def begin(owner, snapshot, header, app, incident):
    state = app.setdefault('watchdog', {})
    state['authorized'] = True
    state.setdefault('seen', [])
    state.setdefault('episodes', {})
    identity = incident['incident_id']
    if state.get('active') or identity in state['episodes']:
        return []
    request = {**json_copy(incident), 'known_task_ids': list(snapshot['tasks']),
               'budget_context': {'deadline_epoch': owner._effective_deadline(header, app),
                   'now': owner.clock(), 'agent_assignments': app.get('agent_assignments', 0),
                   'max_agent_assignments': header['request']['budget'].get('max_agent_assignments')}}
    request.setdefault('target_task_id', None)
    request.setdefault('target_execution_id', None)
    request.setdefault('evidence_refs', [])
    number = len(state['episodes']) + 1
    task_id = f'watchdog.g{snapshot.get("generation", 0)}.{number}'
    workspace = 'workspaces/watchdog/' + task_id
    (Path(header['run_dir']) / workspace).mkdir(parents=True, exist_ok=True)
    payload = {'watchdog_incident': request}
    upstream = {}
    target = snapshot['tasks'].get(request['target_task_id'])
    if target:
        command, outcome = owner._flowthrough_outcome(target['attempts'][-1])
        request['target_result'] = outcome.to_dict()
        request['target_state'] = target['attempts'][-1]['state']
        upstream[command.task_id] = outcome.to_dict()
        upstream[command.stage_id] = outcome.to_dict()
        from .evidence import atomic_json
        evidence_path = Path(header['run_dir']) / 'artifacts' / 'watchdog' / task_id / 'target-attempt.json'
        atomic_json(evidence_path, target['attempts'][-1])
        request['evidence_refs'].append({'path': evidence_path.relative_to(header['run_dir']).as_posix()})
        request['target_input'] = {'stage_id': command.stage_id, 'task_id': command.task_id,
            'workspace': command.options.get('workspace'), 'artifact_refs': command.artifact_refs}
        if (command.stage_id == 'coder' and command.payload.get('goal_scope') != 'contract'
                and command.payload.get('development_task')
                and command.artifact_refs.get('development_plan')):
            payload['diagnostic_repair_targets'] = [{
                'task_id': command.payload['development_task']['id'],
                'plan_ref': command.artifact_refs['development_plan'],
                'source_workspace': command.options.get('workspace'),
                'source_execution_id': command.command_id}]
    operations = owner._schedule(snapshot, header, app, 'supervisor', task_id=task_id,
        dependencies=[], activate=False, payload=payload, upstream_overrides=upstream,
        extra_options={'workspace': workspace})
    if any(op['kind'] in {'add_task', 'new_attempt'} for op in operations):
        episode = {'request': request, 'status': 'pending', 'supervisor_task_id': task_id,
                   'created_at': owner.clock()}
        if target and target['attempts'][-1]['state'] not in TERMINAL:
            from .progress_watchdog import capture_progress
            baseline = capture_progress(header['run_dir'], command)
            baseline_path = evidence_path.parent / 'progress-baseline.json'
            atomic_json(baseline_path, baseline)
            episode['baseline'] = baseline_path.relative_to(header['run_dir']).as_posix()
        state['episodes'][identity] = episode
        state['active'] = identity
    return operations


def _close(state, episode, status):
    episode['status'] = status
    state['active'] = None


def _retry_diagnosis(owner, snapshot, header, app, episode, reason):
    episode['diagnostic'] = reason
    # A malformed model reply grants no authority. A bounded delay avoids
    # spending the remaining assignments in a tight parser retry loop.
    if owner.clock() < episode.setdefault('retry_at', owner.clock() + 60):
        return []
    previous = snapshot['tasks'][episode['supervisor_task_id']]['attempts'][-1]
    previous_command = OperationInput.from_dict(previous['command']['payload'])
    payload = {'watchdog_incident': {**episode['request'], 'prior_recovery': reason}}
    for key in ('diagnostic_repair_targets', 'diagnostic_repair_refs'):
        if key in previous_command.payload:
            payload[key] = json_copy(previous_command.payload[key])
    operations = owner._schedule(snapshot, header, app, 'supervisor',
        task_id=episode['supervisor_task_id'], dependencies=[], activate=False,
        payload=payload, upstream_overrides=previous_command.upstream_results,
        extra_options={'workspace': 'workspaces/watchdog/' + episode['supervisor_task_id']})
    episode.pop('retry_at', None)
    episode.pop('decision', None)
    episode['status'] = 'pending'
    return operations


def decide(owner, snapshot, header, app, sdk):
    """Return (operations, owns_business_routing) while diagnosis is pending."""
    if not enabled(header, app) or app.get('user_cancelled') or app.get('stop_reason'):
        return [], False
    state = app.setdefault('watchdog', {'seen': [], 'episodes': {}})
    if not state.get('active'):
        for event, ref in notifications(header['run_dir'], header['run_id']):
            identity = event['notification_id']
            if identity in state.setdefault('seen', []):
                continue
            state['seen'].append(identity)
            target = event.get('target') or {}
            if event.get('generation', target.get('generation', 0)) != snapshot.get('generation', 0):
                continue
            kind = event.get('kind')
            task_id = event.get('task_id', target.get('task_id'))
            execution_id = event.get('execution_id', (event.get('disposition') or {}).get('execution_id'))
            task = snapshot['tasks'].get(task_id)
            if task:
                current = task['attempts'][-1]
                if current['command']['execution_id'] != execution_id:
                    continue
                command = OperationInput.from_dict(current['command']['payload'])
                if command.stage_id == 'supervisor':
                    continue
                if kind == 'terminal' and successful(owner, current):
                    continue
            elif kind != 'driver_lost':
                continue
            if kind not in {'stalled', 'terminal', 'recovery_required', 'driver_lost', 'no_useful_progress'}:
                continue
            operations = begin(owner, snapshot, header, app, {
                'incident_id': identity, 'kind': kind,
                'reason': event.get('reason') or f'SDK watchdog observation: {kind}',
                'target_task_id': task_id, 'target_execution_id': execution_id,
                'evidence_refs': [{'path': ref}], 'notification': event})
            return operations, True
        return [], False
    episode = state['episodes'][state['active']]
    operations = owner._review_rework_decision(snapshot, header, app)
    if app.get('stop_reason'):
        return operations, False
    supervisor = snapshot['tasks'].get(episode['supervisor_task_id'])
    if not supervisor or supervisor['attempts'][-1]['state'] not in TERMINAL:
        return operations, True
    if 'decision' not in episode:
        command, result = owner._flowthrough_outcome(supervisor['attempts'][-1])
        from .diagnostic_repair_routing import register
        register(app, command, result)
        episode['supervisor_result'] = result.to_dict()
        try:
            if result.status != 'completed':
                raise ValueError(result.detail or result.error_code or 'supervisor failed')
            episode['decision'] = validate_watchdog_decision(
                result.outputs.get('watchdog_decision'), episode['request'])
        except (ValueError, TypeError, KeyError) as error:
            return operations + _retry_diagnosis(owner, snapshot, header, app, episode, str(error)), True
    decision = episode['decision']
    request = episode['request']
    task = snapshot['tasks'].get(request['target_task_id'])
    target = task['attempts'][-1] if task else None
    if target and target['command']['execution_id'] != request['target_execution_id']:
        _close(state, episode, 'superseded')
        return operations, False
    action = decision['action']
    failed_route = request['kind'] in {'run_failure', 'run_failed'}
    if action == 'stop':
        if decision['stop_category'] == 'budget_exhausted' and not exhausted(owner, header, app):
            return operations + _retry_diagnosis(owner, snapshot, header, app, episode,
                'Host budget remains available; unsupported stop rejected.'), True
        state['stop_confirmed'] = {'category': decision['stop_category'],
            'reason': decision['reason'], 'incident_id': request['incident_id']}
        app['stop_reason'], app['stop_state'] = 'watchdog_' + decision['stop_category'], 'failed'
        _close(state, episode, 'stopped')
        return operations, False
    if action == 'wait':
        if not decision['wait_for'] or any(snapshot['tasks'][key]['attempts'][-1]['state'] not in TERMINAL
                                          for key in decision['wait_for']):
            episode['status'] = 'waiting'
            return operations, True
        return operations + _retry_diagnosis(owner, snapshot, header, app, episode,
            'Named prerequisites settled; inspect their actual results before resuming.'), True
    if action == 'continue':
        if failed_route or target and (target['state'] == 'recovery_required'
                       or target['state'] in TERMINAL and not successful(owner, target)):
            return operations + _retry_diagnosis(owner, snapshot, header, app, episode,
                'Target is settled unsuccessfully; supply a concrete recovery instruction or real prerequisite.'), True
        _close(state, episode, 'continued')
        return operations, False
    if not target:
        # Driver restoration has already occurred before this assignment.
        _close(state, episode, 'driver_resumed')
        return operations, False
    if successful(owner, target) and not failed_route:
        _close(state, episode, 'completed_during_diagnosis')
        return operations, False
    command, outcome = owner._flowthrough_outcome(target)
    if target['state'] not in TERMINAL:
        if episode.get('status') == 'cancelling' or (target['state'] == 'recovery_required'
                and episode.get('status') == 'cancellation_authorized'):
            episode['status'] = 'cancelling'
            return operations, True
        from .progress_watchdog import capture_progress, compare_progress
        from .evidence import read_json
        baseline_path = Path(header['run_dir']) / episode['baseline'] if episode.get('baseline') else None
        if baseline_path and (baseline_path.resolve() != baseline_path.absolute()
                              or not baseline_path.resolve().is_relative_to(Path(header['run_dir']).resolve())):
            raise ValueError('unsafe watchdog progress baseline')
        baseline = read_json(baseline_path) if baseline_path else None
        current = capture_progress(header['run_dir'], command, baseline)
        if baseline and compare_progress(baseline, current)['useful_progress']:
            _close(state, episode, 'superseded_by_progress')
            return operations, False
        event = request.get('notification', {})
        if event.get('kind') == 'stalled':
            if sdk is None or sdk.runtime is None:
                episode['diagnostic'] = 'SDK stall authority is unavailable'
                return operations, True
            if episode.get('status') != 'cancellation_authorized':
                # Commit the supervisor's exact authority before the public SDK
                # call, whose cancellation may advance SDK state concurrently.
                episode['status'] = 'cancellation_authorized'
                return operations, True
            try:
                sdk.runtime.cancel_if_stalled(event, reason='watchdog: ' + decision['reason'])
            except Exception as error:
                from dispatcher_sdk.execution_kernel import CASConflictError, StaleFenceError, InvalidStateTransitionError
                if not isinstance(error, (CASConflictError, StaleFenceError, InvalidStateTransitionError)):
                    raise
                episode['diagnostic'] = str(error)
                _close(state, episode, 'stale_stall_decision')
                return operations, False
        else:
            operations.append({'kind': 'cancel', 'task_id': request['target_task_id'],
                               'reason': 'watchdog: ' + decision['reason']})
        episode['status'] = 'cancelling'
        app['cancel_sent'].append(command.command_id)
        return operations, True
    controls = state.get('resume_controls')
    if controls:
        app.update(json_copy(controls))
    if command.stage_id == 'coder':
        from .watchdog_coder import revive
        try:
            restored = revive(owner, snapshot, header, app, episode, command, outcome, decision)
        except ValueError as error:
            return operations + _retry_diagnosis(owner, snapshot, header, app, episode, str(error)), True
        if not restored:
            return operations + _retry_diagnosis(owner, snapshot, header, app, episode,
                'Coder restoration requires a current stopped task and its original development plan.'), True
        _close(state, episode, 'coder_revival_requested')
        return operations, False
    if command.stage_id == 'coder_revival_plan':
        from .watchdog_coder import restart_planner
        try:
            restored = restart_planner(snapshot, app, command, decision, episode)
        except ValueError as error:
            return operations + _retry_diagnosis(owner, snapshot, header, app, episode, str(error)), True
        if not restored:
            return operations + _retry_diagnosis(owner, snapshot, header, app, episode,
                'The failed planner no longer matches the current development group; diagnose its current binding.'), True
        restart = episode['planner_restart']
        operations += owner._schedule(snapshot, header, app, command.stage_id,
            task_id=restart['task_id'], dependencies=[], activate=False,
            payload=restart['payload'], artifact_overrides=restart['artifact_refs'],
            extra_options={key: value for key, value in command.options.items()
                           if key in {'workspace', 'goal_scope'}})
        _close(state, episode, 'planner_resumed')
        return operations, True
    payload = {**command.payload, 'watchdog_recovery': {
        'incident_id': request['incident_id'], 'instruction': decision['instruction'],
        'reason': decision['reason'], 'previous_execution_id': command.command_id}}
    retry = owner._schedule(snapshot, header, app, command.stage_id, task_id=command.task_id,
        dependencies=[], payload=payload,
        artifact_overrides={**command.artifact_refs, **owner._refs(header, app)},
        extra_options={key: value for key, value in command.options.items()
                       if key in {'workspace', 'goal_scope'}})
    operations += retry
    _close(state, episode, 'resumed')
    return operations, True


def intercept_failure(owner, snapshot, header, operations, app):
    if not enabled(header, app) or app.get('user_cancelled') or app.get('watchdog', {}).get('stop_confirmed'):
        return operations
    finish = next((op for op in operations if op['kind'] == 'finish' and op['state'] == 'failed'), None)
    stopping = app.get('stop_reason') and app.get('stop_state') == 'failed'
    if (finish is None and not stopping) or exhausted(owner, header, app):
        return operations
    old = snapshot.get('application_state') or {}
    state = app.setdefault('watchdog', {})
    state['resume_controls'] = {key: json_copy(app.get(key) or old.get(key))
                                for key in ('active_stage', 'active_group')}
    if app.get('failed_development_group'):
        state['resume_controls']['active_group'] = json_copy(app['failed_development_group'])
    task_id = old.get('active_stage')
    target = snapshot['tasks'].get(task_id)
    if target is None or state['resume_controls'].get('active_group') and successful(owner, target['attempts'][-1]):
        candidates = [(key, value) for key, value in snapshot['tasks'].items()
            if value['attempts'][-1]['state'] in TERMINAL
            and not successful(owner, value['attempts'][-1])
            and OperationInput.from_dict(value['attempts'][-1]['command']['payload']).stage_id != 'supervisor']
        if candidates:
            task_id, target = candidates[-1]
    reason = app.get('terminal_reason') or app.get('stop_reason') or 'Run failed'
    app.update(stop_reason=None, stop_state=None, terminal_reason=None)
    incident = {'incident_id': f'failure.g{snapshot.get("generation", 0)}.r{snapshot["revision"]}',
        'kind': 'run_failure', 'reason': reason, 'target_task_id': task_id,
        'target_execution_id': target['attempts'][-1]['command']['execution_id'] if target else None}
    withdrawn = {op['task_id'] for op in operations if op['kind'] == 'cancel'}
    for key in withdrawn:
        execution_id = snapshot['tasks'].get(key, {}).get('attempts', [{}])[-1].get('command', {}).get('execution_id')
        if execution_id in app.get('cancel_sent', []):
            app['cancel_sent'].remove(execution_id)
    return [op for op in operations if op['kind'] not in {'finish', 'cancel'}] + begin(owner, snapshot, header, app, incident)
