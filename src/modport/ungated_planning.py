"""Planning reports and task dispatch without business approval checks."""

from collections.abc import Mapping
from .workspace import project_path, is_project_workspace
from dataclasses import replace
import json
from pathlib import Path
import re

from .contracts import json_copy
from .execution_plan import decode_report, normalize_execution_plan


_SHA256 = re.compile(r'[0-9a-f]{64}')


def _strict_path(root, ref):
    """Resolve one v19 planning input only when its bytes match its digest."""
    from .evidence import file_digest, verified_path
    if not isinstance(ref, Mapping):
        raise ValueError('artifact reference is not an object')
    expected = ref.get('sha256')
    if not isinstance(expected, str) or _SHA256.fullmatch(expected) is None:
        raise ValueError('artifact reference has no valid sha256')
    path = verified_path(root, ref)
    if file_digest(path) != expected:
        raise ValueError('artifact digest mismatch')
    return path


def _strict_ref_map(root, refs, unavailable, context):
    trusted = {}
    if not isinstance(refs, Mapping):
        unavailable[context] = 'artifact_refs is not an object'
        return trusted
    for name, ref in refs.items():
        label = context + ':' + str(name)
        try:
            _strict_path(root, ref)
        except (OSError, ValueError, TypeError, KeyError) as error:
            unavailable[label] = str(error)
        else:
            trusted[name] = json_copy(ref)
    return trusted


def _strict_tree(root, value, unavailable, context='upstream_results'):
    """Remove invalid nested artifact maps before a v19 model sees a command."""
    if isinstance(value, list):
        return [_strict_tree(root, item, unavailable, context) for item in value]
    if not isinstance(value, Mapping):
        return value
    result = {}
    trusted_paths = None
    if 'artifact_refs' in value:
        refs = _strict_ref_map(root, value.get('artifact_refs'), unavailable,
                               context + '.artifact_refs')
        result['artifact_refs'] = refs
        trusted_paths = {ref.get('path') for ref in refs.values()
                         if isinstance(ref, Mapping)}
    for key, item in value.items():
        if key == 'artifact_refs':
            continue
        if (isinstance(key, str) and key.endswith('_refs')
                and key != 'unavailable_artifact_refs'
                and isinstance(item, Mapping)):
            result[key] = _strict_ref_map(root, item, unavailable,
                                          context + '.' + key)
            continue
        if (isinstance(key, str) and key.endswith('_ref')
                and isinstance(item, Mapping)):
            try:
                _strict_path(root, item)
            except (OSError, ValueError, TypeError, KeyError) as error:
                unavailable[context + '.' + key] = str(error)
                result[key] = {'available': False, 'diagnostic': str(error)}
            else:
                result[key] = json_copy(item)
            continue
        if key == 'last_message' and trusted_paths is not None:
            raw = str(item) if isinstance(item, str) else None
            relative = raw
            if raw and Path(raw).is_absolute():
                try:
                    relative = Path(raw).relative_to(root).as_posix()
                except ValueError:
                    relative = None
            if relative not in trusted_paths:
                unavailable[context + '.last_message'] = 'last_message has no trusted artifact reference'
                continue
        result[key] = _strict_tree(root, item, unavailable, context + '.' + str(key))
    return result


def _strict_command(command):
    """Return the v19 model view and digest failures without changing legacy input."""
    if command.options.get('workflow_version', 0) < 19:
        return command, {}
    root = Path(command.run_dir)
    unavailable = {}
    refs = _strict_ref_map(root, command.artifact_refs, unavailable, 'artifact_refs')
    upstream = _strict_tree(root, command.upstream_results, unavailable)
    payload = _strict_tree(root, command.payload, unavailable, 'payload')
    options = _strict_tree(root, command.options, unavailable, 'options')
    findings = tuple(_strict_tree(root, list(command.prior_findings), unavailable,
                                  'prior_findings'))
    if unavailable:
        payload = {**payload, 'unavailable_artifact_refs': {
            **(payload.get('unavailable_artifact_refs', {})
               if isinstance(payload.get('unavailable_artifact_refs'), Mapping) else {}),
            **{name: {'available': False, 'diagnostic': detail}
               for name, detail in unavailable.items()}}}
    return replace(command, artifact_refs=refs, upstream_results=upstream,
                   payload=payload, options=options, prior_findings=findings), unavailable


