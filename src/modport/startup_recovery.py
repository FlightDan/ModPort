"""Evidence gate for one pre-handler technical recovery per SDK task."""

from __future__ import annotations

from typing import Any, Mapping

from .run_monitor import process_identity_state


# No stage in this prefix may launch project/model processes. If a future
# change adds such work before handler_entered, the PID-only exit proof below
# is no longer sufficient and that stage must leave this allowlist.
_PRE_HANDLER_PHASES = frozenset({
    "worker_entered", "input_loading", "input_ready", "restoring_inputs",
    "waiting_memory", "waiting_workspace",
})


def classify_startup_timeout(sdk: Any, *, run_id: str, execution_id: str,
                             task_id: str, application_attempt: int,
                             command: Mapping[str, Any],
                             progress: Mapping[str, Any] | None,
                             process_isolated: bool,
                             already_recovered: bool,
                             current_registry_revision: str,
                             deadline_epoch: float | None,
                             now: float,
                             allow_settled_diagnosis: bool = False,
                             receipt_verified: bool = False,
                             settled_effect: Any = None,
                             expected_effect_request: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Classify a proved pre-handler retry or settled v25 coder diagnosis.

    The business handler marker is written strictly before Effect preparation
    or handler invocation. Absence of a marker is not evidence: the matching
    worker-entry/fence marker is required. SDK terminal timeout alone is not
    enough: this proof also requires the worker's exact PID/birth identity to
    be gone. A completed coder effect may only reach diagnosis, never retry.
    Pre-handler code is deliberately forbidden from spawning project/model
    subprocesses.
    """
    base = {
        "run_id": run_id, "task_id": task_id, "execution_id": execution_id,
        "application_attempt": application_attempt,
        "policy": "pre_handler_once_v1",
    }

    def required(reason: str, **facts: Any) -> dict[str, Any]:
        return {**base, "action": "recovery_required", "reason": reason, **facts}

    if already_recovered and not allow_settled_diagnosis:
        return required("automatic_recovery_already_used")
    if not process_isolated:
        return required("worker_tree_stop_not_proven")
    if not isinstance(progress, Mapping):
        return required("execution_progress_missing")
    if (progress.get("run_id") != run_id or progress.get("task_id") != task_id
            or progress.get("execution_id") != execution_id
            or progress.get("application_attempt") != application_attempt
            or progress.get("stage_id") != str(command.get("handler_id", "")).removeprefix("modport.")):
        return required("execution_progress_identity_mismatch")
    if progress.get("registry_revision") != command.get("registry_revision"):
        return required("execution_deployment_mismatch")
    settled_diagnosis = (allow_settled_diagnosis
                         and progress.get("phase") == "finished"
                         and receipt_verified)
    if progress.get("phase") not in _PRE_HANDLER_PHASES and not settled_diagnosis:
        return required("handler_or_model_may_have_started",
                        last_observed_phase=progress.get("phase"))
    if already_recovered and not settled_diagnosis:
        return required("automatic_recovery_already_used")
    kernel_attempt = progress.get("kernel_attempt")
    fence = progress.get("fence")
    if type(kernel_attempt) is not int or type(fence) is not int or kernel_attempt < 1 or fence < 1:
        return required("execution_fence_not_proven")
    if deadline_epoch is not None and now >= deadline_epoch:
        return required("original_run_deadline_exhausted")

    worker_pid = progress.get("worker_pid")
    worker_birth = progress.get("worker_birth")
    process_state = process_identity_state(worker_pid, worker_birth)
    if process_state is not False:
        return required("old_worker_process_exit_not_proven",
                        worker_process_state=("alive" if process_state is True else "unknown"))

    try:
        execution = sdk.inspect_execution(execution_id)
    except Exception as error:
        return required("sdk_execution_authority_unavailable",
                        inspection_error=type(error).__name__)
    result = getattr(execution, "result", None)
    result_status = getattr(result, "status", None)
    if (getattr(execution, "state", None) != "timed_out"
            or result_status != "timed_out"):
        return required("sdk_timeout_terminal_state_not_proven",
                        sdk_state=getattr(execution, "state", None),
                        result_status=result_status)
    if getattr(execution, "lease", None) is not None:
        return required("old_worker_lease_still_present")
    if getattr(result, "attempt", None) != kernel_attempt or getattr(result, "fence", None) != fence:
        return required("execution_fence_mismatch",
                        progress_kernel_attempt=kernel_attempt,
                        sdk_kernel_attempt=getattr(result, "attempt", None),
                        progress_fence=fence, sdk_fence=getattr(result, "fence", None))
    effect_ids = getattr(result, "effect_ids", None)
    expected_effect = f"modport:{execution_id}"
    if settled_diagnosis:
        if not isinstance(effect_ids, (list, tuple)) or list(effect_ids) != [expected_effect]:
            return required("settled_effect_identity_not_proven",
                            effect_ids=(list(effect_ids)[:20] if isinstance(effect_ids, (list, tuple)) else None))
        if getattr(getattr(result, "error", None), "code", None) != "handler_timeout":
            return required("sdk_handler_timeout_not_proven")
        if (settled_effect is None
                or not isinstance(expected_effect_request, Mapping)
                or getattr(settled_effect, "effect_id", None) != expected_effect
                or getattr(settled_effect, "execution_id", None) != execution_id
                or getattr(settled_effect, "name", None) != "modport.stage"
                or getattr(settled_effect, "state", None) != "committed"
                or getattr(settled_effect, "attempt", None) != kernel_attempt
                or getattr(settled_effect, "fence", None) != fence
                or getattr(settled_effect, "request", None) != expected_effect_request):
            return required("settled_effect_commit_not_proven")
    elif not isinstance(effect_ids, (list, tuple)) or effect_ids:
        return required("effect_state_not_proven_empty",
                        effect_ids=(list(effect_ids)[:20] if isinstance(effect_ids, (list, tuple)) else None))
    try:
        recoveries = sdk.inspect_recoveries(run_id)
    except Exception as error:
        return required("sdk_effect_authority_unavailable",
                        inspection_error=type(error).__name__)
    if any(getattr(getattr(item, "execution", None), "execution_id", None) == execution_id
           for item in recoveries):
        return required("sdk_effect_recovery_present")
    sdk_command = getattr(execution, "command", None)
    if (sdk_command is None
            or sdk_command.execution_id != execution_id
            or sdk_command.registry_revision != current_registry_revision
            or sdk_command.handler_id != command.get("handler_id")
            or sdk_command.correlation_id != run_id):
        return required("sdk_command_identity_mismatch")

    if settled_diagnosis:
        return {
            **base,
            "policy": "settled_coder_diagnosis_v1",
            "action": "diagnose", "reason": "settled_coder_handler_timeout",
            "last_observed_phase": progress.get("phase"),
            "kernel_attempt": kernel_attempt, "fence": fence,
            "worker_pid": worker_pid, "worker_birth": worker_birth,
            "worker_stopped_proof": "sdk_timeout_terminal_and_worker_birth_gone",
            "effects_proof": "matching_effect_id_no_open_recovery_and_valid_stage_receipt",
            "effect_id": expected_effect, "effect_state": "committed",
            "effect_input_sha256": expected_effect_request.get("input_sha256"),
            "registry_revision": current_registry_revision,
        }

    return {
        **base,
        "action": "retry",
        "reason": "pre_handler_timeout_proven",
        "last_observed_phase": progress.get("phase"),
        "progress_last_updated_at": progress.get("last_progress_at"),
        "kernel_attempt": kernel_attempt,
        "fence": fence,
        "worker_pid": progress.get("worker_pid"),
        "worker_birth": progress.get("worker_birth"),
        "worker_stopped_proof": "sdk_timeout_terminal_and_pre_handler_worker_birth_gone",
        "effects_proof": "handler_marker_not_entered_and_sdk_effect_ids_empty",
        "registry_revision": current_registry_revision,
    }


__all__ = ["classify_startup_timeout"]
