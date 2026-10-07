"""Pure evidence windows and narrowly scoped asynchronous supervisor decisions.

The host records business assignments in dispatch order, excluding supervisor
assignments. Each record has execution_id, stage and state; optional result is
an OperationResult dictionary. artifact_refs and progress must be host-observed
evidence, not agent claims. progress is a mapping of stable progress counter
names to finite numbers, where increases mean forward progress. blocker may
provide the host's exact error and file diagnostic.

Windows are fixed groups of five assignments, including running assignments.
The host schedules every due window once, without waiting for its decision
before starting the next business assignment. It persists scheduled window IDs
and applies validated interventions only at subsequent scheduling boundaries.
This module neither schedules executions nor changes host policy or inputs.
"""
from __future__ import annotations

import json
import math
from typing import Any, Iterable, Mapping, Sequence

from .manifest import canonical_json, sha256


INTERVAL = 5
SUPERVISOR_DECISIONS = ("continue", "targeted_fix", "replan", "pause")

DEEP_SUPERVISOR_PROMPT = """Investigate whether this Run's workflow and dispatched goals
are actually producing useful migration progress. The evidence window is an index,
not the investigation itself. Read the referenced original inputs, receipts, raw
errors, tool calls and results, and the available source snapshot. Follow a failure
from its source through context/goal preparation, dispatch, execution and downstream
consumption. Distinguish confirmed causes from hypotheses and missing evidence.
'The agent failed', a timeout, and an exit code are symptoms, not explanations.
Check wrong versions, missing or discarded context, stale artifacts, incorrect
dependencies, scheduling/recovery behavior and actual code/environment defects.
Measure progress by code changes, resolved defects and fresh validation, not reports,
heartbeats or task counts. Check the source snapshot manifest for omissions and
per-file capture identity; it is not an atomic snapshot of live workers.

You may directly edit the supplied goals/*.md documents to correct the work given
to coders. Read their host manifest and original goal first. Preserve the user's
requirements, acceptance criteria and necessary context. Explain why each edit
addresses the observed cause and what verification will expose the original defect.
Only these goal document edits are published. Source files are investigation copies;
edits there do not repair the live project. Use the registered artifact reader for
host evidence, with expected_sha256 when supplied, and bounded slices as needed.
Do not edit SDK state, sealed history, frozen versions, budgets or active workspaces.
Goal revisions are host-bound to the exact task and plan for subsequent matching
dispatches. Running attempts retain their original input. Publication alone does
not prove that any coder consumed the revision or that a repair succeeded.
Do not launch agents or blindly retry work. If a framework change or an explicit
repair dispatch is needed, identify the code path and concrete change for the host.
Return a concise Markdown report with evidence paths, confirmed causes, remaining
uncertainties, goal edits and the next useful validation. No decision JSON required.
"""


def deep_supervision(value):
    options = value.options if hasattr(value, 'options') else value.get('definition', value)
    return options.get('workflow_version', 0) >= 26


def goal_targets(snapshot, app, packet):
    """Select exact existing task/plan identities, including not-yet-dispatched tasks."""
    from .contracts import OperationInput
    from .supervised_goals import target_key
    targets = {}
    window_ids = set(packet['evidence_execution_ids'])
    for item in snapshot['tasks'].values():
        attempt = item['attempts'][-1]
        command = OperationInput.from_dict(attempt['command']['payload'])
        if command.command_id not in window_ids:
            continue
        task = command.payload.get('development_task')
        plan = command.artifact_refs.get('development_plan')
        if not isinstance(task, dict) or not isinstance(plan, dict):
            continue
        key = target_key(task, plan)
        refs = ((attempt.get('result') or {}).get('value') or {}).get('outputs', {}).get('artifact_refs', {})
        targets[key] = {'key': key, 'task': task, 'plan_ref': plan,
                        'source_execution_id': command.command_id,
                        'source_workspace': command.options.get('workspace', 'worktree'),
                        'goal_ref': refs.get('coder_goal', command.artifact_refs.get('coder_goal'))}
    group = app.get('active_group') or {}
    plan = group.get('artifact_refs', {}).get('development_plan')
    if isinstance(plan, dict):
        for task in group.get('tasks', []):
            key = target_key(task, plan)
            targets.setdefault(key, {'key': key, 'task': task, 'plan_ref': plan,
                                    'source_execution_id': None})
    for key, target in targets.items():
        previous = app.get('supervision', {}).get('goal_revisions', {}).get(key, {})
        if previous.get('revision_ref'):
            target['previous_revision_ref'] = previous['revision_ref']
    return list(targets.values())


