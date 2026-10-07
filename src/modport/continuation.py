"""Explicit same-workspace continuation using public SDK execution segments."""
from hashlib import sha256
import json
from pathlib import Path
import re
from uuid import uuid4

from dispatcher_sdk.orchestrator import Orchestrator, Operations

from .contracts import OperationInput, OperationResult, json_copy
from .evidence import atomic_json, digest, read_json, seal_ref, verified_path, workspace_lock
from .kernel_runtime import open_runtime
from .payload_storage import pack_input, unpack_input, verify_operations, read_prepared_json, is_packed_input
from .storage_budget import check_storage_budget
from .sdk_compat import inspect_runtime
from .repair_evidence import snapshot_repair_evidence
from .repair_reset import invalidate_contract_tail, require_settled_review_rework
from .workflow import REVIEW_STAGES
from .application_state_storage import (
    hydrate_run_snapshot,
    is_packed_application_state,
    pack_application_state,
    unpack_application_state,
)

_INHERIT_DEADLINE = object()


_SETTLED = frozenset({"succeeded", "failed", "cancelled", "timed_out", "dead"})
_PREPARED_PAYLOAD_SCHEMA = "modport.continuation-prepared-payload.v1"


def _retire_planned_successor(root, sdk, old_id, new_id, source_id, current):
    """Retire a strictly un-dispatched successor before replacing it.

    A pending_dispatch attempt already has a global SDK outbox entry and must
    be settled separately. The SDK command receipt makes a crash after the
    cancellation, but before the new Run is created, safely replayable.
    """
    unstarted = sdk.get_run(old_id)
    if (current.get('continuation', {}).get('previous_run_id') != source_id
            or unstarted['input'] != current or unstarted['waits']):
        raise ValueError('replaced successor SDK identity or waits changed')
    old_archive = root / 'artifacts' / 'continuations' / old_id / 'run.json'
    if old_archive.is_symlink() or old_archive.resolve() != old_archive.absolute():
        raise ValueError('unsafe replaced header archive')
    if old_archive.exists() and read_json(old_archive) != current:
        raise ValueError('replaced segment archive differs from SDK input')
    command_id = new_id + ':replace-planned-predecessor'
    if unstarted['state'] == 'cancelled':
        if sdk.get_command_receipt(old_id, command_id) is None:
            raise ValueError('replaced successor was cancelled by a different SDK request')
    elif unstarted['state'] == 'running':
        attempts = [attempt for task in unstarted['tasks'].values()
                    for attempt in task['attempts']]
        if any(attempt.get('state') != 'planned' or attempt.get('result') is not None
               for attempt in attempts):
            raise ValueError('replacement requires a direct successor with no dispatched tasks; '
                             'cancel or settle its SDK work before replacement')
        atomic_json(old_archive, current)
        sdk.apply_operations(old_id, command_id=command_id,
            expected_revision=unstarted['revision'],
            operations=[*(Operations.cancel(task_id,
                reason='Superseded by bounded successor ' + new_id)
                for task_id in unstarted['tasks']), Operations.finish('cancelled')])
    else:
        raise ValueError('replacement requires an unstarted direct successor')
    atomic_json(old_archive, current)


def _prepared_payload_digest(application_state, operations):
    return digest({
        "schema": _PREPARED_PAYLOAD_SCHEMA,
        "application_state": application_state,
        "operations": operations,
    })


def _verify_prepared_payload(root, packet):
    """Verify packed state and its immutable successor-header binding."""
    stored = packet.get("application_state")
    operations = packet.get("operations")
    header = packet.get("header")
    if not isinstance(stored, dict) or not isinstance(operations, list) or not isinstance(header, dict):
        raise ValueError("malformed prepared continuation payload")
    expected_header = dict(header)
    header_digest = expected_header.pop("header_sha256", None)
    if not isinstance(header_digest, str) or header_digest != digest(expected_header):
        raise ValueError("prepared continuation header digest mismatch")
    # Blob availability and content are checked before create_run,
    # continue_run, or apply_operations can mutate SDK storage.
    unpack_application_state(root, stored)
    verify_operations(root, operations)
    continuation = header.get("continuation")
    declared = (continuation.get("prepared_payload_sha256")
                if isinstance(continuation, dict) else None)
    if declared is None:
        # Frozen packets created before application-state compaction remain
        # replayable while inline. A packed packet can only have been created
        # by the new protocol and therefore must carry the binding.
        if is_packed_application_state(stored) or any(
                is_packed_input((item.get("command") or {}).get("payload"))
                for item in operations):
            raise ValueError("packed continuation payload has no immutable binding")
        return
    if declared != _prepared_payload_digest(stored, operations):
        raise ValueError("prepared continuation payload digest mismatch")


def _host_interface(root, segment_id):
    from .host_interface import publish_host_interface
    return publish_host_interface(root, "continuation-support:" + segment_id)


def _upgrade_watchdog_policy(root, header):
    """Carry the user's durable watchdog stop into the new execution segment."""
    inherited = header.get('watchdog_policy')
    policy = (json_copy(inherited) if isinstance(inherited, dict) else
              {'enabled': True, 'inactivity_seconds': 600})
    control = Path(root) / 'desktop-watchdog-control.json'
    if not control.exists():
        return policy, None
    try:
        from .desktop_state import read_json as read_desktop_json
        value = read_desktop_json(control, limit=65536)
        if value.get('instance') == Path(root).name and value.get('suppressed') is True:
            policy['enabled'] = False
        return policy, None
    except (OSError, ValueError, TypeError, AttributeError) as error:
        # An unreadable control is diagnostic, never permission to reenable
        # supervision or a prerequisite for continuing the migration.
        policy['enabled'] = False
        return policy, str(error)


def _v15_contract_plan_checkpoint(root, app):
    """Return whether a persisted Markdown plan can accept another review pass."""
    context = app.get("repair_context")
    documents = app.get("plan_documents")
    state = documents.get("contract") if isinstance(documents, dict) else None
    if (not isinstance(context, dict) or not isinstance(context.get("run_id"), str)
            or not context["run_id"] or not isinstance(state, dict)):
        return False
    ref = state.get("current_ref")
    metadata = ref.get("metadata") if isinstance(ref, dict) else None
    rounds = state.get("rounds")
    expected = {
        "document_kind": "modport-planning-markdown-v1",
        "run_id": context.get("run_id"),
        "scope": "contract",
        "repair_generation": app.get("repair_generation"),
        "failure_execution_id": context.get("failure_execution_id"),
        "revision": rounds,
        "status": "continue",
    }
    if (rounds not in {1, 2} or state.get("status") != "continue"
            or state.get("repair_generation") != app.get("repair_generation")
            or state.get("planning_generation") != app.get("planning_generation")
            or not isinstance(metadata, dict)
            or any(metadata.get(key) != value for key, value in expected.items())
            or ref.get("media_type") != "text/markdown"):
        return False
    try:
        raw = verified_path(Path(root), ref).read_bytes()
    except (OSError, ValueError, KeyError, TypeError):
        return False
    return sha256(raw).hexdigest() == ref.get("sha256")


def _preserve_diagnosis(root, app, diagnosis):
    """Retain legacy diagnosis references after removing its effective projection."""
    preserved = snapshot_repair_evidence(root, diagnosis)
    command_id = preserved.get("command_id")
    if isinstance(command_id, str):
        app.setdefault("repair_cycle_results", {}).setdefault(command_id, preserved)
        aliases = app.setdefault("rework_evidence", {})
        for key, ref in preserved.get("outputs", {}).get("artifact_refs", {}).items():
            aliases.setdefault(f"rework_evidence:{command_id}:{key}", ref)


def _attempt_outcome(attempt):
    """Authenticate or reconstruct the business result of a settled SDK attempt."""
    command = OperationInput.from_dict(attempt["command"]["payload"])
    if attempt["state"] == "succeeded":
        value = (attempt.get("result") or {}).get("value")
        if not isinstance(value, dict):
            raise ValueError("settled continuation attempt has no business result")
        result = OperationResult.from_dict(value)
        result.validate_for(command)
        return result
    return OperationResult(
        "blocked", command.run_id, command.task_id, command.stage_id,
        command.command_id, error_code="execution_" + attempt["state"],
    )


def _is_failure(result):
    return (result.status != "completed"
            or (result.stage_id in REVIEW_STAGES
                and result.outputs.get("verdict") != "approved"))


