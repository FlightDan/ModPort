"""Durable SDK assignments for explicitly authorized integration conflicts."""

import json
from pathlib import Path

from .contracts import OperationInput, OperationResult, json_copy
from .development import development_workspace
from .evidence import atomic_json, verified_path
from .execution_plan import normalize_execution_plan


_TERMINAL = frozenset({'succeeded', 'failed', 'cancelled', 'timed_out', 'dead'})
INTEGRATION_STAGES = frozenset({'development_integrate', 'development_prepare_integrate',
                              'target_repair_integrate', 'contract_repair_integrate'})


def _read(root, ref):
    return json.loads(verified_path(root, ref).read_text(encoding='utf-8'))


def _fail(owner, app, state, code, detail):
    state.update(status='failed', error_code=code, detail=detail)
    return owner._finish(app, code)


def _prepare(header, app, merge_ref):
    root = Path(header['run_dir'])
    record = _read(root, merge_ref)
    original = OperationInput.from_dict(_read(root, record['command_ref']))
    if original.stage_id not in INTEGRATION_STAGES or Path(original.run_dir) != root:
        raise ValueError('integration repair command is outside this integration boundary')
    conflicts = record['conflicts']
    if not isinstance(conflicts, list) or not conflicts:
        raise ValueError('integration repair needs actual merge conflicts')
    sequence = app.get('integration_repair_sequence', 0) + 1
    generation = max(app.get('development_generation', 0),
                     original.payload.get('development_generation', 0)) + 1
    task_name = 'integration-repair-' + str(sequence)
    paths = list(dict.fromkeys(path for conflict in conflicts for path in conflict['paths']))
    objective = (
        'Resolve the actual integration conflicts in this isolated materialized merge. '
        'Preserve the current candidate and user changes, and the intended contributions '
        'of every original development task. The user authorized this integration repair; '
        'finish the remaining merge without requesting another approval or replanning '
        'the migration. Edit project files only; do not execute project code, builds or '
        'tests, or stage/commit files. Original frozen contracts and evidence remain '
        'reference material. Host conflict evidence and original patch references:\n'
        + json.dumps({'conflicts': conflicts, 'patch_refs': record['patch_refs'],
                      'original_task_ids': record['original_task_ids'],
                      'source_workspace': record['source_workspace'], 'start': record['start'],
                      'merge_ref': merge_ref}, ensure_ascii=False))
    plan = normalize_execution_plan(
        {'schema_version': 1, 'base_commit': record['merge_head'], 'shared_paths': [],
         'tasks': [{'id': task_name, 'objective': objective, 'owned_paths': paths,
                    'dependencies': [], 'complexity': 'complex'}]},
        workflow_version=header['definition']['workflow_version'],
        model_policy=header.get('model_policy'))
    plan_path = root / 'artifacts' / 'integration-repair' / task_name / 'plan.json'
    if plan_path.is_symlink() or plan_path.resolve() != plan_path.absolute():
        raise ValueError('unsafe integration repair plan path')
    if plan_path.exists():
        plan = json.loads(plan_path.read_text(encoding='utf-8'))
    else:
        atomic_json(plan_path, plan)
    state = {'status': 'planned', 'sequence': sequence, 'merge_ref': json_copy(merge_ref),
             'command_ref': record['command_ref'], 'record': record, 'plan': plan,
             'plan_ref': {'path': plan_path.relative_to(root).as_posix(),
                          'metadata': {'development_base': record['merge_head']}},
             'task_id': 'coder.integration-repair.' + str(sequence),
             'generation': generation}
    previous = app.get('integration_repair')
    if previous:
        app.setdefault('integration_repair_history', []).append(json_copy(previous))
    app['integration_repair_sequence'] = sequence
    app['development_generation'] = generation
    app['integration_repair'] = state
    app['active_stage'] = None
    return state


def _dispatch(owner, snapshot, header, app, state):
    original = OperationInput.from_dict(_read(Path(header['run_dir']), state['command_ref']))
    task = state['plan']['tasks'][0]
    record = state['record']
    refs = {key: ref for key, ref in original.artifact_refs.items()
            if key not in {'coder_goal', 'supervised_goal_revision'}}
    refs.update(development_plan=state['plan_ref'], integration_merge=state['merge_ref'])
    operations = owner._schedule(
        snapshot, header, app, 'coder', task_id=state['task_id'], activate=False,
        dependencies=[], causation_id=original.command_id, artifact_overrides=refs,
        payload={'development_task': task, 'development_base': record['merge_head'],
                 'development_generation': state['generation'],
                 'development_source_workspace': record['workspace'],
                 'execution_development_plan': state['plan'], 'dependency_patches': [],
                 'planning_context': {'development_plan': state['plan_ref'],
                                      'integration_merge': state['merge_ref']},
                 'goal_scope': original.payload.get('goal_scope', 'migration'),
                 'development_kind': original.payload.get('development_kind'),
                 'integration_merge_ref': state['merge_ref']},
        extra_options={'workspace': development_workspace(state['generation'], task['id'], {}),
                       'model': task['model'], 'reasoning_effort': task['reasoning_effort']})
    if any(op['kind'] in {'add_task', 'new_attempt'} for op in operations):
        state['status'] = 'running'
    return operations


