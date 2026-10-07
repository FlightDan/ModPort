"""Persistent planner requests for stopped coders and changed dependencies.

The host produces requests; the SDK executes the planner and the next coder
attempts. Decisions and dispatches are committed by the normal policy tick.
No model verdict resets a deadline, grants budget, or rewrites an old attempt.
"""
from pathlib import Path

from .contracts import json_copy
from .evidence import atomic_json, digest, seal_ref
from .repair_evidence import snapshot_repair_evidence


TERMINAL = frozenset({'succeeded', 'failed', 'cancelled', 'timed_out', 'dead'})
STAGE = 'coder_revival_plan'


def enabled(header):
    definition = header.get('definition', {})
    return (definition.get('workflow_version', 0) >= 25
            and definition.get('revival_policy', {}).get('mode') == 'planner_requests')


def coder_id(group, name):
    return f"coder.g{group['generation']}.{name}"


def successful(result):
    if not isinstance(result, dict) or result.get('status') != 'completed':
        return False
    native = result.get('outputs', {}).get('native_goal', {})
    return native.get('status') not in {'failed', 'blocked', 'cancelled', 'timed_out'}


def state(group):
    return group.setdefault('revival', {'holds': {}, 'requests': {}, 'sequence': 0,
                                        'pending': None, 'seen': {}})


def results(group):
    return {task['id']: group['results'][coder_id(group, task['id'])]
            for task in group['tasks'] if coder_id(group, task['id']) in group['results']}


def _execution_ids(group):
    return {name: result.get('command_id') for name, result in _latest_results(group).items()}


def _latest_results(group):
    previous = {name: hold['previous_result'] for name, hold in state(group)['holds'].items()
                if isinstance(hold.get('previous_result'), dict)}
    return {**previous, **results(group)}


def _snapshot_planner_refs(root, value):
    """Snapshot host artifact interfaces, preserving workspace report metadata.

    A result may also contain {path, sha256} summaries relative to its coder
    workspace. Those are not Run artifact references and must not be opened
    relative to the Run root.
    """
    if isinstance(value, list):
        return [_snapshot_planner_refs(root, item) for item in value]
    if not isinstance(value, dict):
        return value
    return {key: (snapshot_repair_evidence(root, child)
                  if key in {'artifact_refs', 'request_ref'}
                  else _snapshot_planner_refs(root, child))
            for key, child in value.items()}


def _active(snapshot, group, name):
    task = snapshot['tasks'].get(coder_id(group, name))
    return bool(task and task['attempts'][-1]['state'] not in TERMINAL)


def _producer_alive(header, result):
    # A terminal SDK record alone cannot authorize reuse of a live producer.
    from .goal_runtime import _previous_process_alive
    from .evidence import read_json
    command_id = result.get('command_id')
    if not command_id:
        return False
    from hashlib import sha256
    path = (Path(header['run_dir']) / 'artifacts' / 'native-goals'
            / sha256(command_id.encode()).hexdigest()[:24] / 'state.json')
    if not path.exists():
        return False
    if path.is_symlink() or path.resolve() != path.absolute():
        raise ValueError('unsafe stopped-coder state path')
    metadata = read_json(path)
    if metadata.get('command_id') != command_id:
        raise ValueError('stopped-coder identity differs from SDK execution')
    return _previous_process_alive(metadata)


def record_result(group, name, result):
    revival = state(group)
    if revival['seen'].get(name) == result.get('command_id'):
        return
    revival['seen'][name] = result.get('command_id')
    hold = revival['holds'].get(name)
    # A descendant already running on old inputs may finish while B is being
    # repaired. Retain that result, but it cannot clear dependency invalidation.
    if hold and hold['status'] == 'waiting':
        return
    if successful(result):
        revival['holds'].pop(name, None)
    else:
        revival['holds'][name] = {'status': 'needs_plan', 'wait_for': [],
                                  'source_execution_id': result.get('command_id')}


def completed(group):
    holds = state(group)['holds']
    return {name for name, result in results(group).items()
            if successful(result) and name not in holds}


def _descendants(group, names):
    affected = set(names)
    while True:
        following = affected | {task['id'] for task in group['tasks']
                                if set(task.get('dependencies', ())) & affected}
        if following == affected:
            return affected - set(names)
        affected = following


