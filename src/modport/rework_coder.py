"""Targeted coder rework from an interactive review.

The rework is a new SDK assignment.  It deliberately does not reopen the
already accepted native goal belonging to the original coder.  Instead it
freezes that coder's reviewed task onto the reviewer's current integrated
commit, runs a fresh isolated native goal, and integrates only the new delta.
"""
from __future__ import annotations

from dataclasses import replace
import json
import os
from pathlib import Path
import subprocess
from typing import Any, Mapping

from . import handlers
from .contracts import OperationInput, OperationResult, json_copy
from .business_policy import business_gates_disabled
from .development import (
    CoderHandler,
    _apply,
    _artifact,
    _check_ref,
    _head,
    _path,
    _verified,
    development_workspace,
    prepare_merge_workspace,
    validate_plan,
)
from .goal_planning import validate_goal
from .workspace import is_project_workspace, project_path, project_relative, workspace_spec


def rework_workspace(command: OperationInput) -> str:
    """Resolve the destination bound by the host to the requesting SDK author."""
    destination = command.payload.get('reviewer_workspace')
    caller_raw = command.payload.get('rework_caller_command')
    if caller_raw is None:
        if destination not in {'baseline', 'worktree'}:
            raise ValueError('coder rework requires a registered product or bound caller workspace')
        return destination
    caller = OperationInput.from_dict(caller_raw)
    if (caller.stage_id != 'coder' or caller.run_id != command.run_id
            or caller.command_id != command.payload.get('reviewer_execution_id')
            or Path(caller.run_dir).resolve() != Path(command.run_dir).resolve()):
        raise ValueError('coder rework caller does not match its SDK execution')
    task = caller.payload.get('development_task')
    generation = caller.payload.get('development_generation')
    if not isinstance(task, Mapping) or type(generation) is not int or generation < 1:
        raise ValueError('coder rework caller lacks its isolated task binding')
    expected = development_workspace(generation, _path(task.get('id')), caller.payload)
    if destination != expected or caller.options.get('workspace') != expected:
        raise ValueError('coder rework destination differs from its bound caller workspace')
    return expected


def _reviewer_scope(root: Path, reviewer_workspace: Path, *,
                    command: OperationInput | None = None) -> tuple[Path, str]:
    """Resolve only the exact host-registered review workspaces."""
    root = Path(root).resolve()
    requested = Path(reviewer_workspace)
    if requested.is_absolute():
        if requested.is_relative_to(root):
            logical = requested.relative_to(root)
        else:
            logical = project_relative(root, requested)
    else:
        logical = requested
    allowed = ({rework_workspace(command)} if command is not None
               and ('reviewer_workspace' in command.payload
                    or 'rework_caller_command' in command.payload)
               else {"baseline", "worktree"})
    if logical.as_posix() not in allowed:
        raise ValueError("reviewer workspace must be the exact host-registered rework destination")
    workspace = project_path(root, logical)
    if (workspace.is_symlink() or workspace.resolve() != workspace.absolute()
            or not workspace.is_dir() or not is_project_workspace(root, workspace)
            or project_relative(root, workspace).as_posix() != logical.as_posix()):
        raise ValueError("reviewer workspace is missing or outside its registered scope")
    if requested.is_absolute() and not requested.is_relative_to(root):
        if requested.absolute() != workspace.absolute():
            raise ValueError("external reviewer workspace must be the exact registered root")
    return workspace, logical.as_posix()


def _rollback_integration(command: OperationInput, workspace: Path, base: str) -> bool:
    """Reset isolated review workspaces; retain partial direct-folder edits."""
    root = Path(command.run_dir).resolve()
    try:
        specification = workspace_spec(root)
        direct_workspace = bool(
            specification and specification.get("mode") == "direct"
            and project_relative(root, workspace).as_posix() == "worktree")
    except (OSError, ValueError, TypeError):
        direct_workspace = True
    if direct_workspace:
        return False
    rollback = handlers._exec(
        ["git", "-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false",
         "reset", "--hard", base],
        cwd=workspace,
        log=root / "logs" / f"development-{command.command_id}.log",
        timeout=handlers._remaining_timeout(command, 120),
        env={"GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null",
             "GIT_TERMINAL_PROMPT": "0", "GIT_NO_REPLACE_OBJECTS": "1"},
    )
    if rollback.returncode:
        raise RuntimeError("coder rework integration rollback failed")
    return True


