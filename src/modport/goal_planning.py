"""Read-only coder context preparation after the frozen dispatch plan."""
from __future__ import annotations

from hashlib import sha256
import json
from pathlib import Path, PurePosixPath
from typing import Any, Mapping

from .contracts import OperationInput, json_copy
from .author_contracts import acceptance_report_prompt
from .planning_schema import (GOAL_SCHEMA, PlanningValidationError, validate_shape, check_schema_prompt)
from .business_policy import business_gates_disabled

CHECK_TYPES = frozenset({'file_exists', 'json_valid', 'python_syntax', 'contract_schema', 'gradle_tasks', 'gradle_regression'})
LEGACY_GOAL_PROMPT = ('Prepare context for the coder assigned the frozen task below. Read the final '
                      'authenticated Markdown plan, task synthesis, dispatch record, and frozen '
                      'development plan. Explain the concrete work, evidence, relevant files, '
                      'uncertainties, and checks that help the coder solve it in its own OpenCode '
                      'instance. Return free-form text, not a JSON object. Do not echo identities, '
                      'references, hashes, check definitions or frozen fields. The host already owns '
                      'the execution settings. Do not execute project code or change files. Preserve '
                      'all behavior requirements and distinguish pending later work from this task. '
                      'Do not reopen planning or broaden this task.')
GOAL_PROMPT = (
    "Prepare context for the coder assigned the frozen task below. Read the authenticated "
    "planning references supplied with this task, including the original planner report, normalized "
    "development plan and inventory or scan evidence when those references are present. Do not assume "
    "that separate task-synthesis or dispatch reports exist. Explain the concrete work, source evidence, "
    "relevant files, unresolved issue mappings and checks that help the coder solve this task in its own "
    "OpenCode session. If a needed reference is unavailable or a source issue is not assigned, state that "
    "uncertainty. Return free-form text, not a JSON object. Do not echo identities, hashes, check definitions "
    "or frozen fields. The host already owns the execution settings. Do not execute project code or change "
    "files. Preserve all behavior requirements and distinguish pending later work from this task. Do not "
    "reopen planning or broaden this task.")


def relative_path(value: Any) -> str:
    if not isinstance(value, str) or not value or '\\' in value or '\x00' in value:
        raise ValueError('goal path must be a nonempty relative POSIX path')
    path = PurePosixPath(value)
    if path.is_absolute() or any(p in {'', '.', '..'} for p in value.split('/')):
        raise ValueError('unsafe goal path')
    return value


def owned_path(value: Any, owned: list) -> str:
    path = relative_path(value)
    if not any(path == p or path.startswith(p + '/') for p in owned):
        raise ValueError('goal check/report path is outside task ownership')
    return path


def _advisory_goal(value, development_task, planning_context):
    """Build the runnable v17 goal while retaining checks as observations."""
    supplied = json_copy(dict(value)) if isinstance(value, Mapping) else {}
    task = dict(development_task) if isinstance(development_task, Mapping) else {}
    identifier = task.get('id', supplied.get('task_id', 'task'))
    objective = task.get('objective', supplied.get('objective', 'Complete the assigned task.'))
    owned = task.get('owned_paths', supplied.get('owned_paths', []))
    dependencies = task.get('dependencies', supplied.get('dependencies', []))
    acceptance = task.get('acceptance', supplied.get('acceptance', []))
    checks = task.get('validation_checks', supplied.get('checks', []))
    return {
        'task_id': identifier,
        'objective': objective if isinstance(objective, str) and objective.strip()
                     else 'Complete the assigned task.',
        'owned_paths': list(owned) if isinstance(owned, list) else [],
        'dependencies': list(dependencies) if isinstance(dependencies, list) else [],
        'acceptance': list(acceptance) if isinstance(acceptance, list) else [],
        'context_refs': json_copy(dict(planning_context)) if isinstance(planning_context, Mapping) else {},
        'stop_conditions': (list(supplied.get('stop_conditions', []))
                            if isinstance(supplied.get('stop_conditions'), list) else []),
        'acceptance_report': f'.modport/goal-reports/{identifier}.json',
        'checks': list(checks) if isinstance(checks, list) else [],
        'source_objective': task.get('objective'),
        **{key: json_copy(task[key]) for key in
           ('source_task_ids', 'source_objectives', 'source_acceptance',
            'validation_kind', 'structural_reason') if key in task},
    }


