"""Translate an explicit watchdog repair into the existing coder revival route."""
from collections.abc import Mapping
from pathlib import Path

from . import coder_revival
from .contracts import OperationResult, json_copy
from .evidence import atomic_json, read_json


def _release_planner_block(group, names):
    revival = coder_revival.state(group)
    if revival.get('terminal_error') != 'coder_revival_planner_unavailable':
        return
    causal = set(names) | coder_revival._descendants(group, set(names))
    remaining = set(revival.get('terminal_blocked', ())) - causal
    revival['terminal_blocked'] = sorted(remaining)
    if not remaining:
        revival.pop('terminal_error', None)


def _supervisor(episode, decision, logical_id):
    supervisor = OperationResult.from_dict(episode.get('supervisor_result', {}))
    if (supervisor.status != 'completed' or supervisor.stage_id != 'supervisor'
            or supervisor.run_id != logical_id
            or episode.get('supervisor_execution_id', supervisor.command_id) != supervisor.command_id
            or supervisor.outputs.get('watchdog_decision') != decision):
        raise ValueError('watchdog coder revival requires the exact completed supervisor result')
    return supervisor


def restart_planner(snapshot, app, command, decision, episode) -> bool:
    """Prepare a fresh planner task while retaining its failed request verbatim.

    The coordinator schedules ``episode['planner_restart']`` through its usual
    budgeted producer. A new task lets the ordinary pending-request consumer
    archive the new response without overwriting the original failure.
    """
    group = app.get('active_group')
    if (command.stage_id != coder_revival.STAGE or not isinstance(group, Mapping)
            or group.get('kind') != 'development'):
        return False
    if decision.get('action') != 'repair_resume' or not isinstance(decision.get('instruction'), str) or not decision['instruction'].strip():
        raise ValueError('planner restoration requires an explicit repair_resume instruction')
    incident_id = episode.get('incident_id') or episode.get('request', {}).get('incident_id')
    if not incident_id or decision.get('incident_id') != incident_id:
        raise ValueError('planner restoration incident differs from the supervisor decision')
    attempts = snapshot.get('tasks', {}).get(command.task_id, {}).get('attempts', [])
    if (not attempts or attempts[-1]['state'] not in coder_revival.TERMINAL
            or attempts[-1]['command']['execution_id'] != command.command_id):
        raise ValueError('planner restoration attempt is active or superseded')
    supervisor = _supervisor(episode, decision, command.run_id)
    candidate = json_copy(group)
    revival = coder_revival.state(candidate)
    source = revival['requests'].get(command.task_id)
    if (source is None or source.get('status') not in {'unavailable', 'dispatched'}
            or source.get('request') != command.payload.get('revival_request')
            or source['request'].get('generation') != candidate['generation']
            or source['request'].get('base_commit') != candidate['base']
            or source['request'].get('tasks') != candidate['tasks']
            or command.artifact_refs.get('development_plan')
            != candidate.get('artifact_refs', {}).get('development_plan')
            or source.get('status') == 'unavailable'
            and source.get('result', {}).get('command_id') != command.command_id):
        raise ValueError('planner restoration requires its exact original development request')
    # SDK task IDs use the ModPort identifier alphabet. The exact execution
    # remains in the record and reference; this name uses its host task/attempt.
    task_id = 'revival.watchdog.' + supervisor.task_id + '.a' + supervisor.command_id.rsplit(':', 1)[-1]
    recovery = {'incident_id': incident_id, 'instruction': decision['instruction'],
                'reason': decision['reason'], 'previous_execution_id': command.command_id}
    if task_id in revival['requests']:
        if (revival['pending'] == task_id
                and episode.get('planner_restart', {}).get('task_id') == task_id
                and revival['requests'][task_id]['request'].get('watchdog_recovery') == recovery):
            return True
        raise ValueError('planner restoration identity already has a different request')
    if revival.get('pending') not in {None, command.task_id}:
        raise ValueError('a different planner request is already pending')
    _release_planner_block(candidate, source['request']['required_tasks'])
    if revival.get('terminal_error'):
        return False
    request = json_copy(source['request'])
    request['request_id'] = task_id
    request['prior_decisions'].append({'request_id': command.task_id, 'status': source['status'],
        'feedback': source.get('feedback'), 'request_ref': source['request_ref']})
    request['watchdog_recovery'] = recovery
    request['previous_planner_attempt'] = {key: json_copy(attempts[-1].get(key))
                                         for key in ('state', 'error', 'result')}
    root = Path(command.run_dir).resolve()
    path = root / 'artifacts/coder-revival' / snapshot['run_id'] / task_id / 'request.json'
    if path.resolve() != path.absolute() or not path.resolve().is_relative_to(root):
        raise ValueError('unsafe restored planner request path')
    if path.exists():
        if read_json(path) != request:
            raise ValueError('persisted restored planner request differs from its identity')
    else:
        atomic_json(path, request)
    reference = {'path': path.relative_to(root).as_posix(), 'media_type': 'application/json',
                 'metadata': {'execution_id': supervisor.command_id, 'run_id': command.run_id}}
    revival['requests'][task_id] = {'status': 'dispatched', 'request': request,
        'request_ref': reference, 'watchdog_source_task_id': command.task_id}
    revival['pending'] = task_id
    episode['planner_restart'] = {'task_id': task_id,
        'payload': {**command.payload, 'revival_request': request,
                    'watchdog_recovery': request['watchdog_recovery']},
        'artifact_refs': {**command.artifact_refs, 'coder_revival_request': reference}}
    group.clear()
    group.update(candidate)
    return True


