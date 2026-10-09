"""Resume a diagnosed assignment through its existing branch and tool custody."""

from pathlib import Path

from .contracts import json_copy
from .rework_orchestration import _HOST_PAYLOAD_FIELDS


_HOST_OPTIONS = frozenset({'workflow_version', 'agent_assignment', 'rework_round',
    'model', 'reasoning_effort', 'model_policy', 'gate_policy', 'business_gates_disabled',
    'progress_supervision_policy', 'validation_policy', 'agent_dialogue_policy',
    'acceptance_rubric_sha256', 'native_goal_resume'})


class ClosedReworkRequest(ValueError):
    """The caller no longer authorizes this child; retain its failed result."""


def capacity_available(owner, snapshot, header, app, stage, *, execution_id=None, caller_id=None):
    if owner.memory_policy.for_stage(stage) is None:
        return True
    return bool(owner._memory_capacity(snapshot, header, app, 'failure_resume',
        requested_stage=stage,
        exclude_execution_ids=tuple(value for value in (execution_id, caller_id) if value),
        retained_memory_execution_ids=(caller_id,) if caller_id else ()))


def resume_assignment(owner, snapshot, header, app, episode, command, decision):
    group = app.get('active_group')
    member = isinstance(group, dict) and command.task_id in group.get('members', [])
    record = next((row for row in app.get('review_rework', {}).get('requests', {}).values()
                   if row.get('task_id') == command.task_id), None)
    if record is not None:
        caller = next((attempt for task in snapshot['tasks'].values() for attempt in task['attempts']
                       if attempt['command']['execution_id'] == record['reviewer_execution_id']), None)
        if (record.get('state') != 'running' or caller is None
                or caller['state'] in {'succeeded', 'failed', 'cancelled', 'timed_out', 'dead'}
                or owner._tool_cancelled(Path(header['run_dir']), record)):
            raise ClosedReworkRequest('The failed rework no longer has a live caller/tool request. '
                                      'Retain its result for the original caller.')
    if command.payload.get('regression_scope') and not member and record is None:
        raise ValueError('The failed regression scope no longer belongs to the current group. '
                         'Repair through the current independent-test author before fresh verification.')

    payload = {key: value for key, value in command.payload.items() if key not in _HOST_PAYLOAD_FIELDS}
    payload['watchdog_recovery'] = {'incident_id': episode['request']['incident_id'],
        'instruction': decision['instruction'], 'reason': decision['reason'],
        'previous_execution_id': command.command_id}
    options = {key: value for key, value in command.options.items() if key not in _HOST_OPTIONS}
    # Deadline is an original upper bound; the scheduler intersects it with the
    # current Run budget and assigns fresh SDK execution authority and usage.
    refs = {**command.artifact_refs, **owner._refs(header, app)}
    upstream = {**command.upstream_results, **app['effective']}
    if member and group.get('kind') == 'regression':
        refs = {**owner._refs(header, app), **group.get('artifact_refs', {}), **command.artifact_refs}
        scope = command.payload['regression_scope']
        for stage in ('test_design', 'test_review'):
            if stage == command.stage_id:
                break
            result = group['results'].get(f'{stage}.g{group["generation"]}.{scope["scope_id"]}')
            if result is not None:
                upstream[stage] = result
                refs.update(result.get('outputs', {}).get('artifact_refs', {}))
                options['workspace'] = result.get('outputs', {}).get('workspace', options.get('workspace'))
    if command.payload.get('regression_results') is not None:
        generation = command.payload['regression_generation']
        for field, stage in (('regression_results', 'test_execute'),
                             ('regression_designs', 'test_design'),
                             ('regression_reviews', 'test_review')):
            if field not in command.payload:
                continue
            rows = []
            for scope in command.payload['regression_scopes']:
                key = f'{stage}.g{generation}.{scope["scope_id"]}'
                current = app['effective'].get(key)
                if current is None:
                    raise ValueError('Fresh regression input is unavailable: ' + key)
                rows.append(json_copy(current))
            payload[field] = rows
    if command.stage_id == 'test_design':
        options['workspace'] = f'workspaces/tests/recovery-{app["agent_assignments"] + 1}'
    if command.stage_id == 'agent_rework':
        payload['rework_generation'] = app['agent_assignments'] + 1
        options.pop('workspace', None)
    capacity = capacity_available(owner, snapshot, header, app, command.stage_id,
        caller_id=record.get('reviewer_execution_id') if record else None)
    operations = owner._schedule(snapshot, header, app, command.stage_id, task_id=command.task_id,
        dependencies=[], causation_id=episode['supervisor_result']['command_id'],
        activate=not (member or record is not None or command.task_id in app.get('early_pending', [])
                      or command.task_id in app.get('gap_pending', [])),
        payload=payload, artifact_overrides=refs, upstream_overrides=upstream,
        prior_findings_override=command.prior_findings, extra_options=options)
    assignment = next((op for op in operations if op['kind'] in {'add_task', 'new_attempt'}), None)
    if assignment and not capacity:
        operations = [op for op in operations if op['kind'] != 'dispatch']
        episode['resume_pending'] = {'task_id': command.task_id, 'stage': command.stage_id,
            'execution_id': assignment['command']['execution_id'],
            'caller_id': record.get('reviewer_execution_id') if record else None}
    if member and assignment:
        # Keep peer results and historical SDK attempts. The owning group will
        # consume the new result under the same task ID after it actually settles.
        group.get('results', {}).pop(command.task_id, None)
        if group.get('failure', {}).get('task_id') == command.task_id:
            group.pop('failure', None)
    return operations
