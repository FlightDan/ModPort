"""SDK-owned idle observation state and supervisor-authorized cancellation.

Useful-work observations are host diagnostics. Only an admitted supervisor
response can request cancellation; a quiet window never cancels an execution.
"""
from pathlib import Path
from collections.abc import Mapping

from .contracts import OperationInput
from .workflow import agent_stage
from .progress_supervisor import validate_progress_supervisor_decision
from .progress_watchdog import capture_progress, compare_progress, read_snapshot, write_snapshot


TERMINAL = {'succeeded', 'failed', 'cancelled', 'timed_out', 'dead'}
ACTIVE = {'running', 'leased'}


def _operation(attempt):
    return OperationInput.from_dict(attempt['command']['payload'])


def _store(root, execution_id, entry, snapshot):
    # Alternate slots so a failed SDK compare-and-swap leaves the committed
    # observation untouched. Only the SDK commit publishes the new slot path.
    old = entry.get('snapshot_path', '')
    slot = 'previous' if old.endswith('/latest.json') else 'latest'
    return write_snapshot(root, execution_id, snapshot, slot=slot)


def decide(owner, snapshot, header, app):
    policy = header['definition']['progress_supervision_policy']
    interval = policy['observation_interval_seconds']
    threshold = policy['idle_windows_before_review']
    now = owner.clock()
    state = app.setdefault('progress_supervision', {'executions': {}, 'reviews': {}})
    tracked, reviews = state['executions'], state['reviews']
    operations = []

    # Consume each response once, against the exact still-current SDK attempt.
    for review_id, review in reviews.items():
        if review.get('status') != 'pending':
            continue
        task = snapshot['tasks'].get(review['supervisor_task_id'])
        if not task or task['attempts'][-1]['state'] not in TERMINAL:
            continue
        attempt = task['attempts'][-1]
        command, outcome = owner._flowthrough_outcome(attempt)
        from .diagnostic_repair_routing import register
        register(app, command, outcome)
        review.update(status='observed', completed_at=now,
                      supervisor_execution_id=command.command_id,
                      error_code=outcome.error_code)
        target = snapshot['tasks'].get(review['target_task_id'])
        current = target['attempts'][-1] if target else None
        observation = tracked.get(review['target_execution_id'], {})
        observation['pending_review'] = None
        observation['last_observed_at'] = now
        observation['idle_windows'] = 0
        if (current is None or current['command']['execution_id'] != review['target_execution_id']
                or current['state'] in TERMINAL):
            review['status'] = 'superseded'
            continue
        try:
            if outcome.status != 'completed':
                raise ValueError('supervisor did not complete successfully')
            decision = validate_progress_supervisor_decision(
                outcome.outputs.get('progress_supervisor_decision'), review['request'])
        except (ValueError, TypeError, KeyError) as exc:
            review.update(status='invalid', diagnostic=str(exc))
            continue
        review['decision'] = decision
        if decision['decision'] == 'continue':
            review['status'] = 'continue'
            continue
        # The worker may have progressed while the model investigated. A
        # decision based on an older idle window cannot cancel that new work.
        baseline = read_snapshot(header['run_dir'], observation['snapshot_path']) if observation.get('snapshot_path') else None
        fresh = capture_progress(header['run_dir'], _operation(current), baseline)
        latest = compare_progress(baseline, fresh) if baseline else None
        observation['snapshot_path'] = _store(header['run_dir'], review['target_execution_id'], observation, fresh)
        if latest and latest['useful_progress']:
            review.update(status='superseded_by_progress', fresh_observation=latest)
            continue
        # SDK owns authority revocation and process/sandbox cleanup. Never kill
        # a PID or fabricate a completed cancellation receipt here.
        review['status'] = 'termination_requested'
        state.setdefault('terminated_executions', {})[review['target_execution_id']] = {
            'task_id': review['target_task_id'], 'review_id': review_id,
            'reason': decision['reason'], 'requested_at': now,
            'scope': ('source' if _operation(current).stage_id in {'contract_draft', 'contract_review'}
                      else 'target' if _operation(current).stage_id == 'artifact_test_design' else None)}
        if review['target_execution_id'] not in app['cancel_sent']:
            operations.append({'kind': 'cancel', 'task_id': review['target_task_id'],
                               'reason': 'progress_supervisor: ' + decision['reason']})
            app['cancel_sent'].append(review['target_execution_id'])

    active = []
    waiting_parents = set()
    for task_id, task in snapshot['tasks'].items():
        attempt = task['attempts'][-1]
        if attempt['state'] not in ACTIVE:
            continue
        command = _operation(attempt)
        if command.stage_id == 'supervisor' or not agent_stage(header, command.stage_id):
            continue
        parent = command.payload.get('reviewer_rework', {}).get('reviewer_execution_id')
        if parent:
            waiting_parents.add(parent)
        active.append((task_id, attempt, command))
    # Reviewer tool waits are driven by a live child. Observe the leaf doing
    # work instead of spending two supervisor assignments on the same wait.

    for task_id, attempt, command in active:
        execution_id = command.command_id
        if execution_id in waiting_parents or execution_id in state.get('terminated_executions', {}):
            continue
        entry = tracked.setdefault(execution_id, {'task_id': task_id, 'idle_windows': 0,
                                                   'review_count': 0})
        if entry.get('pending_review'):
            continue
        if now - entry.get('last_observed_at', now - interval) < interval:
            continue
        previous = read_snapshot(header['run_dir'], entry['snapshot_path']) if entry.get('snapshot_path') else None
        current = capture_progress(header['run_dir'], command, previous)
        comparison = compare_progress(previous, current) if previous else None
        # Sidecar slots are only bounded observations. SDK state owns the
        # counter, target identity and admission/decision ledger.
        previous_path = entry.get('snapshot_path')
        path = _store(header['run_dir'], execution_id, entry, current)
        entry.update(snapshot_path=path, last_observed_at=now)
        if comparison is None:
            continue
        entry['last_observation'] = comparison
        entry['idle_windows'] = 0 if comparison['useful_progress'] else entry['idle_windows'] + 1
        if entry['idle_windows'] < threshold:
            continue
        from .watchdog_events import enabled as watchdog_enabled, accept_notification
        if watchdog_enabled(header, app):
            number = entry['review_count'] + 1
            accept_notification(header['run_dir'], {
                'notification_id': f'progress:{execution_id}:{number}',
                'run_id': header['run_id'], 'task_id': task_id,
                'execution_id': execution_id, 'generation': snapshot.get('generation', 0),
                'kind': 'no_useful_progress',
                'reason': f'{threshold} consecutive observation windows without measured useful work',
                'observation': comparison, 'snapshot_paths': [previous_path, path]})
            entry.update(review_count=number, idle_windows=0)
            continue
        limit = header['request']['budget']['max_agent_assignments']
        if limit is not None and app['agent_assignments'] >= limit:
            # No authority/budget for a model decision. Do not equate missing
            # supervision with permission to terminate the active worker.
            entry['supervisor_unavailable'] = 'agent_assignment_budget_exhausted'
            continue
        number = entry['review_count'] + 1
        review_id = f'progress.{command.options.get("agent_assignment", 0)}.{command.attempt}.{number}'
        supervisor_task_id = 'supervisor.' + review_id
        request = {'review_id': review_id, 'target_task_id': task_id,
                   'target_execution_id': execution_id, 'idle_windows': entry['idle_windows'],
                   'interval_seconds': interval,
                   'observation': {**comparison, 'snapshot_paths': [previous_path, path],
                       'evidence_paths': current.get('evidence_paths', []),
                       'workspace': current.get('workspace'),
                       'execution_state': attempt['state'],
                       'target_stage': command.stage_id,
                       'rework_waits': [
                           {'child_task_id': row.get('task_id'),
                            'waiting_resources': bool(row.get('waiting_resources')),
                            'state': row.get('state'),
                            'child_state': (snapshot['tasks'].get(row.get('task_id'), {}).get(
                                'attempts') or [{}])[-1].get('state')}
                           for row in app.get('review_rework', {}).get('requests', {}).values()
                           if row.get('reviewer_execution_id') == execution_id][:8]}}
        workspace = 'workspaces/progress-supervision/' + review_id
        (Path(header['run_dir']) / workspace).mkdir(parents=True, exist_ok=True)
        repair_payload = {}
        if (header['definition'].get('workflow_version', 0) >= 40
                and command.stage_id == 'coder'
                and command.payload.get('goal_scope') != 'contract'
                and isinstance(command.payload.get('development_task'), Mapping)
                and isinstance(command.artifact_refs.get('development_plan'), Mapping)):
            target = {'task_id': command.payload['development_task']['id'],
                      'plan_ref': command.artifact_refs['development_plan'],
                      'source_workspace': command.options.get('workspace'),
                      'source_execution_id': command.command_id}
            repair_payload['diagnostic_repair_targets'] = [target]
            repair_payload['diagnostic_repair_refs'] = [
                ref for ref in app.get('diagnostic_repairs', ())
                if ref.get('metadata', {}).get('task_id') == target['task_id']
                and ref.get('metadata', {}).get('plan_ref') == target['plan_ref']]
        produced = owner._schedule(snapshot, header, app, 'supervisor',
            task_id=supervisor_task_id, dependencies=[], activate=False,
            payload={'progress_supervision': request, **repair_payload},
            extra_options={'workspace': workspace})
        if any(op['kind'] in {'add_task', 'new_attempt'} for op in produced):
            entry.update(pending_review=review_id, review_count=number)
            reviews[review_id] = {'status': 'pending', 'supervisor_task_id': supervisor_task_id,
                'target_task_id': task_id, 'target_execution_id': execution_id,
                'requested_at': now, 'request': request}
        operations.extend(produced)
    return operations