def validate_goal(value: Mapping, development_task: Mapping, planning_context: Mapping, *, require_double_check=False,
                  gates_disabled=False) -> dict:
    if gates_disabled:
        return _advisory_goal(value, development_task, planning_context)
    if not isinstance(value, Mapping):
        raise ValueError('goal must be an object')
    validate_shape(dict(value), GOAL_SCHEMA)
    goal = json_copy(dict(value))
    if goal.get('task_id') != development_task.get('id'):
        raise ValueError('goal task identity mismatch')
    for key in ('owned_paths', 'dependencies', 'acceptance'):
        if goal.get(key) != development_task.get(key):
            raise ValueError(f'goal changed frozen {key}')
    if goal.get('context_refs') != planning_context:
        raise ValueError('goal changed authenticated context references')
    if not isinstance(goal.get('objective'), str) or not goal['objective'].strip():
        raise ValueError('goal objective is missing')
    owned = goal['owned_paths']
    if not isinstance(owned, list) or not owned:
        raise ValueError('goal ownership is missing')
    for path in owned:
        relative_path(path)
    acceptance = goal['acceptance']
    if not isinstance(acceptance, list) or not acceptance or any(not isinstance(x, str) or not x.strip() for x in acceptance):
        raise ValueError('goal acceptance must be nonempty strings')
    if len(set(acceptance)) != len(acceptance):
        raise ValueError('goal acceptance must be unique')
    conditions = goal.get('stop_conditions')
    if not isinstance(conditions, list) or not conditions or any(not isinstance(x, str) or not x.strip() for x in conditions):
        raise ValueError('goal stop conditions are missing')
    report = relative_path(goal.get('acceptance_report'))
    if report != f".modport/goal-reports/{goal['task_id']}.json":
        owned_path(report, owned)
    checks = goal.get('checks')
    if not isinstance(checks, list) or not checks:
        raise ValueError('goal needs host-verifiable checks')
    identifiers, covered = set(), set()
    for check in checks:
        if not isinstance(check, dict) or check.get('type') not in CHECK_TYPES:
            raise ValueError('unsupported goal check')
        identifier = check.get('id')
        if not isinstance(identifier, str) or not identifier.strip() or identifier in identifiers:
            raise ValueError('goal check IDs must be unique nonempty strings')
        identifiers.add(identifier)
        criteria = check.get('acceptance')
        if not isinstance(criteria, list) or not criteria or any(x not in acceptance for x in criteria):
            raise ValueError('goal check maps unknown or missing acceptance')
        covered.update(criteria)
        allowed = {'id', 'type', 'acceptance', 'tasks' if check['type'] in {'gradle_tasks', 'gradle_regression'} else 'path'}
        if check['type'] == 'gradle_regression':
            allowed.add('reports')
        if set(check) != allowed:
            raise ValueError('unsupported or missing goal check fields')
        if check['type'] in {'gradle_tasks', 'gradle_regression'}:
            from .contract_inputs import validate_baseline_gradle_tasks
            validate_baseline_gradle_tasks(check['tasks'])
            if check['type'] == 'gradle_regression':
                if any(not task.startswith(':') for task in check['tasks']):
                    raise ValueError('regression tasks must be fully qualified direct Gradle Test task paths')
                reports = check['reports']
                if (not isinstance(reports, list) or not reports
                        or any(not isinstance(path, str) for path in reports)
                        or len(set(reports)) != len(reports)):
                    raise ValueError('regression checks require unique explicit JUnit reports')
                for path in reports:
                    relative_path(path)
                    parts = PurePosixPath(path).parts
                    if ('build' not in parts[:-1] or not path.endswith('.xml')
                            or any(character in path for character in '*?[]') or '.git' in parts):
                        raise ValueError('regression reports must be explicit XML files in build directories')
        else:
            owned_path(check['path'], owned)
            if check['path'] == goal['acceptance_report']:
                raise ValueError('acceptance report cannot verify itself')
    if covered != set(acceptance):
        raise ValueError('goal checks drop source acceptance')
    if 'validation_checks' in development_task and checks != development_task['validation_checks']:
        raise ValueError('goal changed frozen validation checks')
    if require_double_check:
        if 'validation_checks' not in development_task:
            raise ValueError('coder double-check requires frozen validation_checks')
        kind = development_task.get('validation_kind', 'regression')
        if kind not in {'structural', 'regression'}:
            raise ValueError('task validation_kind must be structural or regression')
        if 'validation_kind' in goal and goal['validation_kind'] != kind:
            raise ValueError('goal changed frozen validation_kind')
        goal['validation_kind'] = kind
        if kind == 'structural':
            reason = development_task.get('structural_reason')
            if not isinstance(reason, str) or not reason.strip():
                raise ValueError('structural task requires a reviewed structural_reason')
            if 'structural_reason' in goal and goal['structural_reason'] != reason:
                raise ValueError('goal changed frozen structural_reason')
            goal['structural_reason'] = reason
        else:
            regression_coverage = {criterion for check in checks if check['type'] == 'gradle_regression'
                                   for criterion in check['acceptance']}
            if regression_coverage != set(acceptance):
                raise ValueError('frozen gradle_regression checks must cover every acceptance criterion')
    if 'source_objective' in goal and goal['source_objective'] != development_task.get('objective'):
        raise ValueError('goal changed frozen source objective')
    goal['source_objective'] = development_task.get('objective')
    # Keep source provenance even when the model omits these redundant fields.
    for key in ('source_task_ids', 'source_objectives', 'source_acceptance'):
        if key in development_task:
            if key in goal and goal[key] != development_task[key]:
                raise ValueError(f'goal changed frozen {key}')
            goal[key] = json_copy(development_task[key])
    return goal