def start_or_resume(owner, snapshot, header, app, source_command=None, outcome=None):
    """Return SDK operations, or None when ordinary workflow routing should run.

    A pending repair returns an empty list while its exact SDK task is active.
    The caller must consult this helper before forwarding an integration result
    or a saved continuation boundary to cleanup.
    """
    if isinstance(outcome, dict):
        outcome = OperationResult.from_dict(outcome)
    state = app.get('integration_repair')
    merge_ref = (outcome.outputs.get('integration_merge_ref')
                 if outcome is not None and outcome.error_code == 'integration_merge_required'
                 else None)
    try:
        if merge_ref is not None and (state is None or state['merge_ref'] != merge_ref):
            if state is not None and state['status'] in {'planned', 'running', 'resolved'}:
                raise ValueError('another integration repair is still pending')
            state = _prepare(header, app, merge_ref)
        if state is None:
            return None
        if state['status'] == 'integrating':
            return None
        if state['status'] == 'failed':
            return owner._finish(app, state['error_code'])
        if state['status'] == 'planned':
            # A persisted SDK task is authoritative after driver restart; do
            # not charge an assignment again or create a replacement attempt.
            if state['task_id'] in snapshot['tasks']:
                state['status'] = 'running'
            else:
                return _dispatch(owner, snapshot, header, app, state)
        if state['status'] == 'running':
            task = snapshot['tasks'].get(state['task_id'])
            if task is None:
                return _fail(owner, app, state, 'integration_repair_task_missing',
                             'Persisted integration repair has no matching SDK task')
            attempts = task.get('attempts', [])
            if not attempts or attempts[-1]['state'] not in _TERMINAL:
                return []
            attempt = attempts[-1]
            command, result = owner._flowthrough_outcome(attempt)
            if (command.task_id != state['task_id'] or command.stage_id != 'coder'
                    or command.payload.get('development_task') != state['plan']['tasks'][0]):
                return _fail(owner, app, state, 'integration_repair_identity_invalid',
                             'SDK repair attempt differs from its persisted coder assignment')
            result.validate_for(command)
            state['coder_execution'] = {key: json_copy(attempt[key])
                                        for key in ('state', 'error') if key in attempt}
            state['coder_result'] = result.to_dict()
            owner._flowthrough_record(app, command, result, canonical=False)
            patch = result.outputs.get('artifact_refs', {}).get('coder_patch')
            if result.status != 'completed':
                return _fail(owner, app, state, result.error_code or 'integration_repair_failed',
                             result.detail or 'Integration repair coder did not complete')
            if not isinstance(patch, dict) or verified_path(Path(header['run_dir']), patch).stat().st_size == 0:
                return _fail(owner, app, state, 'integration_repair_patch_missing',
                             'Integration repair completed without an actual candidate patch')
            state.update(status='resolved', coder_patch=patch, coder_execution_id=command.command_id)
        if state['status'] == 'resolved':
            original = OperationInput.from_dict(_read(Path(header['run_dir']), state['command_ref']))
            options = {key: value for key, value in original.options.items()
                       if key not in {'agent_assignment', 'deadline_epoch', 'rework_round'}}
            operations = owner._schedule(
                snapshot, header, app, original.stage_id, task_id=original.task_id,
                dependencies=[], causation_id=state['coder_execution_id'],
                artifact_overrides=original.artifact_refs,
                upstream_overrides=original.upstream_results,
                payload={**original.payload, 'integration_resolution': {
                    'merge_ref': state['merge_ref'], 'coder_patch': state['coder_patch'],
                    'coder_execution_id': state['coder_execution_id']}},
                extra_options=options)
            if any(op['kind'] in {'add_task', 'new_attempt'} for op in operations):
                state['status'] = 'integrating'
            return operations
        return None
    except (OSError, ValueError, TypeError, KeyError) as exc:
        if state is None:
            state = {'status': 'failed', 'merge_ref': merge_ref}
            app['integration_repair'] = state
        return _fail(owner, app, state, 'integration_repair_invalid', str(exc))