def _validate_waits(group, decisions, snapshot):
    graph = {task['id']: set(task.get('dependencies', ())) for task in group['tasks']}
    resumed = {row['task_id'] for row in decisions if row['action'] == 'resume'}
    actions = {row['task_id']: row['action'] for row in decisions}
    stopped = {name for name, hold in state(group)['holds'].items()
               if hold['status'] == 'stopped' and name not in actions}
    stopped.update(name for name, action in actions.items() if action == 'stop')
    waits = {row['task_id']: row['wait_for'] for row in decisions if row['action'] == 'wait'}
    for name, hold in state(group)['holds'].items():
        if hold['status'] == 'waiting' and name not in actions:
            waits[name] = hold['wait_for']
    blocked = stopped | _descendants(group, stopped)
    while True:
        expanded = blocked | {name for name, dependencies in waits.items()
                              if set(dependencies) & blocked}
        if expanded == blocked:
            break
        blocked = expanded
    current = _latest_results(group)
    for name, dependencies in waits.items():
        graph[name].update(dependencies)
        for dependency in dependencies:
            if dependency in stopped:
                raise ValueError(f'{name} waits for stopped {dependency}; select its revival or stop the waiter')
            if dependency in blocked:
                raise ValueError(f'{name} waits for {dependency} blocked by a stopped prerequisite')
            if dependency in current and dependency not in resumed and not _active(snapshot, group, dependency):
                raise ValueError(f'{name} waits for already settled {dependency}; select its revival or use its result')
    visited, visiting = set(), set()
    def visit(name):
        if name in visiting:
            raise ValueError('planner introduced a cyclic revival dependency')
        if name in visited:
            return
        visiting.add(name)
        for dependency in graph[name]:
            visit(dependency)
        visiting.remove(name)
        visited.add(name)
    for name in graph:
        visit(name)


def _apply_decision(snapshot, header, group, record, decision):
    from .revival_planning import validate_decision
    decision = validate_decision(decision, record['request'])
    rows = decision['decisions']
    _validate_waits(group, rows, snapshot)
    current_ids = _execution_ids(group)
    frozen_ids = {name: result.get('command_id')
                  for name, result in record['request']['results'].items()}
    tasks = {task['id']: task for task in group['tasks']}
    for row in rows:
        name = row['task_id']
        relevant = {name, *tasks[name].get('dependencies', ())}
        # Coder workspaces consume transitive dependency patches as well.
        while True:
            expanded = relevant | {parent for key in relevant
                                   for parent in tasks[key].get('dependencies', ())}
            if expanded == relevant:
                break
            relevant = expanded
        if _active(snapshot, group, name) or any(current_ids.get(key) != frozen_ids.get(key) for key in relevant):
            raise ValueError('planner decision is stale relative to coder or dependency execution')
        consumed = relevant if row['action'] == 'resume' else {name}
        for key in consumed:
            if _producer_alive(header, _latest_results(group).get(key, {})):
                raise ValueError(f'coder producer is still alive: {key}')
    revival = state(group)
    # A resumed predecessor invalidates the eligibility of old descendant
    # results, without cancelling independent work or deleting evidence.
    resumed = {row['task_id'] for row in rows if row['action'] == 'resume'}
    for name in _descendants(group, resumed) - resumed:
        if name in current_ids or _active(snapshot, group, name):
            wait_for = sorted(set(tasks[name].get('dependencies', ())) & (resumed | _descendants(group, resumed)))
            revival['holds'][name] = {'status': 'waiting', 'wait_for': wait_for,
                'observed': {key: current_ids.get(key) for key in wait_for},
                'reason': 'dependency_revived', 'request_id': record['request']['request_id']}
    for row in rows:
        name = row['task_id']
        hold = {'status': {'resume': 'ready', 'wait': 'waiting', 'stop': 'stopped'}[row['action']],
                'wait_for': row.get('wait_for', []), 'instruction': row['instruction'],
                'request_id': record['request']['request_id'],
                'observed': {key: current_ids.get(key) for key in row.get('wait_for', [])}}
        if row['action'] == 'resume':
            hold['dependency_executions'] = {key: current_ids.get(key)
                                             for key in tasks[name].get('dependencies', ())}
            hold['reuse_partial'] = row.get('reuse_partial', True)
            hold['previous_result'] = json_copy(_latest_results(group).get(name))
            changed_dependencies = set(tasks[name].get('dependencies', ())) & resumed
            if changed_dependencies:
                hold.update(status='waiting', wait_for=sorted(changed_dependencies),
                            observed={key: current_ids.get(key) for key in changed_dependencies})
            if name in group['scheduled']:
                group['scheduled'].remove(name)
            # Old result remains in the SDK, request and history; not in the
            # set selected for the next integration.
            group['results'].pop(coder_id(group, name), None)
        revival['holds'][name] = hold
    record.update(status='applied', decision=json_copy(decision))


