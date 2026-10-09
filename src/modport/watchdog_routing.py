"""Supervisor decisions under the Run's existing SDK authority and budgets."""
from pathlib import Path

from .contracts import OperationInput, json_copy
from .watchdog_events import enabled, notifications
from .watchdog_supervisor import validate_watchdog_decision
from .failure_supervision import recovery_enabled, next_failure, diagnosed

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


def begin(owner, snapshot, header, app, incident, *, resume_controls=None):
    state = app.setdefault('watchdog', {})
    if enabled(header, app):
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
    if target and target['attempts'][-1]['command']['execution_id'] != request['target_execution_id']:
        target = None
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
                   'created_at': owner.clock(),
                   'resume_controls': json_copy(resume_controls if resume_controls is not None else
                       {key: app.get(key) for key in ('active_stage', 'active_group')})}
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
    state.pop('resume_controls', None)


def _retry_diagnosis(owner, snapshot, header, app, episode, reason):
    episode['diagnostic'] = reason
    # A malformed model reply grants no authority. A bounded delay avoids
    # spending the remaining assignments in a tight parser retry loop.
    if owner.clock() < episode.setdefault('retry_at', owner.clock() + 60):
        return []
    previous = snapshot['tasks'][episode['supervisor_task_id']]['attempts'][-1]
    previous_command = OperationInput.from_dict(previous['command']['payload'])
    request = {**json_copy(episode['request']), 'prior_recovery': reason,
        'known_task_ids': list(snapshot['tasks']),
        'budget_context': {'deadline_epoch': owner._effective_deadline(header, app),
            'now': owner.clock(), 'agent_assignments': app.get('agent_assignments', 0),
            'max_agent_assignments': header['request']['budget'].get('max_agent_assignments')}}
    # Retain the failed attempt as the incident's cause, while exposing results
    # produced by its repairs. Replaying only the initial failure invites the
    # next supervisor to request the same already-completed work again.
    upstream = {**previous_command.upstream_results, **app.get('effective', {})}
    stages = {'code_cleanup', 'target_contract_freeze', 'target_build',
              request.get('target_input', {}).get('stage_id'), request.get('target_task_id')}
    latest = {}
    for stage in stages:
        result = app.get('effective', {}).get(stage)
        if result is not None:
            upstream[stage] = json_copy(result)
            latest[stage] = {key: result.get(key) for key in
                            ('command_id', 'stage_id', 'status', 'error_code', 'detail')}
    request['current_results'] = latest
    previous_outputs = (episode.get('supervisor_result') or {}).get('outputs', {})
    report = previous_outputs.get('watchdog_supervisor_raw_report')
    if report:
        request['previous_supervisor_report'] = {'path': report}
    payload = {'watchdog_incident': request}
    for key in ('diagnostic_repair_targets', 'diagnostic_repair_refs'):
        if key in previous_command.payload:
            payload[key] = json_copy(previous_command.payload[key])
    operations = owner._schedule(snapshot, header, app, 'supervisor',
        task_id=episode['supervisor_task_id'], dependencies=[], activate=False,
        payload=payload, upstream_overrides=upstream,
        extra_options={'workspace': 'workspaces/watchdog/' + episode['supervisor_task_id']})
    if any(op['kind'] in {'add_task', 'new_attempt'} for op in operations):
        episode['request'] = request
        episode.pop('retry_at', None)
        episode.pop('decision', None)
        episode['status'] = 'pending'
    return operations