def _result_mapping(value):
    """Return an authenticated result mapping, or None for scalar legacy state."""
    if not isinstance(value, dict):
        return None
    try:
        return OperationResult.from_dict(value)
    except (TypeError, ValueError):
        return None


def _v16_terminal_failure(state, app):
    """Select the latest attributable failure without inventing a new author."""
    attempts = {}
    for task in state.get("tasks", {}).values():
        for attempt in task.get("attempts", []):
            if attempt.get("state") not in _SETTLED:
                continue
            try:
                command = OperationInput.from_dict(attempt["command"]["payload"])
            except (KeyError, TypeError, ValueError):
                continue
            attempts[command.command_id] = attempt

    # A handoff that was prepared in the previous segment remains the clearest
    # explanation of what blocked progress. It is recreated in the new segment;
    # its old consumer task is never rerun.
    for record in reversed(list(app.get("gate_diagnostics", {}).values())):
        result = _result_mapping(record.get("current_result") or record.get("result"))
        if result is not None and record.get("state") in {"pending", "running"}:
            return result, record.get("location", "main")

    # History is the persisted ordering authority. Prefer the latest failing
    # attempt represented there over dictionary insertion order in SDK tasks.
    for row in reversed(app.get("history", [])):
        attempt = attempts.get(row.get("execution_id"))
        if attempt is None:
            continue
        result = _attempt_outcome(attempt)
        if _is_failure(result):
            return result, _failure_location(app, result)

    for attempt in reversed(list(attempts.values())):
        result = _attempt_outcome(attempt)
        if _is_failure(result):
            return result, _failure_location(app, result)

    # Older application states sometimes retained the result after SDK task
    # pruning. These mappings still carry the complete business identity.
    for value in reversed(list(app.get("early_failures", {}).values())):
        result = _result_mapping(value)
        if result is not None and _is_failure(result):
            return result, "early"
    for value in reversed(list(app.get("effective", {}).values())):
        result = _result_mapping(value)
        if result is not None and _is_failure(result):
            return result, _failure_location(app, result)

    # v12-v15 repair contexts predate generic continuation. Some contain the
    # failed execution identity and findings after the SDK task was pruned.
    context = app.get("repair_context")
    current_failure = context.get("current_failure") if isinstance(context, dict) else None
    if isinstance(current_failure, dict):
        execution_id = (current_failure.get("execution_id")
                        or context.get("failure_execution_id"))
        value = current_failure.get("result")
        result = _result_mapping(value)
        if result is not None and _is_failure(result):
            return result, _failure_location(app, result)
        if isinstance(execution_id, str) and execution_id:
            findings = current_failure.get("findings", [])
            stage = next((row.get("stage") for row in findings
                          if isinstance(row, dict) and isinstance(row.get("stage"), str)), None)
            if stage is None:
                pieces = execution_id.split(":")
                stage = pieces[-2] if len(pieces) >= 3 else None
            if not isinstance(stage, str) or not stage:
                stage = ("contract_review" if context.get("repair_scope") == "contract"
                         else "target_build")
            run_id = state.get("input", {}).get(
                "logical_run_id", state.get("run_id", ""))
            outputs = value.get("outputs", {}) if isinstance(value, dict) else {}
            result = OperationResult(
                "blocked", run_id, stage, stage, execution_id,
                outputs=outputs,
                error_code=(app.get("terminal_reason") or app.get("stop_reason")
                            or "continuation_terminal_failure"),
            )
            return result, _failure_location(app, result)

    # A successful operation can still expose an invalid planning envelope. Its
    # terminal reason is a diagnostic about that exact result and identity.
    reason = app.get("terminal_reason") or app.get("stop_reason")
    for row in reversed(app.get("history", [])):
        attempt = attempts.get(row.get("execution_id"))
        if attempt is None:
            continue
        result = _attempt_outcome(attempt)
        return OperationResult(
            "blocked", result.run_id, result.task_id, result.stage_id,
            result.command_id, result.outputs, error_code=reason or "run_failed",
        ), _failure_location(app, result)
    raise ValueError("v16 continuation requires an attributable terminal result")


def _failure_location(app, result):
    from .workflow import EARLY_STAGES, SUPPORT_STAGES
    identities = {result.stage_id, result.task_id}
    if ((result.task_id.startswith(("coder.", "goal."))
         or result.stage_id in {"coder", "goal_prepare"})
            and (app.get("active_group") or app.get("failed_development_group"))):
        return "group"
    if identities & set(app.get("gap_pending") or []):
        return "gap"
    if identities & set(app.get("early_pending") or []):
        return "early"
    if (app.get("early_active") and result.stage_id in {
            *EARLY_STAGES, "platform_diff", "java_diff",
            "platform_skill_review", "java_skill_review", "gap_research",
            "research_review", "knowledge_publish", "mod_scan", "mod_analysis"}):
        return "early"
    if result.stage_id == "gap_research" and app.get("gap_failure"):
        return "gap"
    if (result.stage_id in {"research_review", "knowledge_publish", "mod_scan", "mod_analysis"}
            and (app.get("gap_failure") or app.get("gap_join_stage")
                 or app.get("gap_rework"))):
        return "gap"
    if result.stage_id in SUPPORT_STAGES:
        return "support"
    if app.get("early_active") or result.stage_id in EARLY_STAGES:
        return "early"
    return "main"