def _drop_artifact_alias(value, alias):
    if isinstance(value, list):
        return [_drop_artifact_alias(item, alias) for item in value]
    if not isinstance(value, Mapping):
        return value
    result = {}
    for key, item in value.items():
        if key == 'artifact_refs' and isinstance(item, Mapping):
            result[key] = {name: json_copy(ref) for name, ref in item.items()
                           if name != alias}
        else:
            result[key] = _drop_artifact_alias(item, alias)
    return result


def _without_artifact_alias(command, alias):
    return replace(command,
        artifact_refs={name: json_copy(ref) for name, ref in command.artifact_refs.items()
                       if name != alias},
        upstream_results=_drop_artifact_alias(command.upstream_results, alias),
        payload=_drop_artifact_alias(command.payload, alias),
        prior_findings=tuple(_drop_artifact_alias(list(command.prior_findings), alias)))


def _bind_inventory_candidate(command, base):
    """Bind authenticated inventory facts to the exact candidate sent to v19 planning."""
    if command.options.get('workflow_version', 0) < 19 or command.stage_id != 'migration_plan':
        return command, []
    ref = command.artifact_refs.get('migration_inventory')
    if not isinstance(ref, Mapping):
        return command, [{'code': 'deterministic_planning_input_unavailable',
                          'artifact': 'migration_inventory',
                          'detail': 'trusted inventory reference unavailable'}]
    try:
        inventory = json.loads(_strict_path(Path(command.run_dir), ref).read_text(encoding='utf-8'))
    except (OSError, ValueError, TypeError, KeyError) as error:
        return _without_artifact_alias(command, 'migration_inventory'), [{
            'code': 'deterministic_planning_input_unavailable',
            'artifact': 'migration_inventory', 'detail': str(error)}]
    candidate = inventory.get('candidate_id') if isinstance(inventory, Mapping) else None
    if not isinstance(candidate, str) or not candidate:
        return _without_artifact_alias(command, 'migration_inventory'), [{
            'code': 'deterministic_planning_input_unavailable',
            'artifact': 'migration_inventory',
            'detail': 'inventory candidate_id unavailable'}]
    if candidate != base:
        raise ValueError('inventory candidate_id differs from planning candidate')
    return command, []


def _workspace(command):
    relative = command.options.get('workspace') or (
        'baseline' if command.stage_id.startswith('contract_')
        or command.payload.get('goal_scope') == 'contract' else 'worktree')
    root = Path(command.run_dir)
    path = project_path(root, relative)
    if path.resolve() != path.absolute() or not is_project_workspace(root, path):
        raise ValueError('planning workspace must be contained without symlinks')
    return path


def _text(root, path):
    path = Path(path)
    if not path.is_absolute():
        path = root / path
    if path.resolve() != path.absolute() or not is_project_workspace(root, path):
        raise ValueError('planning report must be contained without symlinks')
    return path.read_text(encoding='utf-8')


def _base(command):
    from .development import _head
    try:
        return _head(command, _workspace(command))
    except (OSError, ValueError):
        return command.payload.get('development_base')


def _report_ref_text(command, ref):
    root = Path(command.run_dir)
    if command.options.get('workflow_version', 0) >= 19:
        return _strict_path(root, ref).read_text(encoding='utf-8')
    from .evidence import verified_path
    return verified_path(root, ref).read_text(encoding='utf-8')


def _message_text(command, outputs, value):
    root = Path(command.run_dir)
    if command.options.get('workflow_version', 0) < 19:
        return _text(root, value)
    refs = outputs.get('artifact_refs') if isinstance(outputs, Mapping) else None
    if not isinstance(refs, Mapping):
        raise ValueError('planning message has no artifact reference')
    path = Path(value)
    relative = (path.relative_to(root).as_posix() if path.is_absolute()
                and path.is_relative_to(root) else path.as_posix())
    ref = next((item for item in refs.values() if isinstance(item, Mapping)
                and item.get('path') == relative), None)
    if ref is None:
        raise ValueError('planning message has no matching artifact reference')
    return _strict_path(root, ref).read_text(encoding='utf-8')