def decide(owner, snapshot, header, app, sdk):
    """Return (operations, owns_business_routing) while diagnosis is pending."""
    if not recovery_enabled(header, app) or app.get('user_cancelled') or app.get('stop_reason'):
        return [], False
    state = app.setdefault('watchdog', {'seen': [], 'episodes': {}})
    if not state.get('active'):
        incident = next_failure(owner, snapshot, header, app)
        if incident is not None:
            if exhausted(owner, header, app):
                return [], False
            operations = begin(owner, snapshot, header, app, incident)
            return operations, bool(state.get('active') or app.get('stop_reason') or operations)
        if not enabled(header, app):
            return [], False
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
                if kind == 'terminal' and diagnosed(state, execution_id):
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
    queued = episode.get('resume_pending')
    if queued is not None:
        task = snapshot['tasks'].get(queued['task_id'])
        attempt = task['attempts'][-1] if task else None
        if attempt is None or attempt['command']['execution_id'] != queued['execution_id']:
            _close(state, episode, 'superseded')
            return [], False
        if attempt['state'] != 'planned':
            _close(state, episode, 'resumed')
            return [], False
        if queued.get('caller_id'):
            caller = next((row for item in snapshot['tasks'].values() for row in item['attempts']
                           if row['command']['execution_id'] == queued['caller_id']), None)
            record = next((row for row in app.get('review_rework', {}).get('requests', {}).values()
                           if row.get('task_id') == queued['task_id']), None)
            if (caller is None or caller['state'] in TERMINAL or record is None
                    or owner._tool_cancelled(Path(header['run_dir']), record)):
                if record is not None:
                    record['recovery_caller_closed'] = True
                _close(state, episode, 'caller_closed')
                app.get('memory_waits', {}).pop('failure_resume', None)
                if queued['execution_id'] not in app['cancel_sent']:
                    app['cancel_sent'].append(queued['execution_id'])
                    return [{'kind': 'cancel', 'task_id': queued['task_id'],
                             'reason': 'reviewer tool custody closed during recovery queue'}], True
                return [], False
        from .failure_resume import capacity_available
        if not capacity_available(owner, snapshot, header, app, queued['stage'],
                execution_id=queued['execution_id'], caller_id=queued.get('caller_id')):
            return [], True
        _close(state, episode, 'resumed')
        app.get('memory_waits', {}).pop('failure_resume', None)
        return [{'kind': 'dispatch', 'task_id': queued['task_id']}], True
    operations = owner._review_rework_decision(snapshot, header, app)
    if app.get('stop_reason'):
        return operations, False
    supervisor = snapshot['tasks'].get(episode['supervisor_task_id'])
    if not supervisor or supervisor['attempts'][-1]['state'] not in TERMINAL:
        if supervisor and supervisor['attempts'][-1]['state'] == 'recovery_required':
            from .interrupted_supervision import request_cancellation
            operations += request_cancellation(owner, snapshot, header, app, sdk, episode)
        return operations, True
    if 'decision' not in episode:
        command, result = owner._flowthrough_outcome(supervisor['attempts'][-1])
        from .diagnostic_repair_routing import register
        register(app, command, result)
        episode['supervisor_result'] = result.to_dict()
        try:
            if result.status != 'completed':
                raise ValueError(result.detail or result.error_code or 'supervisor failed')
            if result.outputs.get('watchdog_decision') is None:
                diagnostics = result.outputs.get('watchdog_supervisor_diagnostics')
                if isinstance(diagnostics, list) and diagnostics:
                    raise ValueError('; '.join(str(item) for item in diagnostics))
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
                       or target['state'] in TERMINAL and not successful(owner, target)
                       and request['kind'] != 'task_failure'):
            return operations + _retry_diagnosis(owner, snapshot, header, app, episode,
                'Target is settled unsuccessfully; supply a concrete recovery instruction or real prerequisite.'), True
        _close(state, episode, 'continued')
        return operations, False
    if not target:
        if failed_route:
            return operations + _retry_diagnosis(owner, snapshot, header, app, episode,
                'The host failure has no bound execution to resume. Diagnose the missing host '
                'prerequisite or request an explicit prerequisite wait; do not treat it as recovered.'), True
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
    controls = episode.get('resume_controls')
    if controls is not None:
        # The branch may have consumed supervisor-requested repairs meanwhile.
        # Restore its route, without replacing fresh group results with the
        # pre-diagnosis copy when the same group still owns the task.
        group = app.get('active_group')
        restored = json_copy(controls)
        prior_group = restored.get('active_group')
        if (isinstance(group, dict) and isinstance(prior_group, dict)
                and group.get('kind') == prior_group.get('kind')
                and group.get('generation') == prior_group.get('generation')):
            restored['active_group'] = group
        app.update(restored)
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
        retry = owner._schedule(snapshot, header, app, command.stage_id,
            task_id=restart['task_id'], dependencies=[], activate=False,
            payload=restart['payload'], artifact_overrides=restart['artifact_refs'],
            extra_options={key: value for key, value in command.options.items()
                           if key in {'workspace', 'goal_scope'}})
        operations += retry
        if any(op['kind'] in {'add_task', 'new_attempt'} for op in retry):
            _close(state, episode, 'planner_resumed')
        return operations, True
    from .failure_resume import ClosedReworkRequest, resume_assignment
    try:
        retry = resume_assignment(owner, snapshot, header, app, episode, command, decision)
    except ClosedReworkRequest as error:
        episode['diagnostic'] = str(error)
        _close(state, episode, 'caller_closed')
        return operations, False
    except ValueError as error:
        return operations + _retry_diagnosis(owner, snapshot, header, app, episode, str(error)), True
    operations += retry
    if (any(op['kind'] in {'add_task', 'new_attempt'} for op in retry)
            and not episode.get('resume_pending')):
        _close(state, episode, 'resumed')
    return operations, True


