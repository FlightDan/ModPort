"""Settle only an exact supervisor-authorized, reaped SDK cancellation."""
from pathlib import Path

from .application_state_storage import hydrate_run_snapshot
from .contracts import OperationInput, OperationResult, json_copy
from .evidence import atomic_json, read_json, workspace_lock
from .kernel_runtime import operation_lock, repository_facts, validate_stage_response
from .payload_storage import pack_result, unpack_input
from .watchdog_events import enabled
from .watchdog_supervisor import validate_watchdog_decision


_PROOF = ("request_committed", "command_delivered", "execution_authority_revoked",
          "local_process_tree_reaped", "cleanup")


def _authorization(snapshot, operation, reason):
    state = (snapshot.get("application_state") or {}).get("watchdog", {})
    for identity, episode in state.get("episodes", {}).items():
        if episode.get("status") not in {"cancellation_authorized", "cancelling"}:
            continue
        request = episode.get("request") or {}
        if (request.get("target_task_id") != operation.task_id
                or request.get("target_execution_id") != operation.command_id):
            continue
        supervisor = snapshot.get("tasks", {}).get(episode.get("supervisor_task_id"))
        attempts = supervisor.get("attempts") if isinstance(supervisor, dict) else None
        if not attempts:
            continue
        attempt = attempts[-1]
        result = (attempt.get("result") or {}).get("value")
        if (attempt.get("state") != "succeeded" or not isinstance(result, dict)
                or result.get("status") != "completed"
                or result.get("stage_id") != "supervisor"
                or result.get("task_id") != episode.get("supervisor_task_id")
                or result.get("command_id") != attempt.get("command", {}).get("execution_id")):
            continue
        try:
            decision = validate_watchdog_decision(result.get("outputs", {}).get("watchdog_decision"), request)
        except (ValueError, TypeError, KeyError):
            continue
        if decision["action"] != "repair_resume" or episode.get("decision") != decision:
            continue
        expected_reason = "watchdog: " + decision["reason"]
        if reason != expected_reason:
            continue
        return {"incident_id": identity, "supervisor_task_id": episode["supervisor_task_id"],
                "supervisor_execution_id": result["command_id"], "request": json_copy(request),
                "decision": decision, "recovery_reason": reason}
    return None


def _evidence(root, header, sdk, snapshot, recovery, operation):
    execution, effect = recovery.execution, recovery.effect
    if (recovery.run_id != header["run_id"] or recovery.task_id != operation.task_id
            or operation.run_id != header.get("logical_run_id", recovery.run_id)
            or operation.command_id != execution.command.execution_id
            or execution.command.handler_id != "modport." + operation.stage_id
            or execution.recovery_target_state != "cancelled"
            or effect.execution_id != operation.command_id
            or effect.effect_id != "modport:" + operation.command_id or effect.name != "modport.stage"):
        return None
    authority = _authorization(snapshot, operation, execution.recovery_reason)
    if authority is None:
        return None
    report = sdk.inspect_cancellation(recovery.run_id, execution_id=operation.command_id)
    if report.truncated or len(report.executions) != 1:
        return None
    entry = report.executions[0]
    if (entry.issues or entry.effects_truncated or entry.receipts_truncated
            or entry.task_id != recovery.task_id or entry.application_attempt != recovery.attempt
            or entry.execution_id != operation.command_id or entry.execution_revision != execution.revision
            or entry.kernel_attempt != effect.attempt or entry.fence != effect.fence
            or entry.execution_state != "recovery_required" or entry.task_state != "recovery_required"
            or entry.execution_result_known
            or any(getattr(entry, field).status != "confirmed" for field in _PROOF)
            or not any(row.get("effect_id") == effect.effect_id and row.get("revision") == effect.revision
                       and row.get("attempt") == effect.attempt and row.get("fence") == effect.fence
                       and row.get("state") == "indeterminate" for row in entry.effects)):
        return None
    return {"schema": "modport.watchdog-assignment-cancellation.v1",
        "run_id": operation.run_id, "task_id": operation.task_id, "stage_id": operation.stage_id,
        "execution_id": operation.command_id, "effect_id": effect.effect_id,
        "effect_revision": effect.revision, "kernel_attempt": effect.attempt, "fence": effect.fence,
        "application_attempt": recovery.attempt, "supervisor_authorization": authority,
        "cancellation_receipt_ids": list(entry.receipt_ids),
        "proof": {field: getattr(entry, field).status for field in _PROOF},
        "external_outcome": "unknown", "artifacts_complete": False, "acceptance_status": "unverified"}