def revive(owner, snapshot, header, app, episode, target_command, target_outcome, decision) -> bool:
    """Queue a stopped coder through dependency-aware, process-checked revival.

    The caller owns supervisor decision validation and exact cancellation
    settlement. This bridge retains the group and feeds its normal consumer;
    it does not dispatch work, grant budget or rewrite an existing attempt.
    """
    group = app.get('active_group')
    if (target_command.stage_id != 'coder' or not coder_revival.enabled(header)
            or not isinstance(group, Mapping) or group.get('kind') != 'development'):
        return False
    if decision.get('action') != 'repair_resume' or not isinstance(decision.get('instruction'), str) or not decision['instruction'].strip():
        raise ValueError('watchdog coder revival requires an explicit repair_resume instruction')
    incident_id = episode.get('incident_id') or episode.get('request', {}).get('incident_id')
    if not incident_id or decision.get('incident_id') != incident_id:
        raise ValueError('watchdog coder revival incident differs from the supervisor decision')
    target_outcome.validate_for(target_command)
    logical_id = header.get('logical_run_id', header['run_id'])
    if target_command.run_id != logical_id:
        raise ValueError('watchdog coder belongs to a different logical Run')
    task = target_command.payload.get('development_task', {})
    name = task.get('id')
    current_task = next((row for row in group['tasks'] if row['id'] == name), None)
    if current_task is None or current_task != task:
        raise ValueError('watchdog coder task differs from the active development plan')
    task_id = coder_revival.coder_id(group, name)
    if target_command.task_id != task_id:
        raise ValueError('watchdog coder generation differs from the active development group')
    plan = group.get('artifact_refs', {}).get('development_plan')
    if target_command.artifact_refs.get('development_plan') != plan:
        raise ValueError('watchdog coder plan reference differs from the active development group')
    attempts = snapshot.get('tasks', {}).get(task_id, {}).get('attempts', [])
    if (not attempts or attempts[-1]['state'] not in coder_revival.TERMINAL
            or attempts[-1]['command']['execution_id'] != target_command.command_id):
        raise ValueError('watchdog coder attempt is active or superseded')
    supervisor = _supervisor(episode, decision, logical_id)
    request_id = 'watchdog.revival.' + supervisor.command_id
    candidate = json_copy(group)
    revival = coder_revival.state(candidate)
    existing = revival['requests'].get(request_id)
    if existing is not None:
        if (existing.get('status') == 'applied'
                and existing.get('result') == supervisor.to_dict()
                and existing['request']['results'].get(name, {}).get('command_id') == target_command.command_id):
            return True
        raise ValueError('watchdog coder revival request already has different content')
    prior = coder_revival._latest_results(candidate).get(name)
    if prior is not None and prior.get('command_id') != target_command.command_id:
        raise ValueError('watchdog coder result is superseded in the development group')
    candidate['results'][task_id] = target_outcome.to_dict()
    current = json_copy(coder_revival._latest_results(candidate))
    request = {
        'request_id': request_id, 'generation': candidate['generation'],
        'base_commit': candidate['base'], 'trigger_execution_ids': [target_command.command_id],
        'requested_tasks': [name], 'required_tasks': [name],
        'tasks': json_copy(candidate['tasks']), 'results': current,
        'attempts': {name: len(attempts)},
        'budget_context': {'deadline_epoch': owner._effective_deadline(header, app),
            'agent_assignments_used': app['agent_assignments'],
            'agent_assignments_limit': header['request']['budget']['max_agent_assignments']},
        'prior_decisions': [{'request_id': key, 'status': record['status'],
                            'decision': record.get('decision'), 'feedback': record.get('feedback'),
                            'request_ref': record['request_ref']}
                           for key, record in revival['requests'].items()],
        'execution_evidence': {name: {key: json_copy(attempts[-1].get(key))
                                    for key in ('state', 'error', 'result')}},
        'watchdog_incident_id': incident_id,
    }
    root = Path(header['run_dir']).resolve()
    path = root / 'artifacts/coder-revival' / header['run_id'] / request_id / 'request.json'
    if path.resolve() != path.absolute() or not path.resolve().is_relative_to(root):
        raise ValueError('unsafe watchdog coder revival request path')
    reference = {'path': path.relative_to(root).as_posix(), 'media_type': 'application/json',
                 'metadata': {'execution_id': supervisor.command_id, 'run_id': logical_id}}
    record = {'status': 'prepared', 'request': request, 'request_ref': reference,
              'result': supervisor.to_dict()}
    revival_decision = {'reason': decision['reason'], 'decisions': [{
        'task_id': name, 'action': 'resume', 'instruction': decision['instruction'],
        'wait_for': [], 'reuse_partial': True}]}
    # Keep the existing validation of transitive dependencies, active attempts
    # and actual producer processes. It mutates only this private group copy.
    coder_revival._apply_decision(snapshot, header, candidate, record, revival_decision)
    _release_planner_block(candidate, {name})
    revival['requests'][request_id] = record
    if path.exists():
        if read_json(path) != request:
            raise ValueError('persisted watchdog coder revival request differs from its identity')
    else:
        atomic_json(path, request)
    # Mark the retained failed attempt consumed so the group consumer cannot
    # reinsert it and undo the ready hold on its next policy tick.
    if target_command.command_id not in app['processed']:
        owner._flowthrough_record(app, target_command, target_outcome, canonical=False)
    group.clear()
    group.update(candidate)
    return True