def _review_outputs(command: OperationInput, workspace: Path) -> tuple[list[str], dict[str, tuple[bytes, int]]]:
    raw = command.payload.get("reviewer_report_paths", [])
    if (not isinstance(raw, list) or any(not isinstance(value, str) for value in raw)
            or len(raw) != len(set(raw))):
        raise ValueError("reviewer_report_paths must be unique relative paths")
    paths = [_path(value, shared=True) for value in raw]
    retained: dict[str, tuple[bytes, int]] = {}
    # Validate every path before removing any of them.
    for relative in paths:
        target = workspace / relative
        if target.exists() or target.is_symlink():
            if (target.is_symlink() or not target.is_file()
                    or not target.resolve().is_relative_to(workspace.resolve())):
                raise ValueError("reviewer output must be a contained regular file: " + relative)
            if handlers._exec(
                ["git", "-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false",
                 "ls-files", "--error-unmatch", "--", relative],
                cwd=workspace,
                log=Path(command.run_dir) / "logs" / f"development-{command.command_id}.log",
                timeout=handlers._remaining_timeout(command, 120),
                env={"GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null",
                     "GIT_TERMINAL_PROMPT": "0", "GIT_NO_REPLACE_OBJECTS": "1"},
            ).returncode == 0:
                raise ValueError("reviewer output path must be untracked: " + relative)
            retained[relative] = (target.read_bytes(), target.stat().st_mode & 0o777)
    for relative in retained:
        (workspace / relative).unlink()
    return paths, retained


def _restore_review_outputs(workspace: Path, retained: Mapping[str, tuple[bytes, int]]) -> None:
    for relative, (data, mode) in retained.items():
        target = workspace / relative
        if target.exists() or target.is_symlink():
            raise ValueError("coder rework occupied reviewer output path: " + relative)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        os.chmod(target, mode)


def _context_refs(command: OperationInput, original: OperationInput, source_goal: Mapping[str, Any]) -> dict:
    raw = command.payload.get("rework_context_refs", source_goal.get("context_refs"))
    if business_gates_disabled(command):
        from .gate_policy import available_refs
        return available_refs(command.run_dir, dict(raw) if isinstance(raw, Mapping) else {})[0]
    if not isinstance(raw, Mapping) or len(raw) != 4:
        raise ValueError("coder rework needs four refreshed planning references")
    refs = json_copy(dict(raw))
    for name, ref in refs.items():
        if not isinstance(name, str) or not isinstance(ref, Mapping):
            raise ValueError("invalid coder rework planning reference")
        _verified(command, ref)
    return refs


def _request_context(command: OperationInput, original: OperationInput, instructions: str) -> tuple[dict, str, dict]:
    if not isinstance(instructions, str) or not instructions.strip() or len(instructions) > 100_000:
        raise ValueError("coder rework instructions must be nonempty")
    raw_report = command.payload.get("reviewer_report", "")
    tool_context = command.payload.get("review_tool_context", {})
    reviewer_execution_id = command.payload.get("reviewer_execution_id")
    if (not isinstance(raw_report, str) or len(raw_report) > 100_000
            or not isinstance(tool_context, Mapping)
            or not isinstance(reviewer_execution_id, str) or not reviewer_execution_id):
        raise ValueError("review report and tool context have invalid types")
    tool_context = json_copy(dict(tool_context))
    source_artifact_refs = {
        name: json_copy(dict(ref))
        for name in ("development_plan", "coder_goal", "supervised_goal_revision")
        if isinstance((ref := original.artifact_refs.get(name)), Mapping)
    }
    body = {
        "schema_version": 1,
        "run_id": command.run_id,
        "reviewer_execution_id": reviewer_execution_id,
        "rework_execution_id": command.command_id,
        "target_task_id": original.task_id,
        "target_execution_id": original.command_id,
        "instructions": instructions,
        "reviewer_report": raw_report,
        "tool_context": tool_context,
        "source_artifact_refs": source_artifact_refs,
    }
    return body, raw_report, tool_context