class GoalPreparationHandler:
    def __call__(self, command: OperationInput):
        from .handlers import CodexStageHandler, _result
        from .evidence import seal_ref, verified_path
        task = command.payload.get('development_task')
        context = command.payload.get('planning_context')
        scope = command.payload.get('goal_scope')
        generation = command.payload.get('goal_generation')
        advisory = business_gates_disabled(command)
        diagnostics = []
        try:
            if not isinstance(task, dict) or (not advisory and
                    (not isinstance(context, dict) or len(context) != 4)):
                raise ValueError('goal preparation needs a task and four planning references')
            if not isinstance(context, dict):
                context = {}
            if not advisory and command.options.get('workflow_version', 0) >= 15 and 'current_plan' not in context:
                raise ValueError('goal preparation needs the final Markdown plan reference')
            if scope not in {'migration', 'contract', 'target'}:
                if not advisory:
                    raise ValueError('invalid goal scope or generation')
                diagnostics.append(f'goal scope was unavailable or invalid: {scope!r}')
                scope = 'migration'
            if type(generation) is not int or generation < 0:
                if not advisory:
                    raise ValueError('invalid goal scope or generation')
                diagnostics.append(f'goal generation was unavailable or invalid: {generation!r}')
                generation = 0
            root = Path(command.run_dir)
            for name, ref in context.items():
                try:
                    verified_path(root, ref)
                except (OSError, ValueError, TypeError, KeyError) as exc:
                    if not advisory:
                        raise
                    diagnostics.append(f'{name}: {exc}')
        except (OSError, ValueError, TypeError, KeyError) as exc:
            return _result(command, 'failed', detail=str(exc), error_code='goal_input_invalid')
        goal_prompt = (GOAL_PROMPT if command.options.get('workflow_version', 0) >= 19
                       else LEGACY_GOAL_PROMPT)
        supervised = None
        if command.options.get('workflow_version', 0) >= 26:
            from .supervised_goals import apply_to_goal
            try:
                supervised = apply_to_goal(command, {'objective': task['objective']}, task)
            except (OSError, ValueError, KeyError, TypeError) as exc:
                return _result(command, 'failed', detail=str(exc), error_code='supervised_goal_invalid')
        prompt = (goal_prompt + ('' if advisory else acceptance_report_prompt())
                  + '\nFrozen development task: ' + json.dumps(task, ensure_ascii=False)
                  + '\nPlanning references: ' + json.dumps(context, ensure_ascii=False))
        if supervised and 'supervised_goal_revision' in command.artifact_refs:
            prompt += ('\nThe host bound a supervisor revision of this task objective. Prepare context '
                       'for this revised objective, preserving the original behavior requirements: '
                       + json.dumps(supervised, ensure_ascii=False))
        result = CodexStageHandler(prompt, baseline=scope == 'contract', read_only=True, reuse_recovery_prompt=False)(command)
        if result.status != 'completed' and not advisory:
            return result
        if advisory and isinstance(result.outputs.get('business_diagnostics'), list):
            diagnostics = [*result.outputs['business_diagnostics'], *diagnostics]
        try:
            report = ''
            message_value = result.outputs.get('last_message')
            if isinstance(message_value, str):
                message = Path(message_value)
                if message.is_absolute():
                    message = message.relative_to(root)
                from .goal_validation import contained_file
                report = contained_file(root, message.as_posix()).read_text(encoding='utf-8')
            elif advisory:
                diagnostics.append(result.detail or result.error_code or 'goal context agent produced no report')
            else:
                raise ValueError('goal context agent produced no report')
            # Launch/check parameters already belong to the reviewed task. The
            # context author never retypes them and its prose is not parsed.
            task_checks = task.get('validation_checks')
            if task_checks is None and not advisory:
                try:
                    parsed_report = json.loads(report)
                except (TypeError, ValueError):
                    parsed_report = {}
                task_checks = parsed_report.get('checks', []) if isinstance(parsed_report, dict) else []
            body = {'task_id': task['id'], 'objective': report,
                    'owned_paths': json_copy(task['owned_paths']),
                    'dependencies': json_copy(task['dependencies']),
                    'acceptance': json_copy(task['acceptance']),
                    'context_refs': json_copy(context),
                    'stop_conditions': ['Report unresolved blockers and preserve the assigned behavior requirements.'],
                    'acceptance_report': f".modport/goal-reports/{task['id']}.json",
                    'checks': json_copy(task_checks or []), 'raw_report': report}
            for key in ('validation_kind', 'structural_reason'):
                if key in task:
                    body[key] = task[key]
            # Validate host-owned executable settings, never the context report.
            body['objective'] = task['objective']
            goal = validate_goal(body, task, context,
                                 require_double_check=command.options.get('workflow_version', 0) >= 12,
                                 gates_disabled=advisory)
            goal['objective'] = task.get('objective', 'Complete the assigned task.')
            if supervised:
                goal.update(supervised)
            if report:
                goal['objective'] += '\n\nCoder context:\n' + report
            relative = f'artifacts/executions/{command.command_id}/coder-goal.json'
            target = root / relative
            if target.exists() or target.is_symlink() or target.parent.resolve() != target.parent.absolute():
                raise ValueError('goal artifact already exists or has unsafe parent')
            target.parent.mkdir(parents=True, exist_ok=True)
            with target.open('x', encoding='utf-8') as stream:
                json.dump(goal, stream, ensure_ascii=False, sort_keys=True, indent=2)
                stream.write('\n')
            ref = seal_ref(root, {'path': relative, 'sha256': sha256(target.read_bytes()).hexdigest(), 'media_type': 'application/json'}, execution_id=command.command_id)
        except (OSError, ValueError, TypeError, KeyError) as exc:
            outputs = dict(result.outputs)
            if isinstance(exc, PlanningValidationError):
                outputs.update(validation_error=exc.diagnostic,
                               validation_errors=getattr(exc, 'diagnostics', [exc.diagnostic]))
            return _result(command, 'failed', outputs=outputs, detail=str(exc), error_code='goal_output_invalid')
        revision = goal.get('supervised_goal_revision')
        return _result(command, 'completed', outputs={**result.outputs, 'goal': goal,
            'development_task_id': task['id'], 'business_diagnostics': diagnostics,
            'artifact_refs': {**result.outputs.get('artifact_refs', {}), 'coder_goal': ref,
                **({'supervised_goal_application': revision['receipt_ref']}
                   if isinstance(revision, dict) else {})}},
            detail='Coder context recorded with advisory execution settings' if advisory
                   else 'Coder context recorded with reviewed execution settings')