def _diagnostic_repair_targets(snapshot, header, group, request):
    """Bind optional repair copies to stopped coder executions and their plan."""
    if (header.get('definition', {}).get('workflow_version', 0) < 40
            or group.get('goal_scope') == 'contract'):
        return []
    plan_ref = group.get('artifact_refs', {}).get('development_plan')
    if not isinstance(plan_ref, dict):
        return []
    root = Path(header['run_dir']).resolve()
    targets = []
    for name in request['requested_tasks']:
        result = request['results'].get(name, {})
        source_id = result.get('command_id')
        if not source_id or _active(snapshot, group, name) or _producer_alive(header, result):
            continue
        attempts = snapshot['tasks'].get(coder_id(group, name), {}).get('attempts', [])
        for attempt in reversed(attempts):
            command = attempt.get('command', {}).get('payload', {})
            if not isinstance(command, dict) or command.get('command_id') != source_id:
                continue
            relative = command.get('options', {}).get('workspace')
            if (attempt.get('state') not in TERMINAL or command.get('stage_id') != 'coder'
                    or command.get('run_id') != header.get('logical_run_id', header['run_id'])
                    or command.get('task_id') != coder_id(group, name)
                    or command.get('payload', {}).get('development_task', {}).get('id') != name
                    or command.get('artifact_refs', {}).get('development_plan') != plan_ref
                    or not isinstance(relative, str)
                    or not relative.startswith('workspaces/development/')
                    or Path(relative).is_absolute() or '..' in Path(relative).parts):
                break
            source = root / relative
            if (source.is_dir() and not source.is_symlink()
                    and source.resolve() == source.absolute()):
                targets.append({'task_id': name, 'plan_ref': json_copy(plan_ref),
                                'source_workspace': relative,
                                'source_execution_id': source_id})
            break
    return targets