def _run_coder_rework(
    command: OperationInput,
    original: OperationInput,
    reviewer_workspace: Path,
    instructions: str,
    progress_refs: dict[str, Any] | None = None,
) -> OperationResult:
    if command.stage_id != "agent_rework" or original.stage_id != "coder":
        raise ValueError("coder rework requires an agent_rework child and coder target")
    if command.run_id != original.run_id or command.command_id == original.command_id:
        raise ValueError("coder rework target identity mismatch")
    root = handlers._run_root(command)
    if Path(original.run_dir).resolve() != root:
        raise ValueError("coder rework target belongs to another Run directory")
    workspace, source_relative = _reviewer_scope(root, reviewer_workspace, command=command)

    advisory = business_gates_disabled(command)
    source_task = original.payload.get("development_task")
    source_context = original.payload.get("planning_context")
    source_goal_ref = original.artifact_refs.get("coder_goal")
    if advisory:
        from .execution_plan import normalize_execution_plan
        # This is an already dispatched task from a larger plan. Normalizing
        # it as a one-task plan would invalidate its peer dependencies and
        # replace its authenticated identity/objective with a fallback task.
        if not isinstance(source_task, Mapping):
            source_task = normalize_execution_plan(
                {'tasks': [{}]},
                workflow_version=original.options.get('workflow_version'),
                model_policy=original.options.get('model_policy'))['tasks'][0]
        source_context = dict(source_context) if isinstance(source_context, Mapping) else {}
    if not isinstance(source_task, Mapping) or not isinstance(source_context, Mapping):
        raise ValueError("target coder lacks its frozen task or planning context")
    if not advisory and not isinstance(source_goal_ref, Mapping):
        raise ValueError("target coder lacks its native goal")
    source_goal = {}
    if isinstance(source_goal_ref, Mapping):
        # The reference retains its original provenance; reading it consumes
        # this rework assignment's budget, not the settled coder's budget.
        # Preserve authentication/read failures in the returned tool result.
        source_goal = json.loads(_verified(command, source_goal_ref).read_text(encoding="utf-8"))
    source_objective = source_goal.get("objective") if isinstance(source_goal, Mapping) else None
    source_supervision = (source_goal.get("supervised_goal_revision")
                          if isinstance(source_goal, Mapping) else None)
    source_goal = validate_goal(
        source_goal,
        source_task,
        source_context,
        require_double_check=original.options.get("workflow_version", 0) >= 12,
        gates_disabled=advisory,
    )
    selected_revision = (command.artifact_refs.get(
        "supervised_goal_revision", original.artifact_refs.get("supervised_goal_revision"))
        if command.options.get("workflow_version", 0) >= 26 else None)
    if (original.options.get("workflow_version", 0) >= 26
            and isinstance(source_objective, str) and source_objective.strip()):
        # Advisory normalization restores the frozen task objective. The
        # actual coder, however, consumed this authenticated prepared goal and
        # may have received a later supervisor revision at dispatch time.
        source_goal["objective"] = source_objective
        if source_supervision is not None:
            source_goal["supervised_goal_revision"] = source_supervision
        if selected_revision is not None:
            from .supervised_goals import apply_to_goal
            source_plan = original.artifact_refs.get("development_plan")
            if not isinstance(source_plan, Mapping):
                raise ValueError("supervised coder rework needs its original plan")
            selected_command = replace(command, artifact_refs={**command.artifact_refs,
                "development_plan": source_plan,
                "supervised_goal_revision": selected_revision})
            source_goal = apply_to_goal(selected_command, source_goal, source_task)
    elif (original.options.get("workflow_version", 0) >= 26
          and selected_revision is not None):
        raise ValueError("supervised coder rework needs its authenticated source goal")
    request, raw_report, tool_context = _request_context(command, original, instructions)
    if selected_revision is not None:
        request["selected_supervised_goal_revision"] = json_copy(selected_revision)
    progress_refs = progress_refs if progress_refs is not None else {}
    request_ref = _artifact(
        command,
        "coder-rework-request.json",
        (json.dumps(request, ensure_ascii=False, sort_keys=True) + "\n").encode(),
        {"target_execution_id": original.command_id},
    )
    progress_refs["coder_rework_request"] = request_ref

    base = prepare_merge_workspace(command, workspace,
                                   report_paths=command.payload.get('reviewer_report_paths', ()))
    generation = command.payload.get("rework_generation", command.options.get("agent_assignment"))
    if type(generation) is not int or generation < 1:
        raise ValueError("coder rework generation must be positive")
    task = json_copy(dict(source_task))
    task["dependencies"] = []
    scope = original.payload.get("goal_scope", "migration")
    if scope not in {"migration", "contract", "target"}:
        raise ValueError("target coder has an invalid goal scope")
    plan = validate_plan(
        {"schema_version": 1, "base_commit": base, "shared_paths": [], "tasks": [task]},
        allow_contract=scope == "contract",
        allow_preparation=task.get("kind") == "prepare",
        workflow_version=command.options.get("workflow_version", 15),
        model_policy=command.options.get('model_policy'))
    task = plan["tasks"][0]
    plan_ref = _artifact(
        command,
        "coder-rework-plan.json",
        (json.dumps(plan, ensure_ascii=False, sort_keys=True) + "\n").encode(),
        {"development_base": base, "target_execution_id": original.command_id},
    )
    progress_refs["coder_rework_plan"] = plan_ref
    context = _context_refs(command, original, source_goal)
    if advisory:
        prior_plan_ref = context.get("development_plan")
        if prior_plan_ref != plan_ref and isinstance(prior_plan_ref, Mapping):
            context["rework_source_development_plan"] = prior_plan_ref
        context["development_plan"] = plan_ref
    goal = json_copy(source_goal)
    base_objective = (source_goal["objective"] if command.options.get("workflow_version", 0) >= 26
                      else task["objective"])
    rework_objective = (base_objective + "\n\nReviewer-requested rework for "
                        + original.command_id + ":\n" + instructions)
    goal.update(
        dependencies=[],
        context_refs=context,
        objective=rework_objective,
    )
    goal = validate_goal(
        goal,
        task,
        context,
        require_double_check=command.options.get(
            "workflow_version", original.options.get("workflow_version", 0)) >= 12,
        gates_disabled=advisory,
    )
    if command.options.get("workflow_version", 0) >= 26:
        goal["objective"] = rework_objective
    goal_ref = _artifact(
        command,
        "coder-rework-goal.json",
        (json.dumps(goal, ensure_ascii=False, sort_keys=True) + "\n").encode(),
        {"target_execution_id": original.command_id},
    )
    progress_refs["coder_rework_goal"] = goal_ref
    # A child is a new assignment, not a continuation of the selected author's
    # segment. Do not inherit that author's workspace epoch into its clone.
    delegated_payload = {name: value for name, value in command.payload.items()
                         if name not in {'recovered_partial_patch', 'recovered_rework_request',
                                         'coder_revival'}}
    delegated_payload['development_workspace_epoch'] = None
    relative_workspace = development_workspace(generation, task['id'], delegated_payload)
    model, effort = (handlers._agent_model_policy(command)
                     if command.options.get('workflow_version', 0) >= 15
                     else (task['model'], task['reasoning_effort']))
    delegated = replace(
        command,
        payload={
            **delegated_payload,
            "development_base": base,
            "execution_development_plan": plan,
            "development_generation": generation,
            "development_source_workspace": source_relative,
            "development_task": task,
            "dependency_patches": [],
            "planning_context": context,
            "goal_scope": scope,
            "goal_generation": generation,
            "development_kind": original.payload.get("development_kind"),
        },
        options={
            **command.options,
            "workspace": relative_workspace,
            "native_goal_resume": False,
            "model": model,
            "reasoning_effort": effort,
        },
        artifact_refs={
            **{key: ref for key, ref in command.artifact_refs.items()
               if key != "supervised_goal_revision"},
            "development_plan": plan_ref,
            "coder_goal": goal_ref,
        },
    )
    coder = CoderHandler()(delegated)
    common = {
        "target_task_id": original.task_id,
        "target_execution_id": original.command_id,
        "rework_generation": generation,
        "before_head": base,
        "reviewer_report": raw_report,
        "review_tool_context": tool_context,
    }
    if command.options.get('workflow_version', 0) >= 18:
        common['candidate_workspace'] = project_relative(root, workspace).as_posix()
    refs = {
        **coder.outputs.get("artifact_refs", {}),
        "coder_rework_request": request_ref,
        "coder_rework_plan": plan_ref,
        "coder_rework_goal": goal_ref,
    }
    progress_refs.update(refs)
    if coder.status != "completed" and (not advisory
            or not coder.outputs.get("artifact_refs", {}).get("coder_patch")):
        return handlers._result(
            command,
            coder.status,
            outputs={**coder.outputs, **common, "artifact_refs": refs},
            detail=coder.detail,
            error_code=coder.error_code or "coder_rework_failed",
        )

    patch_ref = coder.outputs.get("artifact_refs", {}).get("coder_patch")
    patch = _check_ref(delegated, patch_ref, task, base, generation)
    paths = coder.outputs.get("paths")
    if not advisory and (not isinstance(paths, list) or not paths or patch.stat().st_size == 0):
        raise ValueError("coder rework produced no candidate delta")
    integration_base = prepare_merge_workspace(command, workspace,
        report_paths=command.payload.get('reviewer_report_paths', ()))
    conflicts = []
    caller_bound = isinstance(command.payload.get('rework_caller_command'), Mapping)

    integration_started = True
    try:
        _apply(delegated, workspace, patch, task,
               conflict_handoff=conflicts if caller_bound else None)
        after = _head(command, workspace)
        if after == base and not advisory:
            raise ValueError("coder rework did not advance the reviewer workspace")
        integration = {
            "schema_version": 1,
            "target_execution_id": original.command_id,
            "rework_execution_id": command.command_id,
            "base": base,
            "integration_base": integration_base,
            "head": after,
            "paths": paths,
            "generation": generation,
        }
        if advisory:
            integration['status'] = ('conflict' if conflicts else
                                     'integrated' if after != integration_base else 'no_changes')
            if conflicts:
                integration['conflicts'] = conflicts
        integration_ref = _artifact(
            command,
            "coder-rework-integration.json",
            (json.dumps(integration, sort_keys=True) + "\n").encode(),
        )
        refs["coder_rework_integration"] = integration_ref
    except (OSError, ValueError, TypeError, KeyError, subprocess.SubprocessError):
        if integration_started:
            _rollback_integration(command, workspace, integration_base)
        raise
    from .repair_context import inherited_harness_candidate_mode, observe_candidate
    candidate_fields = {}
    if (inherited_harness_candidate_mode(command, workspace)
            or inherited_harness_candidate_mode(original, workspace)):
        candidate_fields['after_candidate_id'] = observe_candidate(
            workspace, inherited_harness=True)
    return handlers._result(
        command,
        "failed" if conflicts else coder.status if advisory else "completed",
        outputs={
            **coder.outputs,
            **common,
            "head": after,
            "after_head": after,
            **candidate_fields,
            "paths": paths,
            "rework_instructions": instructions,
            **({'dependency_conflicts': conflicts} if conflicts else {}),
            **({'integration_status': integration['status']} if advisory else {}),
            "artifact_refs": refs,
        },
        detail=("coder rework merge conflicts require resolution in the requesting author's workspace: "
                + ', '.join(path for conflict in conflicts for path in conflict['paths'])
                if conflicts else
                (coder.detail or coder.error_code or 'coder rework execution failed')
                if coder.status != 'completed' else
                "targeted coder rework produced no integrated changes; execution diagnostics retained"
                if advisory and integration['status'] == 'no_changes' else
                "targeted coder rework integrated and remains subject to the active review"),
        error_code="coder_rework_conflict" if conflicts else coder.error_code if advisory else None,
    )


