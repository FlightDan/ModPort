"""Turn available planning output into executable work without approval gates.

Author reports are retained separately.  This projection supplies runtime IDs,
defaults and an acyclic dispatch order; it does not certify the plan or tests.
"""

from collections.abc import Mapping
import json
import re


_STABLE_TASK_ID = re.compile(r'[A-Za-z0-9][A-Za-z0-9._-]{0,63}')
_FENCE_START = re.compile(r'^ {0,3}(`{3,}|~{3,})([^\r\n]*)$')


def _embedded_task_plans(text):
    """Read complete JSON fences without interpreting other example blocks."""
    fence, language, body = None, None, []
    candidates = []
    for line in text.splitlines():
        if fence is None:
            match = _FENCE_START.fullmatch(line)
            if match:
                fence, language = match.group(1), match.group(2).strip().lower()
                body = []
            continue
        closing = line.strip()
        if (len(closing) >= len(fence) and set(closing) == {fence[0]}
                and len(line) - len(line.lstrip(' ')) <= 3):
            if language in {'', 'json'}:
                try:
                    value = json.loads('\n'.join(body))
                except ValueError:
                    value = None
                source = value.get('development_plan', value) if isinstance(value, Mapping) else value
                if (isinstance(source, Mapping) and isinstance(source.get('tasks'), list)
                        and value not in candidates):
                    candidates.append(value)
            fence, language, body = None, None, []
        else:
            body.append(line)
    return candidates


def decode_report(text):
    """Use a JSON object when supplied, otherwise keep the author's prose."""
    if not isinstance(text, str):
        return text
    try:
        return json.loads(text)
    except ValueError:
        pass
    stripped = text.strip()
    if stripped.startswith('```'):
        lines = stripped.splitlines()
        if len(lines) > 2 and lines[-1].strip() == '```':
            try:
                return json.loads('\n'.join(lines[1:-1]))
            except ValueError:
                pass
    candidates = _embedded_task_plans(text)
    if len(candidates) == 1:
        return candidates[0]
    if len(candidates) > 1:
        return {'raw_report': text, 'diagnostics': [{
            'code': 'ambiguous_embedded_task_plans',
            'detail': 'Multiple distinct task plans were supplied; original report retained.'}]}
    return {'raw_report': text}


def _strings(value):
    if isinstance(value, str):
        return [value] if value.strip() else []
    if not isinstance(value, (list, tuple)):
        return []
    return list(dict.fromkeys(item for item in value if isinstance(item, str) and item.strip()))


def _path(value):
    """Keep relative project paths; normalize directory globs into prefixes."""
    value = value.rstrip('/')
    while value.endswith('/**') or value.endswith('/*'):
        value = value.rsplit('/', 1)[0]
    if value.startswith('./'):
        value = value[2:]
    parts = value.split('/')
    if (not value or value.startswith('/') or '\\' in value or ':' in parts[0]
            or any(ord(char) < 32 for char in value)
            or any(part in {'', '.', '..', '.git'} for part in parts)):
        return None
    return value


def _ordered_structured_tasks(rows):
    """Validate and topologically order an explicit v22 task DAG.

    IDs and dependency edges are part of the planner's contract. Invalid or
    ambiguous structure is not repaired here: callers retain the full report
    in a visible single-task fallback instead.
    """
    if not isinstance(rows, list) or not rows:
        raise ValueError('tasks must be a nonempty array')

    tasks = []
    identifiers = set()
    for index, row in enumerate(rows):
        if not isinstance(row, Mapping):
            raise ValueError(f'task {index + 1} must be an object')
        identifier = row.get('id')
        if (not isinstance(identifier, str)
                or _STABLE_TASK_ID.fullmatch(identifier) is None):
            raise ValueError(f'task {index + 1} has no safe stable id')
        if identifier in identifiers:
            raise ValueError(f'duplicate task id: {identifier}')
        identifiers.add(identifier)

        objective = row.get('objective')
        if not isinstance(objective, str) or not objective.strip():
            raise ValueError(f'task {identifier} has no explicit objective')
        dependencies = row.get('dependencies')
        if (not isinstance(dependencies, list)
                or any(not isinstance(item, str) or not item for item in dependencies)):
            raise ValueError(f'task {identifier} dependencies must be an explicit string array')
        if len(dependencies) != len(set(dependencies)):
            raise ValueError(f'task {identifier} has duplicate dependencies')
        tasks.append(dict(row))

    for task in tasks:
        for dependency in task['dependencies']:
            if dependency not in identifiers:
                raise ValueError(f'task {task["id"]} has unknown dependency: {dependency}')

    ordered = []
    done = set()
    pending = list(tasks)
    while pending:
        ready = [task for task in pending if set(task['dependencies']) <= done]
        if not ready:
            raise ValueError('task dependencies contain a cycle')
        for task in ready:
            ordered.append(task)
            done.add(task['id'])
            pending.remove(task)
    return ordered