def _reports(command):
    """Read available reports, including failed authors' original output."""
    root = Path(command.run_dir)
    refs = command.artifact_refs
    scope = command.payload.get('goal_scope') or (
        'contract' if command.stage_id.startswith('contract_') else
        'target' if command.stage_id.startswith('target_') else 'migration')
    task_stage = 'migration_tasks' if scope == 'migration' else scope + '_repair_tasks'
    aliases = [task_stage, 'development_plan', 'current_plan']
    seen = set()
    # A failed current task author may have produced a newer plan than the
    # inherited development_plan reference from an earlier execution.
    results = list(command.upstream_results.values())
    for result in reversed(results):
        if not isinstance(result, Mapping) or result.get('stage_id') != task_stage:
            continue
        outputs = result.get('outputs')
        if not isinstance(outputs, Mapping):
            continue
        raw = outputs.get('raw_report')
        if isinstance(raw, str) and raw:
            yield task_stage, raw
        message = outputs.get('last_message')
        if isinstance(message, str) and message:
            seen.add(message)
            try:
                yield task_stage, _message_text(command, outputs, message)
            except (OSError, ValueError, TypeError):
                continue
    for alias in aliases:
        ref = refs.get(alias)
        if not isinstance(ref, Mapping) or ref.get('path') in seen:
            continue
        seen.add(ref.get('path'))
        try:
            yield alias, _report_ref_text(command, ref)
        except (OSError, ValueError, TypeError, KeyError):
            continue
    for result in reversed(results):
        if not isinstance(result, Mapping):
            continue
        outputs = result.get('outputs') or {}
        if not isinstance(outputs, Mapping):
            continue
        for name in ('raw_report', 'last_message'):
            value = outputs.get(name)
            if not isinstance(value, str) or not value:
                continue
            if name == 'raw_report':
                yield result.get('stage_id', ''), value
            elif value not in seen:
                seen.add(value)
                try:
                    yield result.get('stage_id', ''), _message_text(command, outputs, value)
                except (OSError, ValueError, TypeError):
                    continue


def _deterministic_planning_facts(command):
    """Return the complete authenticated v19 inventory and scan as one source."""
    if (command.options.get('workflow_version', 0) < 19
            or command.stage_id != 'migration_plan'):
        return None, []
    root = Path(command.run_dir)
    documents = {}
    diagnostics = []
    for alias in ('migration_inventory', 'mod_scan_report'):
        ref = command.artifact_refs.get(alias)
        if not isinstance(ref, Mapping):
            diagnostics.append({'code': 'deterministic_planning_input_unavailable',
                                'artifact': alias, 'detail': 'trusted reference unavailable'})
            continue
        try:
            document = json.loads(_strict_path(root, ref).read_text(encoding='utf-8'))
        except (OSError, ValueError, TypeError, KeyError) as error:
            diagnostics.append({'code': 'deterministic_planning_input_unavailable',
                                'artifact': alias, 'detail': str(error)})
            continue
        if not isinstance(document, Mapping):
            diagnostics.append({'code': 'deterministic_planning_input_unavailable',
                                'artifact': alias, 'detail': 'document is not an object'})
            continue
        nested_unavailable = {}
        documents[alias] = _strict_tree(root, document, nested_unavailable,
                                        'deterministic.' + alias)
        diagnostics.extend({'code': 'deterministic_planning_input_unavailable',
                            'artifact': name, 'detail': detail}
                           for name, detail in nested_unavailable.items())
    if not documents:
        return None, diagnostics
    facts = {
        'schema_version': 1,
        'kind': 'deterministic_migration_planning_facts',
        'instructions': ('Preserve every inventory issue, scan finding, known gap, location and '
                         'diagnostic below in the implementation plan or an explicit unresolved task.'),
        **documents,
    }
    return json.dumps(facts, ensure_ascii=False, sort_keys=True), diagnostics