def _receipt(root, command, effect, operation, evidence):
    directory = root / "artifacts" / "executions" / operation.command_id
    if directory.resolve() != directory.absolute() or not directory.resolve().is_relative_to(root):
        raise ValueError("watchdog cancellation receipt directory is not contained")
    receipt = directory / "receipt.json"
    expected = dict(effect.request)  # Copy the host's existing provenance; do not recompute it.
    if receipt.exists() or receipt.is_symlink():
        if receipt.is_symlink():
            raise ValueError("watchdog cancellation receipt is a symlink")
        response = read_json(receipt).get("response")
        return pack_result(root, validate_stage_response(root, operation, response, expected))
    note = directory / "watchdog-cancelled-assignment.json"
    if note.is_symlink():
        raise ValueError("watchdog cancellation note is a symlink")
    if note.exists():
        previous = read_json(note)
        # SDK cleanup may append receipts after a crash. Keep the original
        # proved note; recovery identity and authorization must still match.
        stable = lambda value: {key: item for key, item in value.items() if key != "cancellation_receipt_ids"}
        if stable(previous) != stable(evidence):
            raise ValueError("watchdog cancellation note differs from the proved cancellation")
    else:
        atomic_json(note, evidence)
    response = OperationResult("failed", operation.run_id, operation.task_id, operation.stage_id,
        operation.command_id, outputs={"watchdog_cancelled": True, "agent_cancelled": True,
            "watchdog_incident_id": evidence["supervisor_authorization"]["incident_id"],
            "external_outcome": "unknown", "artifacts_complete": False, "acceptance_status": "unverified",
            "workspace_changes": "unknown_unaccepted", "partial_outputs_unaccepted": True,
            "artifact_refs": {"watchdog_cancelled_assignment": {"path": note.relative_to(root).as_posix()}}},
        error_code="watchdog_assignment_interrupted",
        detail="supervisor cancelled this exact execution; partial work retained and unaccepted").to_dict()
    atomic_json(receipt, {"execution_id": operation.command_id, "effect_request": expected,
                         "response": response, "after": repository_facts(root, operation)})
    return pack_result(root, validate_stage_response(root, operation, response, expected))


def settle(root, header, sdk, snapshot=None):
    """Resolve authorized Effects through public SDK APIs; uncertainty stays parked."""
    root = Path(root).resolve()
    if snapshot is None:
        sdk.sync()
        snapshot = hydrate_run_snapshot(root, sdk.get_run(header["run_id"]))
    if not enabled(header, snapshot.get("application_state")):
        return False
    episodes = (snapshot.get('application_state') or {}).get('watchdog', {}).get('episodes', {})
    if not any(row.get('status') in {'cancellation_authorized', 'cancelling'}
               for row in episodes.values()):
        return False
    changed = False
    while True:
        settled = False
        for recovery in sdk.inspect_recoveries(header["run_id"]):
            command = recovery.execution.command.to_dict()
            operation = OperationInput.from_dict(unpack_input(root, command["payload"]))
            evidence = _evidence(root, header, sdk, snapshot, recovery, operation)
            if evidence is None:
                continue
            try:
                with workspace_lock(operation_lock(root, operation), blocking=False):
                    response = _receipt(root, command, recovery.effect, operation, evidence)
                    sdk.resolve_effect(recovery.effect.effect_id, decision="applied", response=response,
                        expected_revision=recovery.effect.revision,
                        recovery_id=("watchdog-settlement:" + recovery.effect.effect_id + ":"
                                     + str(recovery.effect.revision) + ":"
                                     + evidence["supervisor_authorization"]["incident_id"]))
            except BlockingIOError:
                continue
            sdk.sync()
            snapshot = hydrate_run_snapshot(root, sdk.get_run(header["run_id"]))
            changed = settled = True
            break
        if not settled:
            return changed
