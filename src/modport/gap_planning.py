"""Project-local alternatives and independent review; never evidence of closure."""
from .workspace import project_path
from copy import deepcopy
import json

from . import handlers
from .evidence import verified_path
from .planning import _gap_ids, _strings


def _task_index(tasks):
    if not isinstance(tasks, list):
        raise ValueError('tasks must be an array')
    indexed = {}
    for task in tasks:
        if not isinstance(task, dict) or not isinstance(task.get('id'), str) or not task['id'].strip() or task['id'] in indexed:
            raise ValueError('invalid or duplicate task ID')
        _strings(task.get('dependencies', []), 'dependencies', empty=True)
        indexed[task['id']] = task
    for task in tasks:
        if not set(task.get('dependencies', [])) <= indexed.keys():
            raise ValueError('unknown task dependency')
    pending, done = set(indexed), set()
    while pending:
        ready = {key for key in pending if set(indexed[key].get('dependencies', [])) <= done}
        if not ready:
            raise ValueError('task dependency cycle')
        done |= ready
        pending -= ready
    return indexed


def dependency_closure(tasks, seeds):
    """Return seeds and all transitive consumers, rejecting malformed DAGs."""
    indexed = _task_index(tasks)
    affected = set(seeds)
    if not affected <= indexed.keys():
        raise ValueError('unknown affected task')
    while True:
        expanded = affected | {key for key, task in indexed.items()
                               if set(task.get('dependencies', [])) & affected}
        if expanded == affected:
            return affected
        affected = expanded


def reconcile_task_updates(tasks, updates, completed, seeds):
    """Apply partial updates only within the affected DAG; preserve reusable work.

    Compare explicit task values, not hashes. Scheduling annotations alone do
    not invalidate a result. Changed implementation contracts invalidate their
    consumers as well because their consumed upstream result has changed.
    """
    indexed = _task_index(tasks)
    allowed = dependency_closure(tasks, seeds)
    if not isinstance(updates, list):
        raise ValueError('task_updates must be an array')
    merged = deepcopy(indexed)
    seen, changed = set(), set()
    scheduling = {'blocked_by_gaps', 'status', 'reason'}
    for update in updates:
        if not isinstance(update, dict):
            raise ValueError('task update must be an object')
        key = update.get('id')
        if not isinstance(key, str) or key not in allowed or key in seen:
            raise ValueError('duplicate or unrelated task update')
        seen.add(key)
        merged[key].update(deepcopy(update))
        if {k: v for k, v in indexed[key].items() if k not in scheduling} != {k: v for k, v in merged[key].items() if k not in scheduling}:
            changed.add(key)
    result = list(merged.values())
    _task_index(result)
    invalidated = dependency_closure(tasks, changed) | dependency_closure(result, changed)
    if not invalidated <= allowed:
        raise ValueError('task update affects unrelated tasks')
    if isinstance(completed, dict):
        completed_ids = set(completed)
    elif isinstance(completed, list):
        if any(not isinstance(row, (str, dict)) for row in completed):
            raise ValueError('invalid completed task result')
        completed_ids = {row if isinstance(row, str) else row.get('task_id', row.get('id')) for row in completed}
    else:
        raise ValueError('completed results must be an object or array')
    if not completed_ids <= indexed.keys():
        raise ValueError('unknown completed task')
    return result, invalidated, completed_ids - invalidated


ACTIONS = {'existing_answer', 'compatibility_layer', 'alternative_api', 'implementation_change', 'wait_admin'}
SCHEMA = """resolutions=[{gap_id,action:existing_answer|compatibility_layer|alternative_api|implementation_change|wait_admin,alternative_id,rationale,evidence_artifact_ids:[existing artifact alias],affected_tasks:[existing task ID],verification_requirements:[{id,closure_criteria:[nonblank strings],resolution_stage:target_build|test_execute|client_smoke}]}], task_updates optional array of partial existing tasks with id. Cover all unresolved gaps exactly once. Every actionable alternative needs evidence and new verification obligations. Preserve the frozen behavior contract and every existing acceptance requirement. Never mark verification passed or change general skill unknowns. Only project-local bypass is possible after independent approval; actual closure requires subsequent execution evidence. Do not repeat an attempted alternative without new authenticated evidence. If no viable new approach exists use wait_admin. Task updates may affect only the affected tasks and downstream consumers; preserve completed work when inputs, interfaces and behavior requirements remain unchanged."""