def _fallback_report(plan, fallback_objective):
    if isinstance(fallback_objective, str) and fallback_objective.strip():
        return fallback_objective
    if isinstance(plan, Mapping):
        for key in ('raw_report', 'objective'):
            value = plan.get(key)
            if isinstance(value, str) and value.strip():
                return value
    try:
        return json.dumps(plan, ensure_ascii=False, sort_keys=True, default=str)
    except (TypeError, ValueError):
        return str(plan) if plan is not None else None


def normalize_execution_plan(plan, *, base_commit=None, fallback_objective=None,
                             workflow_version=None, model_policy=None):
    """Produce tasks from partial settings and report every normalization.

    File ownership and acceptance/check fields describe work, not permission to
    proceed.  The caller controls the isolated workspace and actual Git base.
    """
    from .workflow import WORKFLOW_VERSION, agent_model_policy
    effective_version = WORKFLOW_VERSION if workflow_version is None else workflow_version
    model_default, reasoning_default = agent_model_policy(effective_version, "coder", model_policy)
    # Only explicitly versioned v22+ Runs opt into the stricter DAG contract.
    # Unversioned callers retain the historical projection semantics.
    strict_work_packages = type(workflow_version) is int and workflow_version >= 22

    if isinstance(plan, str):
        plan = decode_report(plan)
    if isinstance(plan, list):
        plan = {'tasks': plan}
    if not isinstance(plan, Mapping):
        plan = {}
    source = plan.get('development_plan', plan)
    if not isinstance(source, Mapping):
        source = {}
    diagnostics = list(source.get('diagnostics', [])) if isinstance(source.get('diagnostics'), list) else []
    rows = source.get('tasks')
    fallback_reason = None
    if strict_work_packages and isinstance(rows, list) and rows:
        try:
            rows = _ordered_structured_tasks(rows)
        except ValueError as error:
            fallback_reason = str(error)
            rows = None
    if not isinstance(rows, list) or not rows:
        text = _fallback_report(plan, fallback_objective)
        rows = [{'id': 'execute-plan', 'objective': text or
                 'Carry out the available migration plan and report the changes and remaining issues.'}]
        diagnostics.append({'code': 'task_settings_unavailable',
                            'fallback': 'single_task',
                            'detail': ('Explicit task structure is unsafe: ' + fallback_reason
                                       if fallback_reason else
                                       'The available report is passed directly to one execution task.')})

    tasks, aliases, occupied = [], {}, set()
    for index, row in enumerate(rows):
        row = dict(row) if isinstance(row, Mapping) else {'objective': str(row)}
        original = str(row.get('id') or 'task-' + str(index + 1))
        if strict_work_packages and fallback_reason is None:
            identifier = original
        else:
            identifier = re.sub('[^A-Za-z0-9_-]+', '-', original).strip('-_')[:64] or 'task'
            if not identifier[0].isalnum():
                identifier = 'task-' + identifier
            candidate, suffix = identifier, 1
            while candidate in occupied:
                suffix += 1
                candidate = identifier + '-' + str(suffix)
            identifier = candidate
        occupied.add(identifier)
        aliases.setdefault(original, identifier)
        if identifier != original:
            diagnostics.append({'code': 'task_id_normalized', 'source_id': original, 'task_id': identifier})
        paths = []
        for path in _strings(row.get('owned_paths')):
            normalized = _path(path)
            if normalized is None:
                diagnostics.append({'code': 'nonproject_path_omitted', 'task_id': identifier, 'path': path})
            elif normalized not in paths:
                paths.append(normalized)
        objective = row.get('objective')
        if not isinstance(objective, str) or not objective.strip():
            objective = fallback_objective or 'Implement the work described by task ' + original + '.'
        checks = row.get('validation_checks', row.get('checks'))
        complexity = row.get('complexity')
        model = row.get('model')
        reasoning = row.get('reasoning_effort')
        tasks.append({**row, 'id': identifier, 'objective': objective,
                      'owned_paths': paths, 'dependencies': _strings(row.get('dependencies')),
                      'acceptance': _strings(row.get('acceptance')),
                      'validation_checks': checks if isinstance(checks, list) else [],
                      'blocked_by_gaps': _strings(row.get('blocked_by_gaps')),
                      'complexity': complexity if isinstance(complexity, str) and complexity in {'simple', 'complex'} else 'complex',
                      'model': model if isinstance(model, str) and model.strip() else model_default,
                      'reasoning_effort': reasoning if isinstance(reasoning, str) and reasoning.strip() else reasoning_default})

    for task in tasks:
        if strict_work_packages and fallback_reason is None:
            # The v22 structure was validated before normalization; retain the
            # dependency list exactly, including its declared ordering.
            continue
        dependencies = []
        for dependency in task['dependencies']:
            resolved = aliases.get(dependency, dependency)
            if resolved in occupied and resolved != task['id']:
                if resolved not in dependencies:
                    dependencies.append(resolved)
            else:
                diagnostics.append({'code': 'unavailable_dependency', 'task_id': task['id'],
                                    'dependency': dependency})
        task['dependencies'] = dependencies

    ordered, done = [], set()
    pending = list(tasks)
    while pending:
        ready = [task for task in pending if set(task['dependencies']) <= done]
        if not ready:
            task = pending[0]
            removed = [dependency for dependency in task['dependencies'] if dependency not in done]
            task['dependencies'] = [dependency for dependency in task['dependencies'] if dependency in done]
            diagnostics.append({'code': 'dependency_cycle_ordered', 'task_id': task['id'],
                                'unresolved_dependencies': removed})
            ready = [task]
        for task in ready:
            ordered.append(task)
            done.add(task['id'])
            pending.remove(task)

    # Historical plans infer serialization for overlapping paths. In v22,
    # dependency edges are explicit contract data and are never synthesized.
    for index, task in enumerate(ordered):
        for previous in ordered[:index]:
            overlap = any(a == b or a.startswith(b + '/') or b.startswith(a + '/')
                          for a in task['owned_paths'] for b in previous['owned_paths'])
            if (overlap and strict_work_packages and fallback_reason is None
                    and previous['id'] not in task['dependencies']):
                diagnostics.append({'code': 'overlapping_paths_without_dependency',
                                    'task_id': task['id'], 'other_task_id': previous['id']})
            elif (overlap and not (strict_work_packages and fallback_reason is None)
                  and previous['id'] not in task['dependencies']):
                task['dependencies'].append(previous['id'])
    shared = [path for value in _strings(source.get('shared_paths')) if (path := _path(value))]
    return {'schema_version': 1, 'base_commit': base_commit or source.get('base_commit'),
            'shared_paths': list(dict.fromkeys(shared)), 'tasks': ordered,
            **{key: source[key] for key in ('issue_coverage', 'unresolved_issues') if key in source},
            'diagnostics': diagnostics}


