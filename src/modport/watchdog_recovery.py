"""Supervisor-first same-Run recovery through the SDK's public reopen protocol.

The original input, definition, deadline and cumulative usage remain frozen.
SDK preparation and activation records supply crash replay; no parallel recovery
packet, retrying author, or host database mutation is introduced here.
"""
from __future__ import annotations

import math
from pathlib import Path
from typing import Any

from dispatcher_sdk.orchestrator import Orchestrator

from .application_state_storage import hydrate_run_snapshot, pack_application_state
from .contracts import OperationInput, json_copy
from .evidence import workspace_lock
from .token_budget import read_token_budget


_AUTHORIZATION = "modport.watchdog.policy"
_COMMAND_PREFIX = "watchdog-recover:g"


def _enabled(header, app):
    from .watchdog_events import enabled
    return enabled(header, app)


def _begin(owner, snapshot, header, app, incident):
    from .watchdog_routing import begin
    return begin(owner, snapshot, header, app, incident)


def _status(state, status, reason, *, recovery=None, **details):
    result = {"status": status, "run_id": state["run_id"], "state": state["state"],
              "generation": int(state.get("generation", 0)), "reason": reason, **details}
    if recovery is not None:
        result["recovery_id"] = recovery["recovery_id"]
    return result


def _guard(owner, root, header, state, *, assignments=True):
    app = state.get("application_state") or {}
    if state["state"] == "succeeded":
        return _status(state, "terminal", "run_succeeded")
    if (state["state"] == "cancelled" or app.get("user_cancelled") is True
            or app.get("stop_reason") == "user_cancelled"):
        return _status(state, "terminal", "user_cancelled")
    confirmed = app.get("watchdog", {}).get("stop_confirmed")
    if isinstance(confirmed, dict) and confirmed.get("category") in {"unrecoverable", "budget_exhausted"}:
        return _status(state, "terminal", "watchdog_stop_confirmed", stop=confirmed)
    if not _enabled(header, app):
        return _status(state, "blocked", "watchdog_not_authorized")
    deadline = header.get("deadline_epoch")
    if type(deadline) not in (int, float) or not math.isfinite(deadline):
        return _status(state, "blocked", "original_deadline_unknown")
    if owner.clock() >= deadline:
        return _status(state, "terminal", "wall_clock_budget_exhausted")
    terminal_reason = app.get("stop_reason") or app.get("terminal_reason")
    if terminal_reason in {"wall_clock_budget_exhausted", "agent_assignment_budget_exhausted",
                           "token_budget_exhausted", "budget_exhausted"}:
        return _status(state, "terminal", terminal_reason)
    if read_token_budget(root)["exhausted"]:
        return _status(state, "terminal", "token_budget_exhausted")
    if assignments:
        used = app.get("agent_assignments")
        limit = header.get("request", {}).get("budget", {}).get("max_agent_assignments")
        if type(used) is not int or used < 0 or (limit is not None and (type(limit) is not int or limit < 0)):
            return _status(state, "blocked", "assignment_budget_unknown")
        if limit is not None and used >= limit:
            return _status(state, "terminal", "agent_assignment_budget_exhausted")
    return None


def _ours(record, run_id):
    return (record is not None and record.get("run_id") == run_id
            and record.get("owner_id") == "modport-watchdog:" + run_id
            and record.get("authorization_source") == _AUTHORIZATION
            and record.get("command_id") == _COMMAND_PREFIX + str(record.get("target_generation")))


def _latest(sdk, run_id):
    summary = sdk.get_run_summary(run_id)
    return sdk.get_recovery(summary["recovery_id"]) if summary.get("recovery_id") else None


def _activate_committed(owner, root, header, run_id):
    """Roll forward the exact SDK commit before strict execution session checks."""
    sdk = Orchestrator(root / "orchestrator.sqlite3", None, clock=owner.clock)
    try:
        record = _latest(sdk, run_id)
        if not _ours(record, run_id) or record["status"] != "committed":
            return None
        state = hydrate_run_snapshot(root, sdk.get_run(run_id))
        refusal = _guard(owner, root, header, state, assignments=False)
        if refusal is not None:
            return refusal
        # Activation advances only the persisted marker; it neither chooses a
        # deployment nor creates another decision, generation, or assignment.
        sdk.advance_recovery(record["recovery_id"], owner_id="modport-watchdog:" + run_id)
    finally:
        sdk.close()
    return None


def _target(state, app):
    tasks = state.get("tasks", {})
    watchdog = app.get("watchdog", {})
    episode = watchdog.get("episodes", {}).get(watchdog.get("active")) or {}
    request = episode.get("request") or {}
    retained_task = tasks.get(request.get("target_task_id"))
    if retained_task and retained_task.get("attempts"):
        last = retained_task["attempts"][-1]
        operation = OperationInput.from_dict(last["command"]["payload"])
        if (operation.stage_id != "supervisor"
                and last["command"]["execution_id"] == request.get("target_execution_id")):
            return request["target_task_id"], request["target_execution_id"]
    controls = watchdog.get("resume_controls") or {}
    active = app.get("active_stage") or controls.get("active_stage")
    candidates = ([active] if active in tasks else []) + list(reversed(tasks))
    for task_id in candidates:
        attempts = tasks[task_id].get("attempts") or []
        if not attempts:
            continue
        last = attempts[-1]
        operation = OperationInput.from_dict(last["command"]["payload"])
        if operation.stage_id == "supervisor":
            continue
        result = (last.get("result") or {}).get("value")
        failed = (last.get("state") in {"failed", "cancelled", "timed_out", "dead"}
                  or isinstance(result, dict) and result.get("status") in {"failed", "blocked"})
        if task_id == active or failed:
            return task_id, last.get("command", {}).get("execution_id")
    return None, None