SCHEMA += (
    " JSON types: resolutions and verification_requirements are arrays of objects; gap_id, action,"
    " alternative_id, rationale and requirement id are nonblank strings. evidence_artifact_ids,"
    " affected_tasks and closure_criteria are arrays of unique nonblank strings. All are nonempty"
    " except evidence_artifact_ids/affected_tasks/verification_requirements may be [] for wait_admin,"
    " and affected_tasks may be [] when no development tasks exist. Obligation IDs are unique across"
    " the plan. task_updates is an array of objects; id is an existing task ID string and every"
    " updated field retains the exact JSON type of the supplied task. dependencies is an array of"
    " unique existing task ID strings. Even wait_admin needs nonblank alternative_id and rationale."
)
REVIEW_SCHEMA = (
    " Output is a JSON object with verdict (approved or rejected), optional findings in any"
    " readable form, approved_gap_resolutions (array of objects), and approved_task_updates"
    " (array of objects). These approval arrays are machine instructions applied by the host."
    " Independently derive them from the raw proposal and evidence; do not copy an envelope,"
    " identity or digest. For rejection both approval arrays must be []. For approval use the"
    " resolution and task-update requirements below, naming the arrays approved_gap_resolutions"
    " and approved_task_updates. The proposal itself can be prose or legacy JSON. "
)


def validate_gap_plan(command, document):
    # Called only for the reviewer's actionable decision, not the proposal.
    if not isinstance(document, dict):
        raise ValueError('gap decision must be an object')
    gaps = _gap_ids(command.payload.get('unresolved_knowledge_gaps'), 'unresolved_knowledge_gaps')
    tasks = command.payload.get('development_tasks', [])
    indexed = _task_index(tasks)
    rows = document.get('resolutions')
    if not isinstance(rows, list):
        raise ValueError('resolutions must be an array')
    seen, affected, obligations = set(), set(), set()
    attempts = command.payload.get('attempted_gap_alternatives', [])
    if not isinstance(attempts, list) or any(not isinstance(item, dict) for item in attempts):
        raise ValueError('attempted_gap_alternatives must be an array of objects')
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError('resolution must be an object')
        gap = row.get('gap_id')
        if not isinstance(gap, str) or gap not in gaps or gap in seen:
            raise ValueError('unknown or duplicate gap resolution')
        seen.add(gap)
        action = row.get('action')
        if action not in ACTIONS:
            raise ValueError('invalid gap action')
        for field in ('alternative_id', 'rationale'):
            if not isinstance(row.get(field), str) or not row[field].strip():
                raise ValueError('missing ' + field)
        evidence = _strings(row.get('evidence_artifact_ids'), 'evidence_artifact_ids', empty=action == 'wait_admin')
        for alias in evidence:
            if alias not in command.artifact_refs:
                raise ValueError('unknown gap evidence artifact')
            verified_path(handlers._run_root(command), command.artifact_refs[alias])
        affected_tasks = _strings(row.get('affected_tasks'), 'affected_tasks', empty=not tasks or action == 'wait_admin')
        if not affected_tasks <= indexed.keys():
            raise ValueError('unknown affected task')
        affected |= affected_tasks
        requirements = row.get('verification_requirements')
        if not isinstance(requirements, list) or (not requirements and action != 'wait_admin'):
            raise ValueError('alternative requires verification obligations')
        for requirement in requirements:
            if not isinstance(requirement, dict) or not isinstance(requirement.get('id'), str) or not requirement['id'].strip() or requirement['id'] in obligations:
                raise ValueError('invalid or duplicate verification obligation')
            obligations.add(requirement['id'])
            _strings(requirement.get('closure_criteria'), 'closure_criteria')
            if requirement.get('resolution_stage') not in ('target_build', 'test_execute', 'client_smoke'):
                raise ValueError('invalid verification resolution_stage')
        for attempt in attempts:
            if action != 'wait_admin' and attempt.get('gap_id') == gap and attempt.get('alternative_id') == row['alternative_id'] and not evidence - set(attempt.get('evidence_artifact_ids', [])):
                raise ValueError('repeated gap alternative without new evidence')
    if seen != gaps:
        raise ValueError('gap plan omitted unresolved gaps')
    updates = document.get('task_updates', [])
    reconcile_task_updates(tasks, updates, command.payload.get('completed_development_results', {}), affected)
    for update in updates:
        original = indexed[update['id']]
        for field in ('acceptance', 'behavior_requirements', 'behavior', 'source_acceptance', 'source_objectives'):
            if field in update and update[field] != original.get(field):
                raise ValueError('task update must preserve frozen behavior requirements')
    return rows, updates


