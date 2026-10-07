"""Diagnose failed coder attempts and return host-consumable revival decisions."""
from __future__ import annotations

from collections.abc import Mapping
from hashlib import sha256
import json
from pathlib import Path
import re
from typing import Any

from . import handlers
from .contracts import OperationInput, OperationResult
from .evidence import verified_path


_REQUEST_FIELDS = frozenset({
    "request_id", "generation", "base_commit", "trigger_execution_ids",
    "requested_tasks", "required_tasks", "tasks", "results", "attempts",
    "prior_decisions", "execution_evidence",
})
_DECISION_FIELDS = frozenset({"task_id", "action", "instruction", "wait_for"})
_SHA256 = re.compile(r"[0-9a-f]{64}")


def _nonempty_string(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be a non-empty string")
    return value


def _string_ids(value: Any, label: str, *, nonempty: bool) -> list[str]:
    if not isinstance(value, list) or (nonempty and not value):
        raise ValueError(f"{label} must be {'a non-empty ' if nonempty else 'a '}list of task IDs")
    if any(not isinstance(item, str) or not item.strip() for item in value):
        raise ValueError(f"{label} must contain non-empty strings")
    if len(value) != len(set(value)):
        raise ValueError(f"{label} must not contain duplicate IDs")
    return list(value)


def _validate_request(request: Any) -> dict[str, Any]:
    if not isinstance(request, Mapping):
        raise ValueError("payload.revival_request must be an object")
    missing = _REQUEST_FIELDS - set(request)
    if missing:
        raise ValueError("revival_request is missing: " + ", ".join(sorted(missing)))

    _nonempty_string(request["request_id"], "revival_request.request_id")
    if type(request["generation"]) is not int or request["generation"] < 0:
        raise ValueError("revival_request.generation must be a non-negative integer")
    _nonempty_string(request["base_commit"], "revival_request.base_commit")
    _string_ids(request["trigger_execution_ids"], "trigger_execution_ids", nonempty=True)
    requested = _string_ids(request["requested_tasks"], "requested_tasks", nonempty=True)
    required = _string_ids(request["required_tasks"], "required_tasks", nonempty=True)
    if not set(required).issubset(requested):
        raise ValueError("required_tasks must be included in requested_tasks")

    tasks = request["tasks"]
    if not isinstance(tasks, list) or any(not isinstance(task, Mapping) for task in tasks):
        raise ValueError("revival_request.tasks must be a list of task objects")
    task_ids = []
    for task in tasks:
        task_ids.append(_nonempty_string(task.get("id"), "task.id"))
    if len(task_ids) != len(set(task_ids)):
        raise ValueError("revival_request.tasks contains duplicate task IDs")
    known = set(task_ids)
    if not set(requested).issubset(known):
        raise ValueError("requested_tasks contains an ID absent from tasks")

    results = request["results"]
    if not isinstance(results, Mapping) or any(not isinstance(row, Mapping) for row in results.values()):
        raise ValueError("revival_request.results must map task IDs to result objects")
    if not set(results).issubset(known):
        raise ValueError("revival_request.results contains an unknown task ID")

    attempts = request["attempts"]
    if not isinstance(attempts, Mapping):
        raise ValueError("revival_request.attempts must be an object")
    if not set(attempts).issubset(known):
        raise ValueError("revival_request.attempts contains an unknown task ID")
    if any(type(count) is not int or count < 0 for count in attempts.values()):
        raise ValueError("revival_request.attempts values must be non-negative integers")

    prior = request["prior_decisions"]
    if not isinstance(prior, list) or any(not isinstance(row, Mapping) for row in prior):
        raise ValueError("revival_request.prior_decisions must be a list of decision objects")
    execution_evidence = request["execution_evidence"]
    if not isinstance(execution_evidence, Mapping):
        raise ValueError("revival_request.execution_evidence must be an object")
    if (not set(execution_evidence).issubset(known)
            or any(not isinstance(row, Mapping) for row in execution_evidence.values())):
        raise ValueError("execution_evidence must map known task IDs to evidence objects")
    return dict(request)


def validate_decision(value: Any, request: Mapping[str, Any]) -> dict[str, Any]:
    """Validate and normalize a planner reply against host-owned task IDs.

    The reply remains only ``decisions`` and ``reason``. Request identity is
    bound by the host in the decision artifact, never accepted from the model.
    """
    bound_request = _validate_request(request)
    if not isinstance(value, Mapping) or set(value) != {"decisions", "reason"}:
        raise ValueError("revival decision must contain only decisions and reason")
    reason = _nonempty_string(value.get("reason"), "reason").strip()
    rows = value.get("decisions")
    if not isinstance(rows, list) or not rows:
        raise ValueError("decisions must be a non-empty list")

    requested = set(bound_request["requested_tasks"])
    known = {task["id"] for task in bound_request["tasks"]}
    required = set(bound_request["required_tasks"])
    normalized: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, row in enumerate(rows):
        if not isinstance(row, Mapping):
            raise ValueError(f"decisions[{index}] must be an object")
        extra = set(row) - (_DECISION_FIELDS | {"reuse_partial"})
        missing = _DECISION_FIELDS - set(row)
        if missing or extra:
            detail = []
            if missing:
                detail.append("missing " + ", ".join(sorted(missing)))
            if extra:
                detail.append("unsupported " + ", ".join(sorted(extra)))
            raise ValueError(f"decisions[{index}] has " + " and ".join(detail))
        task_id = _nonempty_string(row.get("task_id"), f"decisions[{index}].task_id")
        if task_id not in requested:
            raise ValueError(f"decision task_id is not requested: {task_id}")
        if task_id in seen:
            raise ValueError(f"duplicate decision for task: {task_id}")
        seen.add(task_id)
        action = row.get("action")
        if not isinstance(action, str) or action not in {"resume", "wait", "stop"}:
            raise ValueError(f"unsupported action for {task_id}: {action!r}")
        instruction = _nonempty_string(row.get("instruction"), f"decisions[{index}].instruction").strip()
        wait_for = _string_ids(row.get("wait_for"), f"decisions[{index}].wait_for", nonempty=False)
        if action == "wait":
            if not wait_for:
                raise ValueError(f"wait decision for {task_id} must name a prerequisite")
            if task_id in wait_for:
                raise ValueError(f"wait decision for {task_id} cannot wait for itself")
            unknown = set(wait_for) - known
            if unknown:
                raise ValueError("wait_for contains unknown task IDs: " + ", ".join(sorted(unknown)))
        elif wait_for:
            raise ValueError(f"{action} decision for {task_id} must use an empty wait_for list")

        normalized_row = {
            "task_id": task_id,
            "action": action,
            "instruction": instruction,
            "wait_for": wait_for,
        }
        if action == "resume":
            reuse_partial = row.get("reuse_partial", True)
            if type(reuse_partial) is not bool:
                raise ValueError(f"reuse_partial for {task_id} must be a boolean")
            normalized_row["reuse_partial"] = reuse_partial
        elif "reuse_partial" in row:
            raise ValueError(f"reuse_partial is only valid for resume decisions ({task_id})")
        normalized.append(normalized_row)

    missing_required = required - seen
    if missing_required:
        raise ValueError("decisions omit required tasks: " + ", ".join(sorted(missing_required)))
    return {"decisions": normalized, "reason": reason}


def build_revival_planning_prompt(request: Mapping[str, Any],
                                  artifact_refs: Mapping[str, Any] | None = None) -> str:
    """Build final-turn instructions for diagnosis, scoped repair and revival."""
    from .prompts import CODER_RECOVERY_GUIDANCE, CODER_RECOVERY_OBJECTIVE

    bound = _validate_request(request)
    requested = json.dumps(bound["requested_tasks"], ensure_ascii=False)
    required = json.dumps(bound["required_tasks"], ensure_ascii=False)
    execution_evidence = json.dumps(bound["execution_evidence"], ensure_ascii=False,
                                    sort_keys=True, separators=(",", ":"))
    budget_context = json.dumps(bound.get("budget_context", {}), ensure_ascii=False,
                                sort_keys=True, separators=(",", ":"))
    refs = {}
    if isinstance(artifact_refs, Mapping):
        refs = {str(alias): {key: ref[key] for key in ("path", "sha256", "media_type")
                             if key in ref}
                for alias, ref in artifact_refs.items() if isinstance(ref, Mapping)}
    refs_json = json.dumps(refs, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return (
        CODER_RECOVERY_OBJECTIVE + "\n\n" + CODER_RECOVERY_GUIDANCE + "\n\n"
        "Read this operation's input file, especially payload.revival_request and artifact_refs. The "
        "request contains development task records, serialized OperationResults, attempt counts, prior "
        "planner decisions, and references to source results, patches, process logs and shared context. "
        "The complete host-captured SDK execution_evidence records for the requested tasks are included "
        "below. For a settled SDK timeout, read its settled_timeout_diagnosis.artifact_refs.stage_receipt "
        "reference "
        "to distinguish the committed stage response from the outer assignment timeout. Read those "
        "records and then follow the relevant authenticated artifact references to the "
        "complete original logs, results and patches; inspect the corresponding current source and "
        "execution environment. Do not diagnose from status summaries alone. Preserve the evidence by "
        "citing relevant artifact aliases or paths in each instruction or in reason; do not invent or "
        "echo hashes.\n\n"
        "Host execution_evidence records (task keyed):\n" + execution_evidence + "\n\n"
        "Run-level budget context (not a per-task retry cap):\n" + budget_context + "\n\n"
        "Authenticated source and result references (read the relevant complete files):\n" + refs_json + "\n\n"
        "For each failed task, establish what actually happened from the raw error, stdout/stderr, process "
        "and SDK records, task inputs, patch, source tree, toolchain and dependencies. Separate confirmed "
        "causes from hypotheses and unknowns. A timeout, nonzero exit, exit 137, or 'coder exited' is a "
        "symptom by itself: distinguish assignment timeout from Run deadline, process termination, "
        "resource admission and environment failure; attribute OOM only when matching kernel or cgroup "
        "evidence supports it. Inspect earlier attempts and prior decisions, compare their patches and "
        "diagnostics, and do not repeat an ineffective instruction without new causal evidence.\n\n"
        "The host schedules execution. Completion of a named prerequisite produces a fresh "
        "SDK-delivered planner request for tasks that become ready; decide from the current request and "
        "do not poll or launch work yourself. There is no fixed per-task retry cap. Attempt counts are "
        "context, while the shared Run deadline and assignment budget remain the hard limits. Keep "
        "unaffected independent work moving. Repeated identical failures require a new diagnosis or a "
        "specific bounded diagnostic step, not a blind retry.\n\n"
        "Choose resume only with a concrete instruction grounded in the diagnosed cause, including the "
        "specific correction or diagnostic step and evidence to inspect. For resume, set reuse_partial=false "
        "when the existing partial patch conflicts with the diagnosed cause or should be reconstructed "
        "from the archived source/patch evidence; otherwise omit it (the host defaults to true). Choose "
        "wait only for named known task IDs whose completion is a real prerequisite. Apply the "
        "two permitted stop cases above only after considering the available repair handoffs. "
        "Host code validates task state and wait cycles.\n\n"
        "Every required task must have one decision. Additional requested tasks may be omitted when no "
        "decision is needed. Allowed requested task IDs: " + requested + ". Required task IDs: " + required + ".\n\n"
        "Return exactly one JSON object with only these top-level keys: decisions and reason. Each "
        "decision has task_id, action (resume|wait|stop), instruction, and wait_for (an array of known "
        "task IDs). Resume decisions may also include reuse_partial as a boolean. Use an empty wait_for "
        "array for resume and stop; wait requires at least one known ID and cannot name itself. Do not "
        "include request IDs, generation, base commit, hashes, Markdown fences, or extra fields."
    )


def _merge_artifact_refs(command: OperationInput, agent_outputs: Mapping[str, Any],
                         extra: Mapping[str, Any] | None = None) -> dict[str, Any]:
    refs = {key: value for key, value in command.artifact_refs.items()}
    agent_refs = agent_outputs.get("artifact_refs")
    if isinstance(agent_refs, Mapping):
        for alias, ref in agent_refs.items():
            key = str(alias)
            if key in refs and refs[key] != ref:
                key = "revival_planner:" + key
            suffix = 2
            candidate = key
            while candidate in refs and refs[candidate] != ref:
                candidate = f"{key}:{suffix}"
                suffix += 1
            refs[candidate] = ref
    if extra:
        refs.update(extra)
    return refs


def _agent_report_text(command: OperationInput, outputs: Mapping[str, Any]) -> tuple[str, Mapping[str, Any] | None]:
    raw = outputs.get("raw_report")
    if isinstance(raw, str):
        return raw, None
    value = outputs.get("last_message")
    if not isinstance(value, str) or not value.strip():
        raise ValueError("planner result has no final report")
    stripped = value.lstrip()
    if stripped.startswith(("{", "```")):
        return value, None

    root = Path(command.run_dir).resolve()
    relative = Path(value)
    if relative.is_absolute():
        if not relative.resolve().is_relative_to(root):
            raise ValueError("planner final report is outside the Run")
        relative = relative.resolve().relative_to(root)
    rel_text = relative.as_posix()
    refs = outputs.get("artifact_refs")
    if not isinstance(refs, Mapping):
        raise ValueError("planner final report has no artifact references")
    ref = next((item for item in refs.values() if isinstance(item, Mapping)
                and item.get("path") == rel_text), None)
    if ref is None:
        raise ValueError("planner final report has no matching artifact reference")
    path = verified_path(root, ref)
    data = path.read_bytes()
    digest = ref.get("sha256")
    if not isinstance(digest, str) or not _SHA256.fullmatch(digest) or sha256(data).hexdigest() != digest:
        raise ValueError("planner final report digest mismatch")
    return data.decode("utf-8", errors="replace"), ref


def _decode_decision_report(text: str) -> Any:
    try:
        return json.loads(text)
    except (ValueError, TypeError):
        match = re.fullmatch(r"\s*```(?:json)?\s*\n(.*?)\n```\s*", text, re.IGNORECASE | re.DOTALL)
        if match is None:
            raise ValueError("planner final report is not a JSON object")
        try:
            return json.loads(match.group(1))
        except (ValueError, TypeError) as exc:
            raise ValueError("planner final report contains invalid JSON") from exc


def _failure_result(command: OperationInput, status: str, detail: str, error_code: str,
                    outputs: Mapping[str, Any] | None = None) -> OperationResult:
    result_outputs = dict(outputs or {})
    result_outputs["artifact_refs"] = _merge_artifact_refs(command, result_outputs)
    return handlers._result(command, status, outputs=result_outputs, detail=detail, error_code=error_code)


class CoderRevivalPlannerHandler:
    """Plan bounded root-cause based revival decisions for eligible coder tasks."""

    def __call__(self, command: OperationInput) -> OperationResult:
        from .report_dialogue import dialogue_enabled

        try:
            request = _validate_request(command.payload.get("revival_request"))
        except (TypeError, ValueError) as exc:
            return _failure_result(command, "failed", str(exc), "revival_request_invalid")

        if not dialogue_enabled(command):
            return _failure_result(
                command, "failed",
                "coder revival planning requires the workflow's two-turn agent dialogue policy",
                "revival_dialogue_policy_missing",
            )

        prompt = build_revival_planning_prompt(request, command.artifact_refs)
        prepared = command
        repair_preparation_diagnostic = None
        from . import diagnostic_repairs
        repair_enabled = (diagnostic_repairs.enabled(command)
                          and command.payload.get("goal_scope") != "contract"
                          and bool(command.payload.get("diagnostic_repair_targets")))
        if repair_enabled:
            try:
                prepared = diagnostic_repairs.prepare(command)
                prompt += "\n\n" + diagnostic_repairs.instructions(prepared)
            except (OSError, ValueError, TypeError, KeyError) as exc:
                # A repair snapshot is optional; diagnosis must remain usable
                # without granting writes to the original shared workspace.
                repair_preparation_diagnostic = str(exc)
                prepared = command
        repair_enabled = repair_enabled and bool(
            prepared.payload.get("diagnostic_repair_manifest"))
        try:
            agent_result = handlers.CodexStageHandler(
                prompt, baseline=command.payload.get("goal_scope") == "contract",
                read_only=not repair_enabled, reuse_recovery_prompt=False,
            )(prepared)
            agent_result.validate_for(command)
        except Exception as exc:
            return _failure_result(
                command, "failed", f"coder revival planner execution failed: {exc}",
                "revival_planner_execution_failed",
            )

        outputs = dict(agent_result.outputs)
        if repair_preparation_diagnostic is not None:
            outputs["diagnostic_repair_preparation_diagnostic"] = repair_preparation_diagnostic
        if agent_result.status != "completed":
            try:
                raw, _ = _agent_report_text(command, outputs)
                outputs["raw_report"] = raw
            except (OSError, UnicodeError, ValueError, TypeError):
                pass
            return _failure_result(command, agent_result.status, agent_result.detail,
                                   agent_result.error_code or "revival_planner_execution_failed",
                                   outputs)

        dialogue = outputs.get("agent_dialogue")
        if (not isinstance(dialogue, Mapping) or dialogue.get("transport") != "opencode"
                or type(dialogue.get("turns")) is not int or dialogue.get("turns") != 2):
            try:
                raw, _ = _agent_report_text(command, outputs)
                outputs["raw_report"] = raw
            except (OSError, UnicodeError, ValueError, TypeError):
                pass
            return _failure_result(
                command, "failed", "planner result did not confirm both OpenCode dialogue turns",
                "revival_dialogue_incomplete", outputs,
            )

        try:
            raw, _ = _agent_report_text(command, outputs)
            normalized = validate_decision(_decode_decision_report(raw), request)
        except (OSError, UnicodeError, ValueError, TypeError) as exc:
            outputs["raw_report"] = raw if "raw" in locals() else ""
            outputs["revival_decision_diagnostic"] = str(exc)
            return _failure_result(command, "failed", str(exc), "revival_decision_invalid", outputs)

        if repair_enabled:
            # Only a validated decision can publish an applicable correction.
            # The host supplies every repair identity and artifact reference.
            agent_result = diagnostic_repairs.collect(prepared, agent_result)
            outputs.update(agent_result.outputs)

        decision_artifact = {
            "schema_version": 1,
            "document_kind": "modport-coder-revival-decision-v1",
            "run_id": command.run_id,
            "stage_id": command.stage_id,
            "execution_id": command.command_id,
            "request_id": request["request_id"],
            "generation": request["generation"],
            "base_commit": request["base_commit"],
            "trigger_execution_ids": request["trigger_execution_ids"],
            "input_artifact_refs": dict(command.artifact_refs),
            "decision": normalized,
            **({"diagnostic_repairs": outputs["diagnostic_repairs"]}
               if "diagnostic_repairs" in outputs else {}),
        }
        try:
            from .development import _artifact
            artifact_ref = _artifact(
                command,
                "coder-revival-decision.json",
                (json.dumps(decision_artifact, ensure_ascii=False, sort_keys=True,
                            separators=(",", ":")) + "\n").encode("utf-8"),
                {"document_kind": "modport-coder-revival-decision-v1",
                 "request_id": request["request_id"], "generation": request["generation"]},
            )
            artifact_ref["media_type"] = "application/json"
        except Exception as exc:
            outputs["raw_report"] = raw
            outputs["revival_decision_diagnostic"] = "validated decision could not be archived: " + str(exc)
            return _failure_result(command, "failed", outputs["revival_decision_diagnostic"],
                                   "revival_decision_archive_failed", outputs)

        agent_refs = _merge_artifact_refs(command, outputs, {"revival_decision": artifact_ref})
        normalized_outputs = {
            **outputs,
            "revival_decision": normalized,
            "artifact_refs": agent_refs,
            "decision_artifact": artifact_ref,
        }
        return handlers._result(command, "completed", outputs=normalized_outputs,
                                detail="coder revival decision validated and archived")
