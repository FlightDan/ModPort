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
_CONTROL_PROOF = _PROOF[:3]
_CLEANUP_PROOF = _PROOF[3:]


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
                or result.get("run_id") != operation.run_id
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
            or any(getattr(entry, field).status != "confirmed" for field in _CONTROL_PROOF)
            or not any(row.get("effect_id") == effect.effect_id and row.get("revision") == effect.revision
                       and row.get("attempt") == effect.attempt and row.get("fence") == effect.fence
                       and row.get("state") == "indeterminate" for row in entry.effects)):
        return None
    independent_cleanup = None
    if any(getattr(entry, field).status != "confirmed" for field in _CLEANUP_PROOF):
        # A reopened Runtime has no handle for the old driver. Preserve its
        # unknown cancellation cleanup facts, while independently rechecking
        # the original SDK receipt and registered driver under exact authority.
        if (any(getattr(entry, field).status not in {"confirmed", "unknown"}
                for field in _CLEANUP_PROOF) or sdk.runtime is None):
            return None
        from .interrupted_execution import stopped_execution_evidence
        independent_cleanup = stopped_execution_evidence(root, header, sdk.runtime,
            execution, task_id=operation.task_id, snapshot=snapshot, timeout=.5)
        task = snapshot.get("tasks", {}).get(operation.task_id) or {}
        attempts = task.get("attempts") or []
        current_attempt = attempts[-1] if attempts else {}
        if (independent_cleanup.get("confirmed") is not True
                or len(attempts) - 1 != recovery.attempt
                or current_attempt.get("command", {}).get("execution_id") != operation.command_id
                or current_attempt.get("state") != "recovery_required"
                or independent_cleanup.get("generation") != current_attempt.get(
                    "generation", snapshot.get("generation", 0))):
            return None
        current = sdk.runtime.kernel.get(operation.command_id)
        if (current.state != execution.state or current.revision != execution.revision
                or current.attempt != effect.attempt or current.fence != effect.fence):
            return None
    evidence = {"schema": "modport.watchdog-assignment-cancellation.v1",
        "run_id": operation.run_id, "task_id": operation.task_id, "stage_id": operation.stage_id,
        "execution_id": operation.command_id, "effect_id": effect.effect_id,
        "effect_revision": effect.revision, "kernel_attempt": effect.attempt, "fence": effect.fence,
        "application_attempt": recovery.attempt, "supervisor_authorization": authority,
        "cancellation_receipt_ids": list(entry.receipt_ids),
        "proof": {field: getattr(entry, field).status for field in _PROOF},
        "external_outcome": "unknown", "artifacts_complete": False, "acceptance_status": "unverified"}
    if independent_cleanup is not None:
        evidence["independent_cleanup"] = independent_cleanup
    return evidence


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
        # A crash may leave the original note before the response is written.
        # Fresh SDK proof can improve unknown cleanup to confirmed; retain the
        # original evidence while requiring the same identity/authorization.
        stable = lambda value: {key: item for key, item in value.items()
                                if key not in {"cancellation_receipt_ids", "proof", "independent_cleanup"}}
        old_proof, new_proof = previous.get("proof") or {}, evidence.get("proof") or {}
        monotonic = (set(old_proof) == set(new_proof) == set(_PROOF)
                     and all(old_proof[field] == new_proof[field]
                             or (old_proof[field] == "unknown" and new_proof[field] == "confirmed")
                             for field in _PROOF))
        old_cleanup = previous.get("independent_cleanup")
        new_cleanup = evidence.get("independent_cleanup")
        cleanup_keys = ("confirmed", "execution_id", "attempt", "fence", "generation", "driver", "driver_state", "cleanup")
        same_cleanup = (not old_cleanup or not new_cleanup or
                        all(old_cleanup.get(key) == new_cleanup.get(key) for key in cleanup_keys))
        retained_cleanup = (not old_cleanup or new_cleanup is not None
                            or all(new_proof.get(field) == "confirmed" for field in _CLEANUP_PROOF))
        if (stable(previous) != stable(evidence) or not monotonic
                or not same_cleanup or not retained_cleanup):
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