def intercept_failure(owner, snapshot, header, operations, app):
    if not recovery_enabled(header, app) or app.get('user_cancelled') or app.get('watchdog', {}).get('stop_confirmed'):
        return operations
    finish = next((op for op in operations if op['kind'] == 'finish' and op['state'] == 'failed'), None)
    stopping = app.get('stop_reason') and app.get('stop_state') == 'failed'
    if (finish is None and not stopping) or exhausted(owner, header, app):
        return operations
    old = snapshot.get('application_state') or {}
    state = app.setdefault('watchdog', {})
    controls = {key: json_copy(app[key] if app.get(key) is not None else old.get(key))
                for key in ('active_stage', 'active_group')}
    if app.get('failed_development_group'):
        controls['active_group'] = json_copy(app['failed_development_group'])
    task_id = old.get('active_stage')
    target = snapshot['tasks'].get(task_id)
    if (target is not None and target['attempts'][-1].get('generation', snapshot.get('generation', 0))
            != snapshot.get('generation', 0)):
        target = None
        task_id = None
    if target is None or controls.get('active_group') and successful(owner, target['attempts'][-1]):
        candidates = [(key, value) for key, value in snapshot['tasks'].items()
            if value['attempts'][-1]['state'] in TERMINAL
            and value['attempts'][-1].get('generation', snapshot.get('generation', 0))
                == snapshot.get('generation', 0)
            and not successful(owner, value['attempts'][-1])
            and OperationInput.from_dict(value['attempts'][-1]['command']['payload']).stage_id != 'supervisor']
        if candidates:
            task_id, target = candidates[-1]
    reason = app.get('terminal_reason') or app.get('stop_reason') or 'Run failed'
    app.update(stop_reason=None, stop_state=None, terminal_reason=None)
    incident = {'incident_id': f'finish.g{snapshot.get("generation", 0)}.{len(state.get("episodes", {})) + 1}',
        'kind': 'run_failure', 'reason': reason, 'target_task_id': task_id,
        'target_execution_id': target['attempts'][-1]['command']['execution_id'] if target else None,
        'business_failure': {'acceptance_status': app.get('acceptance_status', 'unverified'),
            'final_cleanup': json_copy(app.get('final_cleanup', {}))}}
    withdrawn = {op['task_id'] for op in operations
                 if op['kind'] == 'cancel' and op.get('reason') == reason}
    for key in withdrawn:
        execution_id = snapshot['tasks'].get(key, {}).get('attempts', [{}])[-1].get('command', {}).get('execution_id')
        if execution_id in app.get('cancel_sent', []):
            app['cancel_sent'].remove(execution_id)
    return [op for op in operations if op['kind'] != 'finish'
            and not (op['kind'] == 'cancel' and op['task_id'] in withdrawn)] + begin(
                owner, snapshot, header, app, incident, resume_controls=controls)