class GapPlanHandler:
    """One planner execution. Retry policy and research budgets belong to host."""
    review = False

    def __call__(self, command):
        root = handlers._run_root(command)
        stage = 'gap_plan_review' if self.review else 'gap_plan'
        relative = '.modport/gap-plan-review.json' if self.review else '.modport/gap-plan.json'
        workspace = project_path(root, 'worktree')
        path = workspace / relative
        result = None
        try:
            if command.stage_id != stage or path.parent.resolve() != path.parent.absolute() or path.is_symlink():
                raise ValueError('invalid gap planner stage or workspace')
            candidate_text = None
            if self.review:
                candidate_ref = command.artifact_refs['gap_plan']
                candidate_path = verified_path(root, candidate_ref)
                producer = candidate_ref.get('metadata', {}).get('producer_execution_id')
                # Older host snapshots encode their producer in the path.
                parts = candidate_path.relative_to(root).parts
                if producer is None and parts[:3] == ('artifacts', 'stage-outputs', 'gap_plan') and len(parts) > 3:
                    producer = parts[3]
                if producer == command.command_id:
                    raise ValueError('gap reviewer must be an independent execution')
                candidate_text = candidate_path.read_bytes().decode('utf-8', errors='replace')
            path.unlink(missing_ok=True)
            if self.review:
                prompt = ('Independently review the raw candidate gap_plan and reject unsupported '
                          'behavior preservation. ' + REVIEW_SCHEMA + '\n' + SCHEMA)
            else:
                prompt = ('Plan project-local alternatives for exhausted knowledge gaps. Write a readable '
                          'report for the independent reviewer, in prose or your preferred format. '
                          'Explain alternatives, evidence, affected tasks, verification obligations and '
                          'remaining unknowns. Preserve frozen behavior and acceptance requirements. '
                          'Do not repeat an attempted alternative without new evidence; explain when '
                          'administrator input is needed. No JSON schema, identity or hash echo is required.')
            prompt += ('\nDo not execute project code. Write ONLY ' + relative +
                       '\nHost inputs: ' + json.dumps(dict(command.payload)))
            if candidate_text is not None:
                prompt += '\nRaw candidate report (input evidence):\n' + candidate_text
            result = handlers.CodexStageHandler(prompt, required_paths=(relative,))(command)
            if result.status != 'completed':
                return result
            if path.is_symlink() or path.resolve() != path.absolute():
                raise ValueError('unsafe gap plan output')
            if self.review:
                from .rework_tools import refresh_review_command
                command = refresh_review_command(command)
                from .agent_reports import review_decision, report_findings
                document = review_decision(path.read_text())
                if not isinstance(document, dict) or document.get('verdict') not in ('approved', 'rejected'):
                    raise ValueError('invalid independent gap review decision')
                approved = document['verdict'] == 'approved'
                rows = document.get('approved_gap_resolutions', [])
                updates = document.get('approved_task_updates', [])
                if approved:
                    rows, updates = validate_gap_plan(command, {'resolutions': rows, 'task_updates': updates})
                elif rows != [] or updates != []:
                    raise ValueError('rejected gap review cannot authorize changes')
                outputs = {'verdict': document['verdict'], 'findings': report_findings(document),
                           'approved_gap_resolutions': rows, 'approved_task_updates': updates}
            else:
                outputs = {}
            artifact_id, ref = handlers._snapshot_stage_output(root, workspace, command, relative)
            ref['metadata'] = {**ref.get('metadata', {}), 'producer_execution_id': command.command_id}
            outputs['artifact_refs'] = {artifact_id: ref, stage: ref}
            return handlers._result(command, 'completed', outputs=outputs)
        except (OSError, ValueError, TypeError, KeyError) as exc:
            return handlers._result(command, 'failed', outputs=dict(result.outputs) if result else {}, detail=str(exc), error_code=stage + '_invalid')


class GapPlanReviewHandler(GapPlanHandler):
    review = True