def advance(host, snapshot, header, app):
    """Collect one planner response and persist at most one new SDK request."""
    group = app['active_group']
    revival = state(group)
    operations = []
    if revival.get('terminal_error'):
        return operations
    pending = revival['pending']
    if pending:
        task = snapshot['tasks'].get(pending)
        if task is None or task['attempts'][-1]['state'] not in TERMINAL:
            return operations
        attempt = task['attempts'][-1]
        command, outcome = host._flowthrough_outcome(attempt)
        host._flowthrough_record(app, command, outcome, canonical=False)
        record = revival['requests'][pending]
        revival['pending'] = None
        record['result'] = outcome.to_dict()
        raw_result = attempt.get('result') or {}
        record['execution'] = {'state': attempt['state'],
                               'error': json_copy(raw_result.get('error'))}
        if outcome.status != 'completed':
            if outcome.error_code in {'revival_decision_invalid', 'revival_dialogue_incomplete'}:
                # A real planner turn returned but its report was incomplete.
                # Deliver the diagnostic to a new planner under the same budget.
                record.update(status='rejected',
                              feedback=outcome.detail or outcome.error_code)
            else:
                # The planner itself never supplied a usable turn. Re-dispatching
                # against the same coder result cannot repair a missing handler,
                # broken provider or executor failure.
                record.update(status='unavailable', feedback=outcome.detail or
                              outcome.error_code or 'revival planner did not complete')
                revival['terminal_error'] = 'coder_revival_planner_unavailable'
                required = set(record['request']['required_tasks'])
                revival['terminal_blocked'] = sorted(required | _descendants(group, required))
                return operations
        else:
            try:
                _apply_decision(snapshot, header, group, record, outcome.outputs.get('revival_decision'))
                from .diagnostic_repair_routing import register
                register(app, command, outcome)
            except (OSError, ValueError, TypeError, KeyError) as exc:
                record.update(status='rejected', feedback=str(exc))
                # Feedback is delivered to the next planner under the same budget;
                # a malformed or stale reply never dispatches a coder.

    current = _latest_results(group)
    ids = _execution_ids(group)
    for name, hold in revival['holds'].items():
        if hold['status'] == 'ready' and any(ids.get(key) != execution
                for key, execution in hold.get('dependency_executions', {}).items()):
            hold['status'] = 'needs_plan'
        if hold['status'] == 'waiting' and all(
                key in current and not _active(snapshot, group, key)
                and ids[key] != hold.get('observed', {}).get(key)
                for key in hold['wait_for']):
            hold['status'] = 'needs_plan'

    required = [name for name, hold in revival['holds'].items()
                if hold['status'] == 'needs_plan' and not _active(snapshot, group, name)
                and not _producer_alive(header, current.get(name, {}))]
    if not required or revival['pending']:
        return operations
    # _schedule charges assignments and keeps the original deadline. The
    # normal SDK planned queue performs stage-specific resource admission.
    revival['sequence'] += 1
    request_digest = digest([snapshot.get('revision'), ids, required])[:12]
    task_id = f"revival.g{group['generation']}.{revival['sequence']}.{request_digest}"
    requested = [task['id'] for task in group['tasks'] if not _active(snapshot, group, task['id'])]
    trigger_ids = sorted(set(filter(None, [ids.get(name) for name in required] + [
        ids.get(key) for name in required for key in revival['holds'][name].get('wait_for', [])])))
    attempts = {name: len(snapshot['tasks'].get(coder_id(group, name), {}).get('attempts', []))
                for name in requested}
    request = {'request_id': task_id, 'generation': group['generation'], 'base_commit': group['base'],
               'trigger_execution_ids': trigger_ids, 'requested_tasks': requested,
               'required_tasks': required, 'tasks': json_copy(group['tasks']),
               'results': json_copy(current), 'attempts': attempts,
               'budget_context': {'deadline_epoch': host._effective_deadline(header, app),
                                  'agent_assignments_used': app['agent_assignments'],
                                  'agent_assignments_limit': header['request']['budget']['max_agent_assignments']},
               'prior_decisions': [{'request_id': key, 'status': value['status'],
                                    'decision': value.get('decision'), 'feedback': value.get('feedback'),
                                    'request_ref': value['request_ref']}
                                   for key, value in revival['requests'].items()],
               'execution_evidence': {name: {
                   key: snapshot['tasks'][coder_id(group, name)]['attempts'][-1].get(key)
                   for key in ('state', 'error', 'result')}
                   for name in requested if coder_id(group, name) in snapshot['tasks']}}
    timeout_proofs = app.get('settled_timeout_diagnoses', {})
    if isinstance(timeout_proofs, dict):
        for name, evidence in request['execution_evidence'].items():
            proof = timeout_proofs.get(coder_id(group, name))
            if (isinstance(proof, dict) and proof.get('action') == 'diagnose'
                    and proof.get('execution_id') == ids.get(name)
                    and isinstance(proof.get('stage_receipt_ref'), dict)):
                evidence['settled_timeout_diagnosis'] = {
                    'proof': {key: value for key, value in proof.items()
                              if key != 'stage_receipt_ref'},
                    'artifact_refs': {'stage_receipt': proof['stage_receipt_ref']},
                }
    # Model tools can read artifacts/, but execution logs may live in logs/.
    # Freeze referenced bytes so the planner sees raw failures through the
    # same bounded reader, without granting access to the Run's other files.
    request = _snapshot_planner_refs(header['run_dir'], request)
    path = (Path(header['run_dir']) / 'artifacts' / 'coder-revival'
            / header['run_id'] / task_id / 'request.json')
    if path.exists():
        from .evidence import read_json
        if read_json(path) != request:
            raise ValueError('persisted revival request differs from its dispatch identity')
    else:
        atomic_json(path, request)
    ref = seal_ref(Path(header['run_dir']), {'path': str(path.relative_to(header['run_dir'])),
                                     'media_type': 'application/json'}, execution_id=task_id)
    refs = {**snapshot_repair_evidence(header['run_dir'], group.get('artifact_refs', {})),
            'coder_revival_request': ref}
    for name, result in request['results'].items():
        for alias, artifact in result.get('outputs', {}).get('artifact_refs', {}).items():
            refs[f'revival:{name}:{alias}'] = artifact
    for name, evidence in request['execution_evidence'].items():
        settled = evidence.get('settled_timeout_diagnosis', {})
        for alias, artifact in settled.get('artifact_refs', {}).items():
            refs[f'revival:{name}:{alias}'] = artifact
    dependencies = [coder_id(group, name) for name in requested
                    if coder_id(group, name) in snapshot['tasks']]
    payload = {'revival_request': request, 'goal_scope': group.get('goal_scope')}
    repair_targets = _diagnostic_repair_targets(snapshot, header, group, request)
    if repair_targets:
        payload['diagnostic_repair_targets'] = repair_targets
    operations = host._schedule(snapshot, header, app, STAGE, task_id=task_id,
        activate=False, dependencies=dependencies, causation_id=trigger_ids[-1] if trigger_ids else None,
        artifact_overrides=refs, payload=payload)
    if any(row['kind'] == 'dispatch' for row in operations):
        revival['pending'] = task_id
        revival['requests'][task_id] = {'status': 'dispatched', 'request': request, 'request_ref': ref}
    return operations