def _budget_exhausted_development_group(state, app):
    """Rebuild a v17 development group after an interrupting coder boundary.

    Budget exhaustion is an operational boundary.  The terminal segment has
    already cleared its live group, but settled goal preparations are valid
    inputs for the next explicitly budgeted segment and must not be rerun.
    A coder isolation result is different: the isolated workspace has already
    rejected the unsafe or conflicting output.  In v17 it is a retained
    diagnostic, so the continuation needs the same reconstruction in order to
    integrate every available patch and continue the remaining tasks.
    """
    budget_reasons = {'agent_assignment_budget_exhausted', 'wall_clock_budget_exhausted',
                      'budget_exhausted'}
    budget_exhausted = (app.get('terminal_reason') in budget_reasons
                        or app.get('stop_reason') in budget_reasons)
    isolated_coder = (app.get("terminal_reason") == "coder_isolation_violation"
                      or app.get("stop_reason") == "coder_isolation_violation")
    if not budget_exhausted and not isolated_coder:
        return None
    implementation = _result_mapping(app.get("effective", {}).get("implementation"))
    if implementation is None or implementation.status != "completed":
        return None
    outputs = implementation.outputs
    tasks = outputs.get("development_tasks")
    base = outputs.get("development_base")
    refs = outputs.get("artifact_refs")
    if (not isinstance(tasks, list) or not tasks or not isinstance(base, str)
            or not isinstance(refs, dict)):
        return None
    identifiers = {task.get("id") for task in tasks if isinstance(task, dict)}
    if len(identifiers) != len(tasks) or not all(isinstance(item, str) and item for item in identifiers):
        return None
    generation = app.get("development_generation")
    if type(generation) is not int or generation <= 0:
        generation = 1
    results = {}
    prepared = []
    scheduled = []
    interrupted = []
    interrupted_setup = []
    identity_diagnostics = []
    # A later budget segment may contain only the newly dispatched tasks.
    # Earlier completed producers remain authenticated application inputs.
    for key, value in app.get('effective', {}).items():
        result = _result_mapping(value)
        if result is None or result.stage_id not in {'coder', 'goal_prepare'}:
            continue
        prefix = 'goal' if result.stage_id == 'goal_prepare' else 'coder'
        identifier = key.removeprefix(f'{prefix}.g{generation}.')
        if key != f'{prefix}.g{generation}.{identifier}' or identifier not in identifiers:
            continue
        if (result.task_id != key
                or result.outputs.get('development_task_id', identifier) != identifier):
            identity_diagnostics.append({'task_id': key, 'reason': 'carried_result_identity_mismatch'})
            continue
        if (result.stage_id == 'goal_prepare' and result.status == 'completed'):
            results[key] = result.to_dict()
            prepared.append(identifier)
        elif (result.stage_id == 'coder' and
              (result.status == 'completed' or result.outputs.get('artifact_refs', {}).get('coder_patch'))):
            results[key] = result.to_dict()
            scheduled.append(identifier)
    for task_id, task in state.get("tasks", {}).items():
        attempts = task.get("attempts") if isinstance(task, dict) else None
        if not isinstance(attempts, list) or not attempts:
            continue
        attempt = attempts[-1]
        try:
            command = OperationInput.from_dict(attempt["command"]["payload"])
            result = _attempt_outcome(attempt)
        except (KeyError, TypeError, ValueError):
            continue
        if command.stage_id not in {"goal_prepare", "coder"}:
            continue
        development_task = command.payload.get('development_task')
        if not isinstance(development_task, dict):
            identity_diagnostics.append({'task_id': task_id, 'reason': 'missing_task_identity'})
            continue
        identifier = result.outputs.get("development_task_id")
        if identifier not in identifiers:
            identifier = development_task.get("id")
        if identifier not in identifiers or not isinstance(task_id, str):
            continue
        prefix = "goal" if command.stage_id == "goal_prepare" else "coder"
        expected = f"{prefix}.g{generation}.{identifier}"
        if (task_id != expected or command.task_id != expected or result.task_id != expected
                or development_task.get('id') != identifier
                or result.outputs.get('development_task_id', identifier) != identifier
                or result.stage_id != command.stage_id or result.command_id != command.command_id
                or result.run_id != command.run_id):
            identity_diagnostics.append({'task_id': task_id, 'reason': 'attempt_identity_mismatch'})
            continue
        if command.stage_id == "goal_prepare" and not isolated_coder and result.status != "completed":
            continue
        if (isolated_coder and command.stage_id == 'coder'
                and result.error_code == 'coder_isolation_violation'
                and not result.outputs.get('artifact_refs', {}).get('coder_patch')):
            # Explicit continuation re-enters setup with the current host.
            # Preserve the stopped attempt; fresh setup still enforces safety
            # and classifies dependency merge conflicts before invoking AI.
            interrupted_setup.append({'task_id': identifier, 'command_id': command.command_id,
                                      'workspace': command.options.get('workspace')})
            continue
        if (budget_exhausted and command.stage_id == 'coder'
                and result.status != 'completed'
                and (result.error_code in budget_reasons
                     or attempt.get('state') in {'cancelled', 'timed_out'})
                and not result.outputs.get('artifact_refs', {}).get('coder_patch')):
            # A new explicitly budgeted continuation resumes unfinished work.
            # Keep its old checkout intact; use a fresh segment workspace.
            interrupted.append({'task_id': identifier, 'command_id': command.command_id,
                                'workspace': command.options.get('workspace')})
            continue
        if command.stage_id == 'coder' and (
                (app.get('effective', {}).get(task_id) or {}).get('error_code')
                == 'rework_execution_failed'):
            # A successful first author attempt remains authenticated, but a
            # later explicit rework failed. Requeue the author with that
            # request and carry the first patch separately; treating this
            # SDK attempt as the effective result would lose the repair.
            continue
        results[task_id] = result.to_dict()
        if command.stage_id == "goal_prepare":
            prepared.append(identifier)
        else:
            scheduled.append(identifier)
    if not results:
        return None
    context = {"development_plan": refs["development_plan"]} if "development_plan" in refs else {}
    return {
        "kind": "development", "generation": generation, "tasks": json_copy(tasks),
        "base": base, "parallel": True, "members": [], "scheduled": sorted(set(scheduled)),
        "results": results, "goal_scheduled": sorted(set(prepared)),
        "entry_stage": "implementation", "goal_scope": "migration",
        "planning_context": context, "artifact_refs": json_copy(refs),
        "execution_payload": {
            **{key: json_copy(value) for key, value in outputs.items()
               if key not in {"development_tasks", "development_base", "artifact_refs"}},
            'development_workspace_epoch': sha256(
                (state['run_id'] + ':' + str(state.get('revision'))).encode()).hexdigest()[:16],
            'interrupted_development_work': interrupted,
            'interrupted_development_setup': interrupted_setup,
            'continuation_identity_diagnostics': identity_diagnostics,
            'carried_development_results': {
                result['command_id']: sha256(json.dumps(
                    result, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
                for key, result in results.items() if key.startswith('coder.')},
        },
    }


def _v17_diagnostic_evidence_view(root, value):
    """Quarantine every unavailable evidence-shaped value before v17 copying.

    Older result envelopes sometimes store an artifact directly in an output
    field instead of under ``artifact_refs``.  A v17 continuation may retain
    that observation but must not make the next execution depend on a report
    file that a completed coder intentionally removed from its workspace.
    """
    if isinstance(value, list):
        return [_v17_diagnostic_evidence_view(root, item) for item in value]
    if not isinstance(value, dict):
        return value
    if isinstance(value.get("path"), str) and isinstance(value.get("sha256"), str):
        try:
            verified_path(Path(root), value)
        except (OSError, ValueError, KeyError, TypeError) as error:
            return {
                "unavailable_evidence": {
                    "path": value.get("path"), "original_sha256": value.get("sha256"),
                    "diagnostic": str(error),
                },
            }
    return {key: _v17_diagnostic_evidence_view(root, child)
            for key, child in value.items()}


def _v17_checkpoint(app, start_stage):
    """Return the explicit unverified checkpoint for the requested successor."""
    for key, value in app.items():
        if (not key.endswith("_checkpoint") or not isinstance(value, dict)
                or value.get("acceptance_status") != "unverified"
                or value.get("next_stage") != start_stage):
            continue
        return value
    return None


def _prepare_v16_application(root, state, app):
    failure, location = _v16_terminal_failure(state, app)
    prior_terminal_reason = app.get("terminal_reason") or app.get("stop_reason")
    prior_group = snapshot_repair_evidence(
        root, app.get("active_group") or app.get("failed_development_group"))
    prior_controls = {
        "active_stage": app.get("active_stage"),
        "early_active": app.get("early_active", False),
        "gap_join_stage": app.get("gap_join_stage"),
        "gap_join_ids": json_copy(app.get("gap_join_ids")),
    }
    failures = json_copy(app.get("continuation_feedback", {}).get("failed_results", []))
    if not any(row.get("command_id") == failure.command_id for row in failures
               if isinstance(row, dict)):
        failures.append(failure.to_dict())
    previous_diagnostics = json_copy(app.get("gate_diagnostics", {}))
    previous_resumptions = json_copy(app.get("gate_resumptions", {}))
    # Recreate this diagnostic with a new SDK consumer task. Keep every prior
    # record in feedback so no diagnostic or result is silently rewritten.
    app["gate_diagnostics"] = {}
    app["continuation_feedback"] = {
        "previous_segment_id": state.get("run_id"),
        "terminal_reason": prior_terminal_reason,
        "failed_results": snapshot_repair_evidence(root, failures),
        "gate_failure": snapshot_repair_evidence(root, failure.to_dict()),
        "gate_location": location,
        "previous_gate_diagnostics": snapshot_repair_evidence(root, previous_diagnostics),
        "previous_gate_resumptions": snapshot_repair_evidence(root, previous_resumptions),
        "resume": {"location": location, **prior_controls},
    }
    # Carried stage results are inputs to the downstream consumer. Snapshot all
    # of their references before the next segment can reuse workspace paths.
    app["effective"] = snapshot_repair_evidence(root, app.get("effective", {}))
    for field in ("repair_context", "rework_context", "gap_rework", "early_rework",
                  "early_failures", "failed_development_group"):
        if isinstance(app.get(field), dict):
            app[field] = snapshot_repair_evidence(root, app[field])
    if prior_group is not None and location != "group":
        app["failed_development_group"] = prior_group
    app["gate_resumptions"] = {}
    app.update(active_stage=None,
               active_group=prior_group if location == "group" else None,
               early_active=(location == "early"),
               early_pending=[], gap_pending=[],
               stop_reason=None, stop_state=None, terminal_reason=None,
               cancel_sent=[], format_context=None)
    app.pop("user_cancelled", None)
    app.pop("repair_evidence_error", None)
    return app


def _rework_source_document(state, app, failure, next_run_id, *, inherited=None):
    """Carry sealed SDK command descriptors needed for cross-segment rework."""
    results = {}
    restarted = app.get("continuation_feedback", {}).get("target_restart", {}).get(
        "previous_results", {})
    for value in [*app.get("effective", {}).values(), *restarted.values(), failure]:
        result = _result_mapping(value)
        if result is not None:
            results[result.command_id] = result
    wanted = set(results)
    sources = []
    seen = set()
    # Follow reviewer-rework children back to their original author descriptor.
    operations = {}
    rows = []
    inherited = inherited or {}
    for task in state.get("tasks", {}).values():
        for attempt in task.get("attempts", []):
            if attempt.get("state") not in _SETTLED:
                continue
            try:
                operation = OperationInput.from_dict(attempt["command"]["payload"])
            except (KeyError, TypeError, ValueError):
                continue
            operations[operation.command_id] = (operation, attempt)
    pending = list(wanted)
    while pending:
        execution_id = pending.pop()
        if execution_id in seen:
            continue
        seen.add(execution_id)
        if execution_id not in operations:
            carried = inherited.get(execution_id)
            if carried is not None:
                operation = carried['command']
                result = results.get(execution_id)
                if result is not None:
                    result.validate_for(operation)
                sources.append({**carried['row'], 'operation': operation.to_dict()})
                request = operation.payload.get('reviewer_rework')
                if isinstance(request, dict) and isinstance(request.get('source_execution_id'), str):
                    pending.append(request['source_execution_id'])
            continue
        operation, attempt = operations[execution_id]
        request = operation.payload.get("reviewer_rework")
        if isinstance(request, dict) and isinstance(request.get("source_execution_id"), str):
            pending.append(request["source_execution_id"])
        result = results.get(execution_id)
        if result is None:
            try:
                result = _attempt_outcome(attempt)
            except (KeyError, TypeError, ValueError):
                continue
        rows.append((operation, attempt, result))
    for operation, attempt, result in sorted(rows, key=lambda row: row[0].command_id):
        sources.append({
            "target_agent": operation.task_id,
            "execution_id": operation.command_id,
            "task_id": operation.task_id,
            "stage_id": operation.stage_id,
            "operation": operation.to_dict(),
            "terminal_state": attempt["state"],
            "result_identity": {
                key: getattr(result, key)
                for key in ("run_id", "task_id", "stage_id", "command_id")
            },
        })
    # An orphaned rework child must not make the whole successor catalog
    # unusable. Keep other authenticated authors and retain the missing link.
    source_diagnostics = []
    while True:
        available = {row['execution_id'] for row in sources}
        missing = []
        for row in sources:
            request = row['operation']['payload'].get('reviewer_rework')
            if isinstance(request, dict) and request.get('source_execution_id') not in available:
                missing.append(row)
        if not missing:
            break
        for row in missing:
            sources.remove(row)
            source_diagnostics.append({'execution_id': row['execution_id'],
                                       'detail': 'original rework author descriptor unavailable'})
    for row in sources:
        value = row["operation"]
        row["operation"] = pack_input(value["run_dir"], value)
    document = {
        "schema_version": 1,
        "previous_run_id": state["run_id"],
        "previous_revision": state["revision"],
        "previous_generation": state.get("generation", 0),
        "next_run_id": next_run_id,
        "sources": sorted(sources, key=lambda row: row['execution_id']),
    }
    if source_diagnostics:
        document['source_diagnostics'] = source_diagnostics
    return document


def _publish_rework_sources(root, state, app, next_run_id, *, source_header=None):
    feedback = app.get("continuation_feedback", {})
    inherited, diagnostics = {}, []
    restarted = feedback.get("target_restart", {}).get("previous_results", {})
    wanted = {value.get('command_id') for value in
              [*app.get('effective', {}).values(), *restarted.values(), feedback['gate_failure']]
              if isinstance(value, dict) and isinstance(value.get('command_id'), str)}
    if source_header is not None:
        from .rework_source_archive import inherited_rework_sources
        local = {attempt.get('command', {}).get('execution_id')
                 for task in state.get('tasks', {}).values()
                 for attempt in task.get('attempts', [])
                 if attempt.get('state') in _SETTLED}
        # Only absent descriptors need an archive lookup. Local rework children
        # may additionally depend on an original author from a prior segment.
        for task in state.get('tasks', {}).values():
            for attempt in task.get('attempts', []):
                payload = attempt.get('command', {}).get('payload', {}).get('payload', {})
                request = payload.get('reviewer_rework')
                if isinstance(request, dict) and isinstance(request.get('source_execution_id'), str):
                    wanted.add(request['source_execution_id'])
        inherited, diagnostics = inherited_rework_sources(
            root, state, source_header, wanted=wanted - local)
    document = _rework_source_document(
        state, app, feedback["gate_failure"], next_run_id, inherited=inherited)
    if diagnostics:
        document.setdefault('source_diagnostics', []).extend(diagnostics)
    available = {row['execution_id'] for row in document['sources']}
    diagnosed = {row.get('execution_id') for row in document.get('source_diagnostics', [])}
    for execution in sorted(wanted - available - diagnosed):
        document.setdefault('source_diagnostics', []).append({
            'execution_id': execution, 'detail': 'upstream author descriptor unavailable'})
    path = root / "artifacts" / "continuations" / next_run_id / "rework-sources.json"
    if path.is_symlink() or path.resolve() != path.absolute():
        raise ValueError("unsafe continuation rework source path")
    if path.exists() and read_json(path) != document:
        raise ValueError("continuation rework sources already differ")
    atomic_json(path, document)
    return seal_ref(root, {
        "path": path.relative_to(root).as_posix(),
        "media_type": "application/json",
        "metadata": {
            "continuation_rework_sources": True,
            "schema_version": 1,
            "previous_run_id": state["run_id"],
            "previous_revision": state["revision"],
            "previous_generation": state.get("generation", 0),
            "next_run_id": next_run_id,
        },
    }, execution_id="continuation-sources:" + next_run_id)


def _prepare_required_target_restart(root, state, app, start_stage):
    """Reopen an explicitly selected target tail, retaining its failed evidence."""
    from .artifact_verification_policy import required_behavior_policy
    from .behavior_requirements import source_reading_policy

    if (start_stage not in {"target_contract_freeze", "artifact_test_design"}
            or not required_behavior_policy(state.get("input", {}))
            or not source_reading_policy(state.get("input", {}))):
        return False
    effective = app["effective"]
    if start_stage == "artifact_test_design":
        immutable_errors = {"artifact_candidate_changed", "artifact_binary_shadowed"}
        if (app.get("artifact_required_failure") in immutable_errors or any(
                result.get("error_code") in immutable_errors for result in effective.values())):
            raise ValueError("target author restart cannot replace a product snapshot after an immutable-product violation")
    prerequisites = ["source", "environment", "behavior_freeze"]
    if start_stage == "target_contract_freeze":
        prerequisites.append("artifact_test_design")
    for stage in prerequisites:
        if effective.get(stage, {}).get("status") != "completed":
            raise ValueError("target continuation requires completed " + stage)
    if not isinstance(effective["behavior_freeze"].get("outputs", {}).get(
            "artifact_refs", {}).get("behavior_requirements"), dict):
        raise ValueError("target continuation requires frozen behavior requirements")
    tail = {"target_contract_freeze", "artifact_test_execute", "artifact_test_report"}
    if start_stage == "artifact_test_design":
        tail.add("artifact_test_design")
    removed = {key: result for key, result in effective.items()
               if result.get("stage_id", key) in tail}
    historical = snapshot_repair_evidence(root, {key: json_copy(app[key]) for key in (
        "artifact_required_failure", "artifact_required_repairs",
        "required_behavior_status", "required_behavior_assessments",
        "artifact_verification_report", "execution_status") if key in app})
    app["continuation_feedback"]["target_restart"] = {
        "start_stage": start_stage, "previous_results": removed,
        "previous_settlement": historical,
    }
    for key in removed:
        del effective[key]
    for key in historical:
        app.pop(key, None)
    app.pop("flowthrough_finish_pending", None)
    app.update(repair_context=None, rework_context=None, active_group=None,
               early_active=False, early_pending=[], gap_pending=[])
    app["flowthrough_resume"].update(
        next_stage=start_stage, required_target_restart=True)
    return True


def _reconsider_dependency_stop(state, app):
    """Reassess a pre-agent conflict only in an explicitly requested successor."""
    feedback = app.get('continuation_feedback', {})
    group = app.get('active_group')
    if (state.get('state') != 'failed'
            or feedback.get('terminal_reason') != 'coder_revival_stopped'
            or feedback.get('gate_location') != 'group'
            or not isinstance(group, dict) or group.get('kind') != 'development'):
        return
    revival = group.get('revival', {})
    stopped = {name: hold for name, hold in revival.get('holds', {}).items()
               if hold.get('status') == 'stopped'}
    if not stopped or revival.get('pending') or revival.get('terminal_error'):
        return
    # A fresh planner can optionally select any settled member. Do not reopen
    # a group containing a supervisor-terminated author through this route.
    terminated = app.get('progress_supervision', {}).get('terminated_executions', {})
    task_ids = {f"coder.g{group['generation']}.{task['id']}" for task in group['tasks']}
    executions = {row.get('command_id') for row in group.get('results', {}).values()}
    for hold in revival.get('holds', {}).values():
        executions.add(hold.get('source_execution_id'))
        executions.add((hold.get('previous_result') or {}).get('command_id'))
    if any(execution in executions or row.get('task_id') in task_ids
           for execution, row in terminated.items()):
        return
    for name, hold in stopped.items():
        previous = hold.get('previous_result') or group['results'].get(
            f"coder.g{group['generation']}.{name}", {})
        if (previous.get('error_code') != 'dependency_patch_conflict'
                or previous.get('outputs', {}).get('agent_started') is not False):
            return
    feedback['dependency_stop_reconsideration'] = {
        'previous_run_id': state['run_id'], 'previous_holds': json_copy(stopped)}
    for hold in stopped.values():
        hold['status'] = 'needs_plan'


def _retained_integration_command(root, state, stage):
    """Resolve the settled integration selected by its retained host result."""
    from .json_catalog import iter_catalog
    root = Path(root)
    source_result = (state.get('application_state') or {}).get('effective', {}).get(stage)
    if not isinstance(source_result, dict) or not isinstance(source_result.get('command_id'), str):
        raise ValueError('requested integration has no retained execution identity')
    execution_id = source_result['command_id']
    original = None
    for task in state.get('tasks', {}).values():
        for attempt in task.get('attempts', []):
            command = attempt.get('command', {})
            if command.get('execution_id') != execution_id or attempt.get('state') not in _SETTLED:
                continue
            original = OperationInput.from_dict(unpack_input(root, command['payload']))
    if original is None:
        header = state.get('input') or {}
        ref = header.get('continuation', {}).get('support_refs', {}).get('continuation:rework_sources')
        if not isinstance(ref, dict):
            raise ValueError('requested integration SDK command is not retained')
        metadata = ref.get('metadata', {})
        if (header.get('run_id') != state['run_id']
                or metadata.get('next_run_id') != state['run_id']
                or ref.get('path') != 'artifacts/continuations/' + state['run_id'] + '/rework-sources.json'):
            raise ValueError('integration source catalog identifies another execution segment')
        # Select this one host-bound descriptor. The existing storage decoder
        # handles packed inputs; no new checksum or content identity is added.
        for kind, name, row in iter_catalog(verified_path(root, ref)):
            if kind != 'source' or row.get('execution_id') != execution_id:
                continue
            if row.get('terminal_state') not in _SETTLED or row.get('stage_id') != stage:
                raise ValueError('requested integration catalog entry is not a settled integration')
            candidate = OperationInput.from_dict(unpack_input(root, row['operation']))
            if (row.get('task_id') != candidate.task_id or row.get('target_agent') != candidate.task_id
                    or row.get('execution_id') != candidate.command_id or row.get('stage_id') != candidate.stage_id):
                raise ValueError('requested integration catalog operation identity differs')
            if original is not None:
                raise ValueError('requested integration has duplicate retained execution descriptors')
            original = candidate
    if original is None:
        raise ValueError('requested integration SDK command is not retained')
    if original.stage_id != stage or original.command_id != execution_id or Path(original.run_dir) != root:
        raise ValueError('requested integration SDK command belongs to another boundary')
    OperationResult.from_dict(source_result).validate_for(original)
    return original


def _prepare_integration_replay(root, state, app, stage, *, resolution=None):
    """Retain the exact settled integration input selected by the host history."""
    root = Path(root)
    original = _retained_integration_command(root, state, stage)
    execution_id = original.command_id
    path = root / 'artifacts' / 'continuations' / 'integration-replays' / uuid4().hex / 'command.json'
    if path.resolve() != path.absolute():
        raise ValueError('integration replay input must not traverse symlinks')
    replay = original.to_dict()
    if resolution is not None:
        replay['payload']['integration_resolution'] = json_copy(resolution)
    atomic_json(path, replay)
    app['integration_replay'] = {'stage': stage, 'source_execution_id': execution_id,
                                 'command_ref': {'path': path.relative_to(root).as_posix()}}
    previous = app.pop('integration_repair', None)
    if previous is not None:
        app.setdefault('integration_repair_history', []).append(previous)
    app.update(active_group=None, early_active=False)


def _prepare_integration_successor(root, state, app):
    """Rebind a retained merge only when preparing an explicit SDK successor."""
    from .integration_repair import INTEGRATION_STAGES
    repair = app.get('integration_repair')
    if not isinstance(repair, dict) or not isinstance(repair.get('command_ref'), dict):
        return False
    root = Path(root)
    original = OperationInput.from_dict(read_json(verified_path(root, repair['command_ref'])))
    header = state.get('input') or {}
    if (original.stage_id not in INTEGRATION_STAGES or Path(original.run_dir) != root
            or original.run_id != header.get('logical_run_id', state['run_id'])):
        raise ValueError('retained integration repair belongs to another continuation')
    record = read_json(verified_path(root, repair['merge_ref']))
    if (record.get('command_ref') != repair['command_ref']
            or record.get('source_workspace') != original.options.get('workspace', 'worktree')
            or not record.get('conflicts')):
        raise ValueError('retained integration repair differs from its host merge binding')
    source_result = (state.get('application_state') or {}).get('effective', {}).get(original.stage_id)
    if not isinstance(source_result, dict):
        raise ValueError('retained integration repair has no integration result')
    result = OperationResult.from_dict(source_result)
    selected = _retained_integration_command(root, state, original.stage_id)
    if selected.command_id != original.command_id:
        if selected.payload.get('integration_resolution', {}).get('merge_ref') != repair['merge_ref']:
            raise ValueError('selected integration does not consume the retained merge')
    elif (result.error_code != 'integration_merge_required'
          or result.outputs.get('integration_merge_ref') != repair['merge_ref']):
        raise ValueError('retained integration result does not identify its merge')
    if repair.get('status') == 'integrating' and result.status == 'completed':
        # A later failure must not replay an integration that already settled.
        app.setdefault('integration_repair_history', []).append(app.pop('integration_repair'))
        return True
    resolution = None
    coder_result = repair.get('coder_result')
    execution_id = repair.get('coder_execution_id')
    patch = repair.get('coder_patch')
    if repair.get('status') == 'running':
        task = state.get('tasks', {}).get(repair.get('task_id'))
        attempts = task.get('attempts', []) if isinstance(task, dict) else []
        if attempts and attempts[-1].get('state') == 'succeeded':
            attempt = attempts[-1]
            command = OperationInput.from_dict(attempt['command']['payload'])
            settled = _attempt_outcome(attempt)
            if (command.task_id != repair['task_id'] or command.stage_id != 'coder'
                    or command.payload.get('development_task') != repair['plan']['tasks'][0]
                    or command.payload.get('integration_merge_ref') != repair['merge_ref']):
                raise ValueError('settled integration coder differs from its retained assignment')
            coder_result = settled.to_dict()
            execution_id = command.command_id
            patch = settled.outputs.get('artifact_refs', {}).get('coder_patch')
    if isinstance(coder_result, dict) and coder_result.get('status') == 'completed':
        coder = OperationResult.from_dict(coder_result)
        if (coder.task_id != repair['task_id'] or coder.stage_id != 'coder'
                or coder.run_id != original.run_id or coder.command_id != execution_id
                or coder.outputs.get('artifact_refs', {}).get('coder_patch') != patch):
            raise ValueError('retained integration resolution differs from its coder result')
        if isinstance(patch, dict) and verified_path(root, patch).stat().st_size:
            resolution = {'merge_ref': repair['merge_ref'], 'coder_patch': patch,
                          'coder_execution_id': execution_id}
    _prepare_integration_replay(root, state, app, original.stage_id, resolution=resolution)
    return True


def prepare_application(root, state, *, start_stage=None,
                        target_workflow_version=None, force_initial_plan=False):
    """Carry settled work forward, invalidating only the requested repair tail."""
    state = hydrate_run_snapshot(root, state)
    v16 = target_workflow_version is not None and target_workflow_version >= 16
    if not v16 and start_stage not in {None, "contract_repair_plan"}:
        raise ValueError("continuation currently supports contract_repair_plan only")
    app = json_copy(state.get("application_state") or {})
    checkpoint = _v17_checkpoint(app, start_stage)
    checkpoint_continuation = (
        state["state"] == "succeeded"
        and target_workflow_version is not None
        and target_workflow_version >= 17
        and isinstance(checkpoint, dict)
        and checkpoint.get("acceptance_status") == "unverified"
        and isinstance(start_stage, str)
        and checkpoint.get("next_stage") == start_stage
    )
    if state["state"] not in {"failed", "cancelled"} and not checkpoint_continuation:
        raise ValueError("continuation requires a failed or cancelled segment")
    if any(task["attempts"][-1]["state"] not in _SETTLED
           for task in state.get("tasks", {}).values()):
        raise ValueError("settle every execution before continuation")
    if any(wait["state"] == "open" for wait in state.get("waits", {}).values()):
        raise ValueError("resolve open waits before continuation")
    budget_group = _budget_exhausted_development_group(state, app)
    require_settled_review_rework(app)
    if app.get("support_pending") or app.get("administrator_wait"):
        raise ValueError("settle support and administrator work before continuation")
    if target_workflow_version is not None and target_workflow_version >= 17:
        # A completed v17 coder may deliberately remove its untracked goal
        # report after exporting the available patch.  Preserve an unavailable
        # reference as a diagnostic rather than making a later continuation
        # depend on that business-report file.  The prior SDK segment remains
        # the raw immutable record; consumers receive only contained evidence.
        app = _v17_diagnostic_evidence_view(root, app)
        app = _prepare_v16_application(root, state, app)
        feedback = app["continuation_feedback"]
        failure = OperationResult.from_dict(feedback["gate_failure"])
        app.setdefault("effective", {})[failure.stage_id] = json_copy(feedback["gate_failure"])
        if failure.task_id != failure.stage_id:
            app["effective"][failure.task_id] = json_copy(feedback["gate_failure"])
        app["flowthrough_resume"] = {
            "stage": failure.stage_id, "task_id": failure.task_id,
            "command_id": failure.command_id,
            "location": feedback.get("gate_location", "main"),
        }
        if start_stage is not None:
            from .workflow import DEPENDENCIES
            if start_stage not in DEPENDENCIES:
                raise ValueError("continuation has unknown start stage")
            app["flowthrough_resume"]["next_stage"] = start_stage
            app.update(active_group=None, early_active=False)
        from .integration_repair import INTEGRATION_STAGES
        if budget_group is not None and start_stage not in INTEGRATION_STAGES:
            app["active_group"] = budget_group
            app["failed_development_group"] = None
            app["flowthrough_resume"] = {
                "stage": "implementation", "task_id": "implementation",
                "command_id": app["effective"].get("implementation", {}).get("command_id"),
                "location": "group",
            }
            feedback_key = ("budget_resume" if app["continuation_feedback"].get("terminal_reason")
                            == "agent_assignment_budget_exhausted" else "diagnostic_group_resume")
            app["continuation_feedback"][feedback_key] = {
                "prior_terminal_reason": app["continuation_feedback"].get("terminal_reason"),
                "prepared_task_ids": budget_group["goal_scheduled"],
                "settled_coder_task_ids": budget_group["scheduled"],
            }
        _prepare_required_target_restart(root, state, app, start_stage)
        rebound_integration = _prepare_integration_successor(root, state, app)
        if start_stage in INTEGRATION_STAGES and not rebound_integration:
            _prepare_integration_replay(root, state, app, start_stage)
        if target_workflow_version >= 40 and start_stage is None:
            _reconsider_dependency_stop(state, app)
        app["acceptance_status"] = "unverified"
        return app
    if v16:
        return _prepare_v16_application(root, state, app)
    start_stage = start_stage or "contract_repair_plan"
    effective = app.get("effective", {})
    if (effective.get("contract_freeze", {}).get("status") == "completed"
            or app.get("locked_artifacts", {}).get("contract_lock_sha256")
            or app.get("locked_artifacts", {}).get("contract_sha256")):
        raise ValueError("contract planner continuation cannot replace a frozen contract")
    context = app.get("repair_context")
    if (effective.get("contract_diagnose", {}).get("status") != "completed"
            or not isinstance(context, dict) or context.get("repair_scope") != "contract"):
        raise ValueError("contract planner continuation requires completed diagnosis and repair context")
    # The previous diagnostic packet remains byte-for-byte identical: planning
    # verifies the original input bindings, even across SDK execution segments.
    for ref in context.get("artifact_refs", {}).values():
        verified_path(Path(root), ref)
    restart_fixed_plan = (target_workflow_version is not None
                          and target_workflow_version >= 15
                          and (force_initial_plan
                               or not _v15_contract_plan_checkpoint(root, app)))
    if restart_fixed_plan:
        _preserve_diagnosis(root, app, effective["contract_diagnose"])
        start_stage = "contract_diagnose"
        app["plan_documents"] = {}
    failures = json_copy(app.get("continuation_feedback", {}).get("failed_results", []))
    for task in state.get("tasks", {}).values():
        attempt = task["attempts"][-1]
        result = (attempt.get("result") or {}).get("value")
        if isinstance(result, dict) and (result.get("status") != "completed"
                or result.get("outputs", {}).get("verdict") == "rejected"):
            if not any(prior.get("command_id") == result.get("command_id") for prior in failures):
                failures.append(result)
    app["continuation_feedback"] = {
        "previous_segment_id": state.get("run_id"),
        "terminal_reason": app.get("terminal_reason"),
        "failed_results": snapshot_repair_evidence(root, failures),
    }
    invalidate_contract_tail(app, include_diagnosis=restart_fixed_plan)
    pending = [start_stage]
    review = effective.get("research_review", {})
    if (review.get("error_code") == "research_review_invalid"
            and effective.get("gap_research", {}).get("status") == "completed"):
        effective.pop("research_review")
        app.setdefault("early_failures", {}).pop("research_review", None)
        pending.append("research_review")
        app["gap_failure"] = None
    # Archive current upstream outputs now, before the next author can reuse
    # their paths. Do not rewrite previous SDK attempts or the diagnostic packet.
    app["effective"] = snapshot_repair_evidence(root, effective)
    app.update(active_stage=None, active_group=None, early_active=True,
               early_pending=pending, gap_pending=[], stop_reason=None,
               stop_state=None, terminal_reason=None, cancel_sent=[],
               format_context=None, rework_context=context["current_failure"])
    app.pop("repair_evidence_error", None)
    app.setdefault("early_rework", {})["contract"] = context["current_failure"]
    return app


def continue_from_planner(operations, run_dir, run_id, *, next_run_id, reason,
                          additional_seconds=_INHERIT_DEADLINE, upgrade_workflow=False,
                          start_stage=None, additional_agent_assignments=0,
                          replace_unstarted_successor=None, model_policy=None):
    """Resume the same logical migration in a new, auditable SDK segment.

    A prepared packet and SDK command receipts make a crash between continuation,
    initialization and publishing run.json replayable with the same next_run_id.
    No worker is started until all three steps have completed.
    """
    from .operations import MigrationRun
    from .model_policy import validate_model_config
    selected_models = None if model_policy is None else validate_model_config(model_policy)
    if not isinstance(reason, str) or not reason.strip():
        raise ValueError("continuation requires a reason")
    omitted_budget = additional_seconds is _INHERIT_DEADLINE
    if not omitted_budget and additional_seconds is not None and (type(additional_seconds) is not int or additional_seconds <= 0):
        raise ValueError("additional_seconds must be a positive integer or None")
    if (type(additional_agent_assignments) is not int
            or additional_agent_assignments < 0):
        raise ValueError("additional_agent_assignments must be a nonnegative integer")
    if replace_unstarted_successor is not None and (
            not isinstance(replace_unstarted_successor, str)
            or not re.fullmatch(r"[A-Za-z0-9_.:-]+", replace_unstarted_successor)):
        raise ValueError("replace_unstarted_successor must be a valid SDK segment identity or None")
    if (not isinstance(next_run_id, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]+", next_run_id)
            or next_run_id == run_id):
        raise ValueError("continuation requires a distinct SDK segment identity")
    root = Path(run_dir).resolve()
    # Reject unknown/legacy locations before opening writers or creating locks.
    frozen_header = operations._header(root, run_id)
    inherit_deadline = omitted_budget and frozen_header['definition']['workflow_version'] >= 19
    if omitted_budget:
        additional_seconds = None if inherit_deadline else 43200
    check_storage_budget(root, phase="continue")
    packet_path = root / "artifacts" / "continuations" / next_run_id / "prepared.json"
    if packet_path.is_symlink() or packet_path.resolve() != packet_path.absolute():
        raise ValueError("unsafe continuation packet path")
    if not packet_path.exists():
        from .artifact_retention import restore_archived_artifact
        restore_archived_artifact(root, packet_path.relative_to(root).as_posix())
    if packet_path.exists():
        _verify_prepared_payload(root, read_prepared_json(root, packet_path.relative_to(root).as_posix()))
    definition_changed = False
    with workspace_lock(root / ".locks" / "continuation", blocking=False):
        successor_path = (root / "artifacts" / "continuations" / run_id / "successor.json"
                          if replace_unstarted_successor is None else
                          root / "artifacts" / "continuations" / run_id / "replacements"
                          / next_run_id / "successor.json")
        if successor_path.is_symlink() or successor_path.resolve() != successor_path.absolute():
            raise ValueError("unsafe continuation successor path")
        successor = {"next_run_id": next_run_id, "reason": reason,
                     "upgrade_workflow": upgrade_workflow, "additional_seconds": additional_seconds,
                     "additional_agent_assignments": additional_agent_assignments}
        if selected_models is not None:
            successor["model_policy"] = selected_models
        if inherit_deadline:
            successor['inherit_deadline'] = True
        if start_stage is not None:
            successor["start_stage"] = start_stage
        if replace_unstarted_successor is not None:
            successor["replaces_unstarted_successor"] = replace_unstarted_successor
        if successor_path.exists() and read_json(successor_path) != successor:
            raise ValueError("source segment already has a different prepared successor")
        check_storage_budget(root, phase="continue-final")
        with open_runtime(root, handlers=operations.handlers,
                          isolation_mode=operations.isolation_mode, now=operations.clock,
                          memory_policy=operations.memory_policy) as runtime:
            sdk = Orchestrator(root / "orchestrator.sqlite3", runtime.kernel,
                               runtime=runtime, clock=operations.clock)
            try:
                if packet_path.exists():
                    packet = read_prepared_json(root, packet_path.relative_to(root).as_posix())
                    if (packet["previous_run_id"] != run_id or packet["header"]["run_id"] != next_run_id
                            or packet["reason"] != reason or packet["header"]["run_dir"] != str(root)
                            or packet.get("upgrade_workflow", False) != upgrade_workflow
                            or packet.get("requested_start_stage") != start_stage
                            or packet["header"]["continuation"]["additional_seconds"] != additional_seconds
                            or packet["header"]["continuation"].get('inherit_deadline', False) != inherit_deadline
                            or packet["header"]["continuation"].get(
                                "additional_agent_assignments", 0) != additional_agent_assignments
                            or packet["header"]["continuation"].get(
                                "replaces_unstarted_segment") != replace_unstarted_successor):
                        raise ValueError("continuation replay differs from prepared request")
                    _verify_prepared_payload(root, packet)
                    if selected_models is not None and packet["header"].get("model_policy") != selected_models:
                        raise ValueError("continuation replay selects different model settings")
                    current = read_json(root / "run.json")
                    if current.get("run_id") not in {run_id, next_run_id}:
                        raise ValueError("a later segment is current; cannot republish historical continuation")
                    expected_header = packet["header"] if current["run_id"] == next_run_id else sdk.get_run(run_id)["input"]
                    if current != expected_header:
                        raise ValueError("current header differs from the continuation segment")
                else:
                    header = operations._header(root, run_id)
                    for ref in header["initial_refs"].values():
                        verified_path(root, ref)
                    current = read_json(root / "run.json")
                    replacing = current["run_id"] != run_id
                    if replacing and current["run_id"] != replace_unstarted_successor:
                        raise ValueError("only the current segment may be continued")
                    if replacing:
                        _retire_planned_successor(root, sdk, replace_unstarted_successor,
                                                  next_run_id, run_id, current)
                    state = hydrate_run_snapshot(root, sdk.get_run(run_id))
                    if state["input"] != header or state["definition"] != header["definition"]:
                        raise ValueError("SDK segment disagrees with frozen input")
                    from .workflow_upgrade import validate_upgrade_definition
                    current_definition = validate_upgrade_definition(header)
                    if upgrade_workflow:
                        definition = current_definition
                    else:
                        # A continuation preserves its authenticated frozen DAG.
                        # Installing a newer host is not consent to upgrade it.
                        definition = json_copy(header["definition"])
                    source_workflow_version = header["definition"]["workflow_version"]
                    target_workflow_version = definition["workflow_version"]
                    app = prepare_application(
                        root, state,
                        start_stage=start_stage,
                        target_workflow_version=target_workflow_version,
                        force_initial_plan=(upgrade_workflow
                                            and source_workflow_version < 15
                                            <= target_workflow_version),
                    )
                    if replacing:
                        replaced = hydrate_run_snapshot(root, sdk.get_run(replace_unstarted_successor))
                        used = (replaced.get('application_state') or {}).get('agent_assignments', 0)
                        app['agent_assignments'] = max(app.get('agent_assignments', 0), used)
                        app['continuation_feedback']['replaced_successor_usage'] = {
                            'run_id': replace_unstarted_successor,
                            'agent_assignments': used,
                        }
                    interrupted_group = app.get('active_group')
                    if (target_workflow_version >= 17
                            and isinstance(interrupted_group, dict)
                            and interrupted_group.get('kind') == 'development'):
                        from .partial_recovery import (recover_interrupted_coders,
                                                       carry_overwritten_coder_patches,
                                                       carry_failed_rework_instructions)
                        partial_refs = recover_interrupted_coders(root, state, interrupted_group)
                        predecessor_id = header.get('continuation', {}).get('previous_run_id')
                        if isinstance(predecessor_id, str):
                            predecessor = hydrate_run_snapshot(root, sdk.get_run(predecessor_id))
                            partial_refs.update(carry_overwritten_coder_patches(
                                root, predecessor, state, interrupted_group))
                        if partial_refs:
                            interrupted_group['execution_payload']['recovered_partial_patches'] = partial_refs
                        original_requests = carry_failed_rework_instructions(state, interrupted_group)
                        if original_requests:
                            interrupted_group['execution_payload']['recovered_rework_requests'] = original_requests
                    v16_continuation = target_workflow_version >= 16
                    ungated_continuation = target_workflow_version >= 17
                    if ungated_continuation:
                        from .workflow import stage_routes
                        _, _, next_stages, _ = stage_routes(definition)
                        resume = app["flowthrough_resume"]
                        resumed_stage = resume.get("next_stage") or next_stages.get(
                            resume["stage"], resume["stage"])
                    else:
                        resumed_stage = ("gate_handoff" if v16_continuation
                                         else app["early_pending"][0])
                    request = json_copy(header["request"])
                    overrides = app.get("recovery_budget_override", {})
                    if overrides:
                        request["budget"].update(overrides)
                    if additional_agent_assignments:
                        assignment_limit = request["budget"].get("max_agent_assignments")
                        if assignment_limit is not None:
                            if type(assignment_limit) is not int or assignment_limit <= 0:
                                raise ValueError("continuation has an invalid assignment budget")
                            request["budget"]["max_agent_assignments"] = (
                                assignment_limit + additional_agent_assignments)
                    definition['request'] = json_copy(request)
                    definition['budget'] = json_copy(request['budget'])
                    definition_changed = definition != header["definition"]
                    app.pop("recovery_budget_override", None)
                    inherited_deadline = app.pop("recovery_deadline_epoch", header['deadline_epoch'])
                    carried = [stage for stage, result in app["effective"].items()
                               if ungated_continuation or result["status"] == "completed"
                               and (stage not in REVIEW_STAGES
                                    or result.get("outputs", {}).get("verdict") == "approved")]
                    now = operations.clock()
                    limit = additional_seconds
                    resumed = {**header, "run_id": next_run_id,
                               "request": request, "definition": definition,
                               "logical_run_id": header.get("logical_run_id", run_id),
                               "registry_revision": runtime.registry_revision,
                               "sdk_identity": inspect_runtime(root)["module"],
                               "started_at": now,
                               "deadline_epoch": (inherited_deadline if inherit_deadline else
                                   None if limit is None else now + limit - (
                                       app.get("administrator_wait_seconds", 0) if target_workflow_version < 19 else 0)),
                               "continuation": {"previous_run_id": run_id, "reason": reason,
                                   "start_stage": resumed_stage, "carried_stages": carried,
                                   "previous_registry_revision": header["registry_revision"],
                                   "previous_deadline_epoch": header["deadline_epoch"],
                                   "additional_seconds": limit,
                                   "additional_agent_assignments": additional_agent_assignments,
                                   "replaces_unstarted_segment": replace_unstarted_successor,
                                   "agent_assignments_carried": app["agent_assignments"],
                                   "support_refs": {"continuation:host_launch_interface":
                                                    _host_interface(root, next_run_id)}}}
                    if selected_models is not None:
                        resumed["model_policy"] = selected_models
                    resumed["continuation"]["model_policy_changed"] = (
                        resumed.get("model_policy") != header.get("model_policy"))
                    if inherit_deadline:
                        resumed['continuation']['inherit_deadline'] = True
                    if v16_continuation:
                        resumed["continuation"]["support_refs"][
                            "continuation:rework_sources"] = _publish_rework_sources(
                                root, state, app, next_run_id, source_header=header)
                    if isinstance(app.get('active_group'), dict):
                        for name, ref in app['active_group'].get('execution_payload', {}).get(
                                'recovered_partial_patches', {}).items():
                            resumed['continuation']['support_refs'][
                                'continuation:interrupted-coder:' + name] = ref
                    if upgrade_workflow:
                        resumed['watchdog_policy'], watchdog_diagnostic = _upgrade_watchdog_policy(root, header)
                        if watchdog_diagnostic:
                            resumed['continuation']['watchdog_control_diagnostic'] = watchdog_diagnostic
                        from .workflow_upgrade import publish_upgrade_rules
                        rule_refs = publish_upgrade_rules(root, next_run_id)
                        if target_workflow_version >= 34:
                            from .target_session import target_session_support_files
                            for relative, contents in target_session_support_files().items():
                                target = root / "artifacts" / "harness-support" / relative
                                if target.is_symlink() or target.resolve() != target.absolute():
                                    raise ValueError("unsafe target session support path")
                                if target.exists() and target.read_text(encoding="utf-8") != contents:
                                    raise ValueError("target session support already differs")
                                target.parent.mkdir(parents=True, exist_ok=True)
                                target.write_text(contents, encoding="utf-8")
                                rule_refs["harness_support:" + relative] = {
                                    "path": target.relative_to(root).as_posix(), "media_type": "text/plain"}
                        resumed["initial_refs"] = {**header["initial_refs"], **rule_refs}
                        resumed["workflow_upgrade"] = {
                            "schema_version": 1, "link_kind": "application_upgrade",
                            "previous_run_id": run_id, "previous_revision": state["revision"],
                            "previous_generation": state.get("generation", 0),
                            "previous_header_sha256": digest(header),
                            "previous_definition_sha256": digest(header["definition"]),
                            "target_definition_sha256": digest(definition),
                            "from_version": header["definition"]["workflow_version"],
                            "to_version": definition["workflow_version"], "reason": reason,
                            "budget_carried": json_copy(request["budget"]),
                            "procedural_rules": {key: {"previous": header["initial_refs"].get(key),
                                                        "current": ref} for key, ref in rule_refs.items()},
                        }
                    else:
                        resumed.pop("workflow_upgrade", None)
                    # Preserve the old header for status/history by its SDK ID.
                    archive = root / "artifacts" / "continuations" / run_id / "run.json"
                    if archive.is_symlink() or archive.resolve() != archive.absolute():
                        raise ValueError("unsafe historical header path")
                    if archive.exists() and read_json(archive) != header:
                        raise ValueError("historical header already differs")
                    atomic_json(archive, header)
                    empty = {"run_id": next_run_id, "tasks": {}}
                    if ungated_continuation:
                        schedules = operations._advance_without_business_gates(
                            empty, resumed, app)
                    elif v16_continuation:
                        feedback = app["continuation_feedback"]
                        failure = OperationResult.from_dict(feedback["gate_failure"])
                        schedules = operations._diagnostic_handoff(
                            empty, resumed, app, failure.stage_id, failure,
                            failure.command_id,
                            location=feedback.get("gate_location", "main"),
                        )
                    else:
                        schedules = []
                        for stage in app["early_pending"]:
                            causation_stage = ("contract_diagnose" if stage == "contract_repair_plan"
                                               else "gap_research")
                            causation_id = app["effective"].get(causation_stage, {}).get("command_id")
                            if stage == "contract_diagnose":
                                causation_id = app.get("repair_context", {}).get("failure_execution_id")
                            schedules.extend(operations._schedule(empty, resumed, app, stage,
                                activate=False, dependencies=[],
                                causation_id=causation_id))
                    if any(op["kind"] == "finish" for op in schedules):
                        reason = app.get("terminal_reason") or app.get("stop_reason") or "no_runnable_work"
                        raise ValueError("continuation cannot start: " + str(reason))
                    stored_app = pack_application_state(root, app)
                    # Bind the exact compact state and initial SDK operations
                    # into the successor's immutable Run input. The header is
                    # finalized only after this value is known.
                    resumed["continuation"]["prepared_payload_sha256"] = (
                        _prepared_payload_digest(stored_app, schedules)
                    )
                    resumed.pop("header_sha256", None)
                    resumed["header_sha256"] = digest(resumed)
                    packet = {"previous_run_id": run_id, "previous_revision": state["revision"],
                              "upgrade_workflow": upgrade_workflow,
                              "previous_generation": state["generation"],
                              "reason": reason, "header": resumed, "application_state": stored_app,
                              "operations": schedules}
                    if start_stage is not None:
                        packet["requested_start_stage"] = start_stage
                    # Reserve the source before publishing the packet: a crash
                    # between these writes may replay this request, never fork it.
                    atomic_json(successor_path, successor)
                    atomic_json(packet_path, packet)
                    _verify_prepared_payload(root, packet)
                if packet["header"]["registry_revision"] != runtime.registry_revision:
                    raise ValueError("prepared continuation deployment changed")
                for ref in packet["header"]["initial_refs"].values():
                    verified_path(root, ref)
                from .workflow_upgrade import validate_upgrade_definition
                validate_upgrade_definition(packet["header"])
                atomic_json(successor_path, successor)
                source = hydrate_run_snapshot(root, sdk.get_run(run_id))
                create_segment = (upgrade_workflow or replace_unstarted_successor is not None
                                  or packet["header"]["definition"] != source["definition"]
                                  or packet["header"].get("model_policy") != source["input"].get("model_policy"))
                if create_segment:
                    source_checkpoint = _v17_checkpoint(
                        source.get("application_state") or {}, start_stage)
                    checkpoint_source = (
                        source["state"] == "succeeded"
                        and isinstance(source_checkpoint, dict)
                        and source_checkpoint.get("acceptance_status") == "unverified"
                        and source_checkpoint.get("next_stage") == start_stage
                    )
                    if (source["state"] not in {"failed", "cancelled"}
                            and not checkpoint_source
                            or source["revision"] != packet["previous_revision"]
                            or source.get("generation", 0) != packet.get("previous_generation", 0)
                            or (not replace_unstarted_successor
                                and sdk.get_run_summary(run_id).get("next_run_id"))):
                        raise ValueError("source segment changed after upgrade preparation")
                    kind = "upgrade" if upgrade_workflow else "configuration-continuation"
                    sdk.create_run(next_run_id, command_id=kind + ":" + next_run_id,
                                   input=packet["header"], definition=packet["header"]["definition"])
                else:
                    sdk.continue_run(run_id, next_run_id, command_id="continue:" + next_run_id,
                                     expected_revision=packet["previous_revision"],
                                     expected_generation=packet.get("previous_generation", 0), input=packet["header"])
                # A retry after target creation must consume the payload bound
                # by that already-durable SDK input, never a changed packet.
                target = sdk.get_run(next_run_id)
                if target.get("input") != packet["header"]:
                    raise ValueError("prepared continuation differs from SDK successor input")
                sdk.apply_operations(next_run_id, command_id="initialize-continuation", expected_revision=0,
                                     expected_generation=0,
                                     operations=packet["operations"], application_state=packet["application_state"])
                atomic_json(root / "run.json", packet["header"])
                atomic_json(root / "workflow-definition.json", packet["header"]["definition"])
                operations._drain_terminal_events(sdk, {**packet["header"], "run_id": run_id})
                operations._audit_action(root, next_run_id, "continue-from-contract-planner")
                from .storage_lifecycle import automatic_retention
                automatic_retention(root, sdk.get_run(run_id))
                return MigrationRun(
                    next_run_id, root, hydrate_run_snapshot(root, sdk.get_run(next_run_id)))
            finally:
                sdk.close()