def retain_inventory_issues(plan, facts):
    """Keep host issues visible when the author omits them, without replanning.

    A task's issue_ids are claims of intended work, never evidence of resolution.
    No task, dependency, approval or automatic retry is created here.
    """
    inventory = facts.get('migration_inventory', {}) if isinstance(facts, Mapping) else {}
    rows = inventory.get('issues', []) if isinstance(inventory, Mapping) else []
    issues = {row['issue_id']: row for row in rows
              if isinstance(row, Mapping) and isinstance(row.get('issue_id'), str)}
    tasks = plan.get('tasks', [])
    claims = {identifier for task in tasks for identifier in _strings(task.get('issue_ids'))}
    unresolved = plan.get('unresolved_issues', [])
    unresolved = list(unresolved) if isinstance(unresolved, list) else []
    declared = {row if isinstance(row, str) else row.get('issue_id')
                for row in unresolved if isinstance(row, (str, Mapping))
                and (isinstance(row, str) or isinstance(row.get('issue_id'), str))}
    omitted = sorted(set(issues) - claims - declared)
    unresolved.extend({'issue_id': identifier, 'reason': 'omitted_by_planner',
                       'host_issue': issues[identifier]} for identifier in omitted)
    plan['unresolved_issues'] = unresolved
    plan['issue_coverage'] = {
        'inventory_issue_ids': sorted(issues), 'claimed_issue_ids': sorted(claims & issues.keys()),
        'omitted_issue_ids': omitted, 'unknown_claim_ids': sorted(claims - issues.keys()),
        'resolution_verified': False,
    }
    if omitted:
        plan['diagnostics'].append({'code': 'inventory_issues_omitted_by_planner', 'issue_ids': omitted})
        if tasks:
            # Existing task only: this is unresolved context, not an invented
            # assertion that the author accepted responsibility or fixed it.
            tasks[0]['unresolved_inventory_issues'] = [issues[key] for key in omitted]
            tasks[0]['objective'] += ('\nHost note: unresolved_inventory_issues contains facts '
                                     'omitted by the plan. Account for them or explicitly report '
                                     'them unresolved; this note does not certify any fix.')
    return plan
