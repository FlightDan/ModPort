"""Reopen a settled, interrupted contract verification in the same SDK Run.

Only the execution deployment and the verification attempt change. The
immutable workflow, authenticated inputs, elapsed time, and assignment budget
remain those of the original Run.
"""

from pathlib import Path

from .application_state_storage import hydrate_run_snapshot, pack_application_state
from .contracts import json_copy
from .evidence import atomic_json, read_json, workspace_lock
from .models import MigrationRequest
from .operations import (
    MigrationRun, _recovery_payload_digest, _verify_recovery_payload,
)
from .payload_storage import read_prepared_json
from .sdk_compat import inspect_runtime
from .storage_budget import check_storage_budget
from .workflow import MAIN_STAGES, REWORK_STAGES, agent_model_policy, compile_migration_workflow


_STAGE = "contract_verify"
_ERROR = "contract_verify_interrupted"
_COMMAND_PREFIX = "reopen:contract_verify_interrupted:g"
_DOWNSTREAM = frozenset(MAIN_STAGES[MAIN_STAGES.index(_STAGE) + 1:]) | frozenset(
    (*REWORK_STAGES, "coder", "agent_rework", "goal_prepare", "gate_handoff")
)


def _validate_source(ops, header, state):
    if state["state"] != "failed":
        raise ValueError("verification recovery requires a failed Run")
    if state["definition"] != header["definition"] or state["input"] != header:
        raise ValueError("verification recovery cannot change frozen input")
    app = state.get("application_state") or {}
    if any(key in app.get("effective", {}) for key in _DOWNSTREAM):
        raise ValueError("verification recovery cannot replace downstream results")
    if (app.get("locked_artifacts", {}).get("contract_lock_sha256")
            or app.get("locked_artifacts", {}).get("contract_sha256")
            or app.get("effective", {}).get("contract_freeze", {}).get("status") == "completed"):
        raise ValueError("verification recovery cannot replace a frozen contract")
    if any(key in state.get("tasks", {}) for key in _DOWNSTREAM):
        raise ValueError("verification recovery cannot replace downstream attempts")
    if app.get("administrator_wait") or app.get("support_pending"):
        raise ValueError("verification recovery requires settled host waits")
    if app.get("stop_reason") not in (None, _ERROR):
        raise ValueError("verification recovery cannot override another stop reason")
    predecessor = "contract_restore" if header.get("inherit_harness") else "contract_draft"
    if app.get("effective", {}).get(predecessor, {}).get("status") != "completed":
        raise ValueError("verification recovery requires a completed contract input")
    task = state.get("tasks", {}).get(_STAGE)
    attempts = task.get("attempts", ()) if isinstance(task, dict) else ()
    if not attempts:
        raise ValueError("verification recovery requires an interrupted verification attempt")
    last = attempts[-1]
    result = (last.get("result") or {}).get("value")
    if (last.get("state") not in {"succeeded", "failed", "cancelled", "timed_out", "dead"}
            or not last.get("dispatched")
            or not isinstance(result, dict)
            or result.get("stage_id") != _STAGE
            or result.get("status") != "failed"
            or result.get("error_code") != _ERROR
            or result.get("command_id") != last.get("command", {}).get("execution_id")):
        raise ValueError("last verification attempt lacks the truthful interruption result")
    effective = app.get("effective", {}).get(_STAGE)
    if effective is not None and effective != result:
        raise ValueError("verification recovery disagrees with the recorded failure")
    deadline = ops._effective_deadline(header, app)
    if deadline is not None and ops.clock() >= deadline:
        raise ValueError("original Run deadline expired; verification cannot extend it")
    return app, last["command"]["execution_id"]


def _check_packet(root, packet, *, run_id, command_id, reason,
                  generation, revision, target_deployment=None):
    if (packet.get("schema_version") != 1 or packet.get("run_id") != run_id
            or packet.get("command_id") != command_id or packet.get("stage") != _STAGE
            or packet.get("reason") != reason
            or packet.get("source_generation") != generation - 1
            or packet.get("target_generation") != generation
            or (revision is not None and packet.get("source_revision") != revision)
            or (target_deployment is not None
                and packet.get("target_deployment") != target_deployment)):
        raise ValueError("verification recovery replay differs from prepared request")
    _verify_recovery_payload(root, packet)