def execution_refs(command, outputs=None):
    """Expose queued inputs and original receipts without recursively copying history."""
    from pathlib import Path
    from .evidence import atomic_json, file_digest
    root = Path(command.run_dir)
    refs = dict((outputs or {}).get('artifact_refs', {}))
    directory = root / 'artifacts' / 'supervision-inputs'
    if directory.resolve() != directory.absolute():
        raise ValueError('unsafe supervision input directory')
    path = directory / (sha256({'execution_id': command.command_id}) + '.json')
    if path.is_symlink():
        raise ValueError('unsafe supervision input snapshot')
    if not path.exists():
        value = command.to_dict()
        upstream = value.pop('upstream_results', {})
        # Payload/options/context refs are the actual dispatched values. Full
        # upstream reports stay at their original refs and execution receipts.
        value['upstream_execution_index'] = {
            name: {key: result.get(key) for key in ('command_id', 'task_id', 'stage_id', 'status', 'error_code')}
            for name, result in upstream.items() if isinstance(result, dict)}
        value['snapshot_note'] = 'Dispatched input; upstream_results replaced by execution index to avoid recursive history copies.'
        atomic_json(path, value)
    refs['dispatched_input'] = {'path': path.relative_to(root).as_posix(),
        'sha256': file_digest(path), 'media_type': 'application/json'}
    for name in ('input', 'receipt'):
        path = root / 'artifacts' / 'executions' / command.command_id / (name + '.json')
        if path.is_file() and path.resolve() == path.absolute():
            refs['execution_' + name] = {'path': path.relative_to(root).as_posix(),
                'sha256': file_digest(path), 'media_type': 'application/json'}
    return refs

SUPERVISOR_PROMPT = """Independently supervise the supplied five-business-assignment
evidence window. The host continues business work concurrently. Inspect every
attempt and result, tangible progress and its concrete deltas, blocker stage,
error, file, signature and repeat count, and previous process improvements.
Distinguish actual progress from another report, pending work, or a repeated
failure. Cite the five execution IDs exactly in their supplied order.
Return only a JSON object with schema_version=1, decision (continue,
targeted_fix, replan, or pause), reason (nonempty string),
evidence_execution_ids (the exact five IDs), and process_improvements (array of
specific nonempty strings). For targeted_fix or replan also provide intervention
with one or more of prompt (edited assignment instructions), task_ids (a
nonempty selection from the supplied allowed task bundle), stage (one supplied
allowed agent stage), profile (one supplied allowed profile). continue and
pause must omit intervention. Use only host-supplied allowlists; unknown
selections are invalid. A recommendation is not authorization to skip work.
Never waive validators, independent reviews, tests, frozen behavior, sandbox
gates, budgets, or evidence requirements. Do not alter host inputs, locked
versions, manifests, budget settings, workspace, command arguments, policy,
or the authenticated repair/planning chain. Prompt edits remain subordinate to
the original assignment and host rules. Do not execute tools or project code,
edit files, launch agents, or start retries. Recommend a process correction or
pause when evidence is insufficient. A changed report alone is not proof of
migration progress; pending results are unknown, never successes.
Wire types: schema_version is integer 1; decision is a string enum. The five
listed root fields are required; intervention is the only optional root field.
evidence_execution_ids and process_improvements are arrays of unique nonblank
strings, never mappings or joined text; process_improvements may be [].
intervention is a nonempty object containing only prompt:string, task_ids:array
of unique nonblank strings, stage:string, profile:string; include only chosen
fields. All text is nonblank without NUL characters, at most 16000 characters
per string, except intervention.prompt permits 32000 characters.
"""


def _copy(value: Any) -> Any:
    return json.loads(canonical_json(value))


def _text(value: Any, name: str, *, limit: int = 16_000) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > limit or "\x00" in value:
        raise ValueError(f"{name} must be a nonempty string of at most {limit} characters")
    return value