def recover(owner: Any, run_dir: str | Path, run_id: str) -> dict[str, Any]:
    """Recover a terminal failure into one supervisor assignment, idempotently."""
    root = Path(run_dir).resolve()
    header = owner._header(root, run_id)
    with workspace_lock(root / ".locks" / "continuation", blocking=False):
        refusal = _activate_committed(owner, root, header, run_id)
        if refusal is not None:
            return refusal
        with owner.session(root, run_id, allow_terminal_deployment=True) as (root, header, runtime, sdk):
            runtime.reap()
            sdk.sync()
            state = hydrate_run_snapshot(root, sdk.get_run(run_id))
            record = _latest(sdk, run_id)
            replay = _ours(record, run_id) and record["status"] in {"preparing", "prepared", "activated"}
            # Waking an existing generation does not authorize an assignment.
            # The driver enforces admission when it schedules the diagnosis.
            refusal = _guard(owner, root, header, state, assignments=state["state"] != "running")
            if refusal is not None:
                return refusal
            if replay and record["status"] in {"preparing", "prepared"}:
                if record["target_deployment"].get("registry_revision") != runtime.registry_revision:
                    return _status(state, "blocked", "prepared_deployment_not_installed", recovery=record)
                record = sdk.advance_recovery(record["recovery_id"], owner_id="modport-watchdog:" + run_id)
                state = hydrate_run_snapshot(root, sdk.get_run(run_id))
                return _status(state, "reopened", "persisted_recovery_activated", recovery=record)
            if state["state"] == "running":
                if replay and record["status"] == "activated" and record["target_generation"] == state.get("generation", 0):
                    return _status(state, "reopened", "recovery_already_activated", recovery=record)
                from .watchdog_events import notifications
                watchdog = (state.get("application_state") or {}).get("watchdog", {})
                seen = set(watchdog.get("seen", []))
                pending = any(event.get("notification_id") not in seen
                              and event.get("generation", event.get("target", {}).get("generation", 0))
                              == state.get("generation", 0)
                              and event.get("kind") in {"driver_lost", "stalled", "terminal", "recovery_required", "no_useful_progress"}
                              for event, _ in notifications(root, run_id))
                if pending or watchdog.get("active"):
                    return _status(state, "reopened", "watchdog_diagnostic_pending")
                return _status(state, "blocked", "run_not_terminal")
            if state["state"] != "failed":
                return _status(state, "blocked", "run_not_failed")
            preflight = sdk.inspect_reopen(run_id, expected_revision=state["revision"],
                                           expected_generation=state.get("generation", 0))
            if not preflight["complete"] and all(
                    item["code"] in {"pending_result_delivery", "kernel_result_not_settled"}
                    for item in preflight["blockers"]):
                owner._drain_result_delivery(sdk, owner="modport-watchdog:" + run_id)
                preflight = sdk.inspect_reopen(run_id, expected_revision=state["revision"],
                                               expected_generation=state.get("generation", 0))
            if not preflight["complete"]:
                return _status(state, "blocked", "sdk_reopen_blocked", blockers=preflight["blockers"])
            app = json_copy(state.get("application_state") or {})
            task_id, execution_id = _target(state, app)
            generation = int(state.get("generation", 0)) + 1
            watchdog = app.setdefault("watchdog", {})
            watchdog.setdefault("resume_controls", {
                "active_stage": json_copy(app.get("active_stage")),
                "active_group": json_copy(app.get("active_group") or app.get('failed_development_group')),
            })
            watchdog["recovered_failure"] = {"generation": state.get("generation", 0),
                "revision": state["revision"], "reason": app.get("terminal_reason") or app.get("stop_reason")}
            prior_episode = watchdog.get("episodes", {}).get(watchdog.get("active"))
            if isinstance(prior_episode, dict):
                prior_episode["status"] = "interrupted_by_terminal_failure"
            watchdog["active"] = None
            reason = app.get("terminal_reason") or app.get("stop_reason") or "terminal Run failure"
            incident = {"incident_id": "recovery.g" + str(generation), "kind": "run_failed",
                "reason": str(reason), "target_task_id": task_id, "target_execution_id": execution_id}
            app["active_stage"] = None
            app["active_group"] = None
            app["stop_reason"] = None
            app["stop_state"] = None
            app["terminal_reason"] = None
            execution_header = json_copy(header)
            execution_header["registry_revision"] = runtime.registry_revision
            operations = _begin(owner, state, execution_header, app, incident)
            if not any(operation.get("kind") == "dispatch" for operation in operations):
                return _status(state, "blocked", "supervisor_not_scheduled")
            deployment = {"registry_revision": runtime.registry_revision,
                "handler_revisions": {
                    (f"{key[0]}:{key[1]}" if isinstance(key, tuple) and len(key) == 2 else str(key)): value
                    for key, value in runtime.handler_revisions.items()}}
            record = sdk.reopen_run(run_id, command_id=_COMMAND_PREFIX + str(generation),
                expected_revision=state["revision"], expected_generation=state.get("generation", 0),
                actor="modport-watchdog", authorization_source=_AUTHORIZATION,
                reason="Investigate terminal failure under the original Run budget",
                owner_id="modport-watchdog:" + run_id, target_deployment=deployment,
                decision={"start_stage": "supervisor", "reused_artifacts": [], "invalidated_artifacts": [],
                          "budget_change": None, "incident_id": incident["incident_id"]},
                application_state=pack_application_state(root, app), operations=operations)
            state = hydrate_run_snapshot(root, sdk.get_run(run_id))
            return _status(state, "reopened", "supervisor_recovery_activated", recovery=record)