def coder_payload(snapshot, header, group, name):
    hold = state(group)['holds'].get(name)
    if not hold or hold['status'] != 'ready':
        return {}
    previous = hold.get('previous_result') or {}
    attempt = len(snapshot['tasks'].get(coder_id(group, name), {}).get('attempts', [])) + 1
    record = state(group)['requests'][hold['request_id']]
    payload = {'development_workspace_epoch': digest([header['run_id'], coder_id(group, name), attempt])[:16],
            'coder_revival': {'request_id': hold['request_id'], 'instruction': hold['instruction'],
                             'planner_execution_id': record['result']['command_id'],
                             'previous_execution_id': previous.get('command_id'),
                             'request_ref': record['request_ref']},
            'recovered_partial_patch': previous.get('outputs', {}).get('artifact_refs', {}).get('coder_patch')
                if hold.get('reuse_partial', True) else None}
    if header.get('definition', {}).get('workflow_version', 0) >= 40:
        plan_ref = group.get('artifact_refs', {}).get('development_plan')
        refs = record['result'].get('outputs', {}).get('diagnostic_repairs', [])
        if not isinstance(refs, list):
            refs = []
        matching = [ref for ref in refs if isinstance(ref, dict)
                    and isinstance(ref.get('metadata'), dict)
                    and ref['metadata'].get('applicable') is True
                    and ref['metadata'].get('task_id') == name
                    and ref['metadata'].get('plan_ref') == plan_ref
                    and ref['metadata'].get('producer_execution_id') == record['result']['command_id']
                    and ref['metadata'].get('run_id') == header.get('logical_run_id', header['run_id'])]
        if matching:
            payload['diagnostic_repair_refs'] = json_copy(matching)
    return payload


def allowed(group, name):
    hold = state(group)['holds'].get(name)
    return hold is None or hold['status'] == 'ready'


def dependencies_ready(group, name, finished):
    task = next(task for task in group['tasks'] if task['id'] == name)
    dependencies = set(task.get('dependencies', ()))
    if dependencies <= finished:
        return True
    hold = state(group)['holds'].get(name, {})
    # A planner can explicitly use the available output of a settled failed B;
    # that authorization is different from silently treating B as successful.
    return (hold.get('status') == 'ready' and dependencies <= results(group).keys()
            and all(state(group)['holds'].get(key, {}).get('status') in {None, 'stopped'}
                    for key in dependencies))


def dispatched(group, name):
    hold = state(group)['holds'].get(name)
    if hold:
        hold['status'] = 'running'


def stopped_group(snapshot, group):
    revival = state(group)
    if revival['pending']:
        return False
    stopped = {name for name, hold in revival['holds'].items() if hold['status'] == 'stopped'}
    if not stopped:
        return False
    blocked = stopped | _descendants(group, stopped)
    unfinished = {task['id'] for task in group['tasks']} - completed(group)
    return unfinished <= blocked and not any(_active(snapshot, group, task['id']) for task in group['tasks'])