def _strings(value: Any, name: str, *, nonempty: bool = False) -> list[str]:
    if not isinstance(value, (list, tuple)) or (nonempty and not value):
        raise ValueError(f"{name} must be {'a nonempty' if nonempty else 'an'} array")
    values = [_text(item, name) for item in value]
    if len(set(values)) != len(values):
        raise ValueError(f"{name} must not contain duplicates")
    return values


def due_windows(business_attempts: int, scheduled_windows: Iterable[int] = ()) -> tuple[int, ...]:
    """Return unscheduled window end ordinals, independent of completion order."""
    if type(business_attempts) is not int or business_attempts < 0:
        raise ValueError("business_attempts must be a nonnegative integer")
    scheduled = tuple(scheduled_windows)
    if any(type(end) is not int or end < INTERVAL or end % INTERVAL for end in scheduled):
        raise ValueError("scheduled windows must be positive multiples of five")
    return tuple(end for end in range(INTERVAL, business_attempts + 1, INTERVAL)
                 if end not in scheduled)


def _metrics(value: Any) -> dict[str, int | float]:
    if not isinstance(value, Mapping):
        raise ValueError("progress must be a host-observed metric mapping")
    result = {}
    for key, number in value.items():
        _text(key, "progress metric")
        if type(number) not in (int, float) or not math.isfinite(number):
            raise ValueError("progress metrics must be finite numbers")
        result[key] = number
    return result


def build_evidence_packet(attempts: Sequence[Mapping[str, Any]], *,
                          window_end: int | None = None,
                          process_improvements: Sequence[str] = ()) -> dict[str, Any]:
    """Detach exactly five records and compute deltas against prior stage evidence.

    Repeated blockers count matching stage/error/file signatures across the full
    supplied history through this window. Running records remain in the window
    with their original state and null result. Future records cannot contaminate
    historical windows. Artifact changes are reported separately from tangible
    progress: host metric deltas and newly completed stages are concrete signals,
    while a rewritten diagnostic alone does not prove forward progress.
    """
    end = len(attempts) // INTERVAL * INTERVAL if window_end is None else window_end
    if type(end) is not int or end < INTERVAL or end % INTERVAL or end > len(attempts):
        raise ValueError("window_end must select a complete five-assignment window")
    history = _copy(attempts[:end])
    ids = [_text(row.get("execution_id"), "execution_id") for row in history]
    if len(ids) != len(set(ids)):
        raise ValueError("business execution IDs must be unique")
    previous_artifacts: dict[str, dict] = {}
    previous_metrics: dict[str, dict] = {}
    completed_stages: set[str] = set()
    repeats: dict[str, int] = {}
    records = []
    for ordinal, row in enumerate(history, 1):
        stage = _text(row.get("stage"), "stage")
        if stage == "supervisor":
            raise ValueError("supervisor assignments are not business attempts")
        state = _text(row.get("state"), "state")
        result = row.get("result")
        if result is not None and not isinstance(result, dict):
            raise ValueError("result must be an OperationResult mapping or null")
        outcome = result or {}
        artifacts = row.get("artifact_refs", {})
        if not isinstance(artifacts, dict):
            raise ValueError("artifact_refs must be a mapping")
        old_artifacts = previous_artifacts.get(stage, {})
        changed = sorted(key for key, ref in artifacts.items() if old_artifacts.get(key) != ref)
        metrics = _metrics(row.get("progress", {}))
        old_metrics = previous_metrics.get(stage, {})
        metric_delta = {key: {"before": old_metrics.get(key), "after": value,
                              "change": value - old_metrics[key] if key in old_metrics else None}
                        for key, value in metrics.items() if old_metrics.get(key) != value}
        verdict = (outcome.get("outputs") or {}).get("verdict") if isinstance(outcome.get("outputs"), Mapping) else None
        completed = (state in {"succeeded", "completed"}
                     and outcome.get("status") == "completed"
                     and verdict in {None, "approved"})
        new_completion = completed and stage not in completed_stages
        if completed:
            completed_stages.add(stage)
        # A newly observed metric is evidence, but without a baseline it is not
        # a measured delta. Regressions remain explicit but are not progress.
        measured_change = any(item["change"] is not None and item["change"] > 0
                              for item in metric_delta.values())
        delta = {"newly_completed_stage": stage if new_completion else None,
                 "changed_artifact_refs": changed, "metrics": metric_delta}
        diagnostic = row.get("blocker") or {}
        if not isinstance(diagnostic, dict) or set(diagnostic) - {"stage", "error", "file"}:
            raise ValueError("blocker accepts only stage, error and file")
        rejected = verdict is not None and verdict != "approved"
        failed = (state in {"failed", "blocked", "cancelled", "canceled", "timed_out"}
                  or outcome.get("status") in {"failed", "blocked"} or rejected)
        blocker = None
        if failed or diagnostic:
            identity = {"stage": diagnostic.get("stage", stage),
                        "error": diagnostic.get("error") or outcome.get("error_code")
                                 or ("review_rejected" if rejected else state),
                        "file": diagnostic.get("file")}
            _text(identity["stage"], "blocker stage")
            _text(identity["error"], "blocker error")
            if identity["file"] is not None:
                _text(identity["file"], "blocker file")
            signature = sha256(identity)
            repeats[signature] = repeats.get(signature, 0) + 1
            blocker = {**identity, "signature": signature, "repeat_count": repeats[signature]}
        previous_artifacts[stage] = {**old_artifacts, **artifacts}
        previous_metrics[stage] = {**old_metrics, **metrics}
        if ordinal > end - INTERVAL:
            records.append({**row, "ordinal": ordinal, "result": result,
                            "tangible_progress": new_completion or measured_change,
                            "delta": delta, "blocker": blocker})
    return {"schema_version": 1, "window_start": end - INTERVAL + 1,
            "window_end": end, "evidence_execution_ids": ids[-INTERVAL:],
            "attempts": records, "tangible_progress": any(row["tangible_progress"] for row in records),
            "process_improvements": _strings(process_improvements, "process_improvements")}