def run_coder_rework(
    command: OperationInput,
    original: OperationInput,
    reviewer_workspace: Path,
    instructions: str,
) -> OperationResult:
    """Run and integrate one new coder delta while preserving reviewer output.

    The caller must hold the reviewer workspace's ordinary write lock for the
    duration.  ``original`` must be reconstructed from the exact upstream SDK
    command selected by the host, rather than from model-provided fields.
    """
    retained: dict[str, tuple[bytes, int]] = {}
    progress_refs: dict[str, Any] = {}
    workspace = Path(reviewer_workspace)
    result: OperationResult | None = None
    try:
        root = handlers._run_root(command)
        workspace, _ = _reviewer_scope(root, workspace, command=command)
        _, retained = _review_outputs(command, workspace)
        result = _run_coder_rework(
            command, original, workspace, instructions, progress_refs=progress_refs)
    except TimeoutError as exc:
        outputs: dict[str, Any] = {"artifact_refs": dict(progress_refs)}
        metadata = getattr(exc, "metadata", None)
        if isinstance(metadata, Mapping):
            try:
                outputs["inner_diagnostics"] = json_copy(dict(metadata))
            except (TypeError, ValueError):
                pass
        result = handlers._result(
            command,
            "failed",
            outputs=outputs,
            detail=str(exc),
            error_code="budget_exhausted",
        )
    except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError,
            subprocess.SubprocessError) as exc:
        result = handlers._result(
            command,
            "failed",
            detail=str(exc),
            error_code="coder_rework_invalid",
        )
    if retained:
        try:
            _restore_review_outputs(workspace, retained)
        except (OSError, ValueError) as exc:
            return handlers._result(
                command,
                "blocked",
                outputs=result.outputs if result is not None else {},
                detail="could not restore reviewer output: " + str(exc),
                error_code="reviewer_output_restore_failed",
            )
    assert result is not None
    return result