def available_execution_plan(command):
    """Use the most recent usable task settings or the available prose."""
    if command.options.get('workflow_version', 0) >= 19:
        ref = command.artifact_refs.get('development_plan')
        if isinstance(ref, Mapping):
            document = json.loads(_strict_path(Path(command.run_dir), ref).read_text(encoding='utf-8'))
            report = decode_report(document)
            candidate = report.get('development_plan', report) if isinstance(report, Mapping) else report
            if not isinstance(candidate, Mapping) or not isinstance(candidate.get('tasks'), list):
                raise ValueError('authenticated development plan has no tasks array')
            return normalize_execution_plan(candidate, base_commit=_base(command),
                                            workflow_version=command.options.get('workflow_version'),
                                            model_policy=command.options.get('model_policy'))
    reports = list(_reports(command))
    base = _base(command)
    for alias, text in reports:
        report = decode_report(text)
        if isinstance(report, Mapping):
            candidate = report.get('development_plan', report)
            if isinstance(candidate, Mapping) and isinstance(candidate.get('tasks'), list) and candidate['tasks']:
                return normalize_execution_plan(candidate, base_commit=base,
                                                workflow_version=command.options.get('workflow_version'),
                                                model_policy=command.options.get('model_policy'))
    report = next((text for alias, text in reports if alias == 'current_plan'),
                  reports[0][1] if reports else '')
    return normalize_execution_plan({}, base_commit=base, fallback_objective=report or None,
                                    workflow_version=command.options.get('workflow_version'),
                                    model_policy=command.options.get('model_policy'))