def validate_supervisor_decision(document: Mapping[str, Any], *,
                                 evidence_execution_ids: Sequence[str],
                                 allowed_stages: Iterable[str] = (),
                                 allowed_profiles: Iterable[str] = (),
                                 allowed_task_ids: Iterable[str] = ()) -> dict[str, Any]:
    """Validate advisory scope, never grant changes to host-enforced invariants.

    The host must layer prompt text below immutable assignment/rule instructions
    and revalidate selected tasks against the authenticated planning chain.
    This validator permits only selection of preauthorized IDs; it cannot make
    arbitrary natural-language instructions trustworthy.
    """
    if not isinstance(document, Mapping):
        raise ValueError("supervisor decision must be an object")
    required = {"schema_version", "decision", "reason", "evidence_execution_ids", "process_improvements"}
    if set(document) - required - {"intervention"} or required - set(document):
        raise ValueError("supervisor decision contains missing or unsupported fields")
    value = _copy(document)
    if type(value["schema_version"]) is not int or value["schema_version"] != 1:
        raise ValueError("unsupported supervisor schema_version")
    decision = value["decision"]
    if decision not in SUPERVISOR_DECISIONS:
        raise ValueError("unsupported supervisor decision")
    _text(value["reason"], "reason")
    expected = _strings(list(evidence_execution_ids), "expected evidence_execution_ids", nonempty=True)
    actual = _strings(value["evidence_execution_ids"], "evidence_execution_ids", nonempty=True)
    if len(expected) != INTERVAL or actual != expected:
        raise ValueError("supervisor must cite the exact ordered five execution IDs")
    _strings(value["process_improvements"], "process_improvements")
    if decision in {"continue", "pause"}:
        if "intervention" in value:
            raise ValueError("continue and pause must omit intervention")
        return value
    intervention = value.get("intervention")
    if not isinstance(intervention, dict) or not intervention:
        raise ValueError("targeted_fix and replan require a concrete intervention")
    if set(intervention) - {"prompt", "task_ids", "stage", "profile"}:
        raise ValueError("unsupported supervisor intervention field")
    if "prompt" in intervention:
        _text(intervention["prompt"], "intervention prompt", limit=32_000)
    if "task_ids" in intervention:
        selected = _strings(intervention["task_ids"], "task_ids", nonempty=True)
        if not set(selected) <= set(allowed_task_ids):
            raise ValueError("supervisor task selection is outside the authorized bundle")
    for key, allowed in (("stage", allowed_stages), ("profile", allowed_profiles)):
        if key in intervention:
            _text(intervention[key], "intervention " + key)
            if intervention[key] not in set(allowed):
                raise ValueError(f"supervisor {key} is outside the host allowlist")
    return value