def reopen_verification(ops, run_dir, run_id, reason):
    """Resume one truthful interrupted verification through public SDK reopen.

    A prepared packet is durable before invoking the SDK. The same reason may
    be replayed after either preparation or activation; a different request is
    rejected without creating another generation.
    """
    if not isinstance(reason, str) or not reason.strip():
        raise ValueError("verification recovery requires a reason")
    root = Path(run_dir).resolve()
    ops._header(root, run_id)
    check_storage_budget(root, phase="recover-reopen")
    packet_dir = root / "artifacts" / "recoveries"
    if packet_dir.exists():
        count = 0
        for candidate in packet_dir.glob(f"{_COMMAND_PREFIX}*/prepared.json"):
            count += 1
            if count > 4096:
                raise ValueError("verification recovery packet inspection exceeds its bound")
            packet = read_prepared_json(root, candidate.relative_to(root).as_posix())
            if packet.get("run_id") == run_id:
                _verify_recovery_payload(root, packet)

    with workspace_lock(root / ".locks" / "continuation", blocking=False), ops.session(
            root, run_id, allow_terminal_deployment=True) as (root, header, runtime, sdk):
        header = ops._header(root, run_id)
        if read_json(root / "run.json")["run_id"] != run_id:
            raise ValueError("only the current segment may be reopened")
        if (compile_migration_workflow(MigrationRequest.from_mapping(header["request"])).to_dict()
                != header["definition"]):
            raise ValueError("verification recovery cannot change the frozen workflow")
        if (root / "artifacts" / "continuations" / run_id / "successor.json").exists():
            raise ValueError("Run has a prepared successor")
        runtime.reap()
        sdk.sync()
        state = hydrate_run_snapshot(root, sdk.get_run(run_id))
        summary = sdk.get_run_summary(run_id)
        recovery = sdk.get_recovery(summary["recovery_id"]) if summary.get("recovery_id") else None
        generation = int(state.get("generation", 0))
        active_id = f"{_COMMAND_PREFIX}{generation}"
        if (state["state"] == "running" and recovery
                and recovery.get("command_id") == active_id
                and recovery.get("status") == "activated"):
            packet = read_prepared_json(root, f"artifacts/recoveries/{active_id}/prepared.json")
            _check_packet(root, packet, run_id=run_id, command_id=active_id,
                          reason=reason, generation=generation, revision=None,
                          target_deployment=recovery.get("target_deployment"))
            manifest = recovery.get("manifest") or {}
            if (packet["decision"] != recovery.get("decision")
                    or packet["application_state"] != recovery.get("application_state")
                    or packet["operations"] != manifest.get("operations")):
                raise ValueError("verification recovery differs from immutable SDK decision")
            sdk.flush()
            sdk.sync()
            return MigrationRun(run_id, root, hydrate_run_snapshot(root, sdk.get_run(run_id)))

        app, failed_execution_id = _validate_source(ops, header, state)
        target_generation = generation + 1
        command_id = f"{_COMMAND_PREFIX}{target_generation}"
        preflight = sdk.inspect_reopen(run_id, expected_revision=state["revision"],
                                       expected_generation=generation)
        if not preflight["complete"] and all(
                item["code"] in {"pending_result_delivery", "kernel_result_not_settled"}
                for item in preflight["blockers"]):
            ops._drain_result_delivery(sdk)
            preflight = sdk.inspect_reopen(run_id, expected_revision=state["revision"],
                                           expected_generation=generation)
        if not preflight["complete"]:
            codes = ", ".join(item["code"] for item in preflight["blockers"])
            raise ValueError(f"verification recovery blocked by SDK preflight: {codes}")
        if ops.handlers is None and ops.isolation_mode == "process":
            from .handlers import preflight_opencode_host
            model, effort = agent_model_policy(header["definition"]["workflow_version"], model_policy=header.get("model_policy"))
            preflight_opencode_host(root, model=model, reasoning_effort=effort)

        packet_path = packet_dir / command_id / "prepared.json"
        if packet_path.is_symlink() or packet_path.resolve() != packet_path.absolute():
            raise ValueError("unsafe verification recovery packet path")
        if packet_path.exists():
            packet = read_prepared_json(root, packet_path.relative_to(root).as_posix())
            _check_packet(root, packet, run_id=run_id, command_id=command_id,
                          reason=reason, generation=target_generation,
                          revision=state["revision"])
            if packet["target_deployment"].get("registry_revision") != runtime.registry_revision:
                raise ValueError("prepared recovery deployment changed")
        else:
            prepared_app = json_copy(app)
            prepared_app.setdefault("effective", {}).pop(_STAGE, None)
            prepared_app.setdefault("early_failures", {}).pop(_STAGE, None)
            prepared_app.update(
                active_stage=None, active_group=None, early_active=True,
                early_pending=[_STAGE], stop_reason=None, stop_state=None,
                terminal_reason=None, cancel_sent=[],
            )
            target_identity = inspect_runtime(root)["module"]
            target_deployment = {
                "registry_revision": runtime.registry_revision,
                "handler_revisions": {
                    (f"{key[0]}:{key[1]}" if isinstance(key, tuple) and len(key) == 2 else str(key)): value
                    for key, value in runtime.handler_revisions.items()
                },
                "sdk_identity": target_identity,
            }
            execution_header = json_copy(header)
            execution_header["registry_revision"] = runtime.registry_revision
            execution_header["sdk_identity"] = target_identity
            operations = ops._schedule(
                state, execution_header, prepared_app, _STAGE,
                activate=False, dependencies=[], causation_id=failed_execution_id,
            )
            if not any(operation["kind"] == "dispatch" for operation in operations):
                raise ValueError("verification recovery could not schedule a new attempt")
            if prepared_app.get("agent_assignments") != app.get("agent_assignments"):
                raise ValueError("verification recovery cannot change assignment budget")
            decision = {
                "start_stage": _STAGE,
                "reused_artifacts": sorted(prepared_app.get("effective", {})),
                "invalidated_artifacts": [_STAGE],
                "invalidated_effective_keys": [_STAGE],
                "budget_change": None,
            }
            stored_app = pack_application_state(root, prepared_app)
            decision["prepared_payload_sha256"] = _recovery_payload_digest(
                stored_app, operations, decision)
            packet = {
                "schema_version": 1, "run_id": run_id, "command_id": command_id,
                "stage": _STAGE, "reason": reason,
                "source_revision": state["revision"],
                "source_generation": generation,
                "target_generation": target_generation,
                "target_deployment": target_deployment,
                "decision": decision, "application_state": stored_app,
                "operations": operations,
            }
            atomic_json(packet_path, packet)
            _verify_recovery_payload(root, packet)
        sdk.reopen_run(
            run_id, command_id=command_id,
            expected_revision=state["revision"], expected_generation=generation,
            actor="modport-host", authorization_source="user-approved",
            reason=reason, target_deployment=packet["target_deployment"],
            decision=packet["decision"], application_state=packet["application_state"],
            operations=packet["operations"], owner_id="modport-reopen",
        )
        sdk.flush()
        sdk.sync()
        return MigrationRun(run_id, root, hydrate_run_snapshot(root, sdk.get_run(run_id)))