class UngatedPlanningHandler:
    def __call__(self, command):
        from . import handlers
        from .development import _artifact
        from .planning import STAGES, REPAIRS

        stage = command.stage_id
        scope = 'contract' if stage.startswith('contract_') else 'target' if stage.startswith('target_') else 'migration'
        chain = STAGES if scope == 'migration' else REPAIRS[scope]
        index = chain.index(stage)
        single_plan = command.options.get('workflow_version', 0) >= 19 and stage == 'migration_plan'
        if single_plan:
            index = 2
        root = Path(command.run_dir)
        model_command, unavailable = _strict_command(command)
        planning_base = _base(model_command)
        if single_plan and (not isinstance(planning_base, str) or not planning_base):
            return handlers._result(command, 'failed', error_code='candidate_identity_mismatch',
                detail='planning candidate identity is unavailable', outputs={
                    'product_state': 'unavailable', 'acceptance_status': 'unverified'})
        candidate_diagnostics = []
        if single_plan:
            try:
                model_command, candidate_diagnostics = _bind_inventory_candidate(
                    model_command, planning_base)
            except (OSError, ValueError, TypeError, KeyError) as error:
                return handlers._result(command, 'failed', error_code='candidate_identity_mismatch',
                    detail=str(error), outputs={'product_state': 'unavailable',
                        'acceptance_status': 'unverified'})
        for diagnostic in candidate_diagnostics:
            unavailable.setdefault('artifact_refs:migration_inventory', diagnostic['detail'])
        existing = list(_reports(model_command))
        deterministic_facts, fact_diagnostics = _deterministic_planning_facts(model_command)
        fact_diagnostics = candidate_diagnostics + [row for row in fact_diagnostics
            if not any(row.get('artifact') == existing_row.get('artifact')
                       and row.get('detail') == existing_row.get('detail')
                       for existing_row in candidate_diagnostics)]
        if deterministic_facts is not None:
            existing.insert(0, ('deterministic_inventory_and_scan', deterministic_facts))
        input_index = {'artifact_refs': {
            name: {key: ref[key] for key in ('path', 'sha256', 'media_type') if key in ref}
            for name, ref in model_command.artifact_refs.items() if isinstance(ref, Mapping)},
            'upstream_results': [
                {key: result.get(key) for key in ('stage_id', 'command_id', 'status', 'error_code', 'detail')}
                for result in model_command.upstream_results.values() if isinstance(result, Mapping)]}
        if unavailable:
            input_index['unavailable_artifact_refs'] = {
                name: {'available': False, 'diagnostic': detail}
                for name, detail in unavailable.items()}
        if existing:
            input_index['primary_report'] = _artifact(model_command, 'planning-input-report.txt',
                existing[0][1].encode('utf-8'), {'source_alias': existing[0][0]})
        index_ref = _artifact(model_command, 'planning-input-index.json',
            json.dumps(input_index, ensure_ascii=False).encode('utf-8'))
        instructions = (
            'Explain the concrete work from the available source, reports and failure diagnostics. '
            'Write the initial plan in ordinary text.',
            'Improve the available plan once. Keep useful work, add missing details and explain uncertainties. '
            'Write the complete resulting plan in ordinary text.',
            'Organize the available plan into independently executable tasks. '
            'A JSON object with development_plan.tasks is useful: each task can describe id, objective, '
            'owned_paths, dependencies, acceptance and optional validation_checks. Project files and build files are '
            'allowed in every repair scope. Missing fields, report format, reviews and test outcomes do not '
            'prevent execution. Describe later work and unresolved questions without inventing approval.',
            'Explain the task assignments and their dependencies for the next coders. '
            'Use the available work even if a previous report was rejected or incomplete. '
            'Your report is context for execution, not permission to execute.',
        )
        prompt = instructions[index] + (
            '\nBusiness gates are disabled. Report failures and uncertainty honestly. '
            'Do not repeat a stage merely to satisfy a schema, approval or test gate. '
            'Read the supplied original reports as evidence. Do not execute project code or edit project files.\n'
            'Read the available report index, then its primary_report and relevant referenced files: '
            + str(root / index_ref['path']) + ' (sha256 ' + index_ref['sha256'] + '). '
            'Inspect fields and reports selectively; do not dump the entire historical input or metadata. '
            'Preserve original evidence references in your findings.')
        if single_plan:
            prompt = ('Produce the one implementation plan from the deterministic inventory, '
                      'codemod result and early compilation evidence. Group remaining semantic work '
                      'by shared interfaces and dependencies. Do not repeat already applied mechanical '
                      'changes. Include development_plan.tasks in your report when possible; '
                      'unstructured output remains diagnostic input rather than an approval gate.\n' + prompt)
        if command.options.get('workflow_version', 0) >= 23:
            prompt += (
                '\nMake the implementation decisions concrete: for each file or related issue group, '
                'identify the source path and class/method (line or diagnostic when available), '
                'explain the problem, specify the intended code/API change and affected callers, '
                'and state how its behavior will be verified. Check proposed APIs against the locked '
                'target sources; label unresolved alternatives and the evidence needed to choose. '
                'Group related inventory issue_ids into tasks and retain unmapped issues with reasons; '
                'a directory glob alone does not explain how a compiler error will be fixed. '
                'Order provider/interface changes before callers. Do not copy the whole inventory '
                'or report history into each objective; cite the authenticated input references.')
            if index == 2:
                prompt += (
                    '\nFor the host task handoff, include exactly one JSON code block in your Markdown '
                    'with development_plan.tasks. Give each task a stable id, a concrete objective, '
                    'owned_paths, dependencies (task IDs), issue_ids, acceptance and optional '
                    'validation_checks. Put remaining issues and reasons in '
                    'development_plan.unresolved_issues. These fields communicate the work; '
                    'missing coverage or a rejected review is diagnostic and does not require approval '
                    'or another planning round.')
        result = handlers.CodexStageHandler(prompt, baseline=scope == 'contract', read_only=True,
                                            reuse_recovery_prompt=False)(model_command)
        if single_plan and _base(model_command) != planning_base:
            outputs = dict(result.outputs)
            diagnostics = (list(outputs.get('diagnostics', []))
                           if isinstance(outputs.get('diagnostics'), list) else [])
            diagnostics.append({'code': 'candidate_identity_mismatch',
                                'detail': 'planning candidate changed during model execution'})
            outputs['diagnostics'] = diagnostics
            outputs['acceptance_status'] = 'unverified'
            return replace(result, status='failed', error_code='candidate_identity_mismatch',
                           detail='planning candidate changed during model execution',
                           outputs=outputs)
        if result.error_code == 'opencode_cleanup_unconfirmed':
            # A completed message may be present in the execution log, but its
            # producer has not been confirmed stopped. Keep the diagnostic
            # evidence without promoting that message into a dispatchable plan.
            outputs = dict(result.outputs)
            refs = dict(outputs.get('artifact_refs', {}))
            refs['planning_input_index'] = index_ref
            if existing:
                refs['planning_input_report'] = input_index['primary_report']
            outputs['artifact_refs'] = refs
            outputs['acceptance_status'] = 'unverified'
            return replace(result, outputs=outputs)
        outputs = dict(result.outputs)
        refs = dict(outputs.get('artifact_refs', {}))
        refs['planning_input_index'] = index_ref
        if existing:
            refs['planning_input_report'] = input_index['primary_report']
        raw = outputs.get('raw_report', '')
        if not isinstance(raw, str):
            raw = ''
        message = outputs.get('last_message')
        if isinstance(message, str):
            try:
                raw = _message_text(model_command, outputs, message)
            except (OSError, ValueError, TypeError):
                pass
        diagnostics = list(outputs.get('diagnostics', [])) if isinstance(outputs.get('diagnostics'), list) else []
        diagnostics.extend({'code': 'planning_artifact_unavailable', 'artifact': name,
                            'detail': detail} for name, detail in unavailable.items())
        diagnostics.extend(fact_diagnostics)
        if result.status != 'completed':
            diagnostics.append({'stage': stage, 'status': result.status,
                                'error_code': result.error_code, 'detail': result.detail})
        if not raw.strip():
            diagnostics.append({'code': 'planning_report_empty', 'stage': stage})
        base = planning_base if single_plan else _base(model_command)
        raw_ref = _artifact(model_command, 'planning-report.txt', raw.encode('utf-8'))
        refs['planning_report:' + stage] = raw_ref

        if index < 2:
            revision = index + 1
            ref = _artifact(model_command, 'planning-plan.md', raw.encode('utf-8'), {
                'document_kind': 'modport-planning-markdown-v1', 'run_id': model_command.run_id,
                'scope': scope, 'revision': revision, 'base_commit': base,
                'status': 'ready' if index == 1 else 'continue',
                'parent_sha256': model_command.artifact_refs.get('current_plan', {}).get('sha256'),
            })
            refs.update({stage: ref, 'current_plan': ref})
            outputs.update(plan_revision=revision, plan_status='ready' if index == 1 else 'continue')
        else:
            parsed = decode_report(raw)
            fallback = raw or (existing[0][1] if existing else None)
            if deterministic_facts is not None:
                if command.options.get('workflow_version', 0) >= 23:
                    fallback = ('Planner report (possibly empty or incomplete):\n' + raw
                                + '\nRead authenticated inventory and scan evidence through '
                                'planning_input_index: ' + json.dumps(index_ref, ensure_ascii=False))
                else:
                    fallback = ('Planner report (possibly empty or incomplete):\n' + raw
                                + '\n\nAuthenticated deterministic inventory and scan facts:\n'
                                + deterministic_facts)
            plan = (normalize_execution_plan(parsed, base_commit=base,
                        fallback_objective=fallback,
                        workflow_version=model_command.options.get('workflow_version'),
                        model_policy=model_command.options.get('model_policy'))
                    if index == 2 else available_execution_plan(model_command))
            if command.options.get('workflow_version', 0) >= 20 and deterministic_facts is not None:
                from .execution_plan import retain_inventory_issues
                plan = retain_inventory_issues(plan, json.loads(deterministic_facts))
            diagnostics.extend(plan.get('diagnostics', []))
            current = model_command.artifact_refs.get('current_plan')
            document = {'schema_version': 1, 'run_id': model_command.run_id, 'stage': stage,
                        'producer_execution_id': model_command.command_id, 'base_commit': base,
                        'parallel_decision': 'parallel', 'development_plan': plan,
                        'raw_report_ref': raw_ref, 'diagnostics': diagnostics,
                        'report_format': 'execution_review', 'source_plan_ref': current,
                        'source_plan_sha256': current.get('sha256') if isinstance(current, dict) else None}
            deferred = parsed.get('deferred_obligations', []) if isinstance(parsed, Mapping) else []
            document['deferred_obligations'] = deferred if isinstance(deferred, list) else []
            stage_ref = _artifact(model_command, stage + '.json', json.dumps(document, ensure_ascii=False).encode())
            development_ref = _artifact(model_command, 'development-plan.json', json.dumps(plan, ensure_ascii=False).encode(),
                                        {'development_base': base})
            refs.update({stage: stage_ref, 'development_plan': development_ref})
            if current:
                refs['current_plan'] = json_copy(current)
            outputs.update(parallel_decision='parallel', development_tasks=plan['tasks'],
                           development_base=base, deferred_obligations=document['deferred_obligations'])
        outputs.update(artifact_refs=refs, diagnostics=diagnostics, business_gates='disabled')
        return replace(result, outputs=outputs)
