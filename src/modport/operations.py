"""ModPort application policy over the public Dispatcher SDK.

Only the SDK owns execution, queues, Run persistence and transaction boundaries.
Application decisions atomically record routing, budget, inputs and event cursors.
"""
from .workspace import project_path
from contextlib import contextmanager, nullcontext
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
import json
import math
import re
import sqlite3
import time
import uuid

from dispatcher_sdk.execution_kernel import ExecutionCommandV2, RetryPolicy
from dispatcher_sdk.orchestrator import Orchestrator, OrchestratorHost, RevisionConflict

from .contracts import FORMAT_VERSION, OperationInput, OperationResult, json_copy
from .analysis_contract import VERIFICATION_STAGES
from .evidence import atomic_json, digest, file_digest, read_json, seal_ref, verified_path, workspace_lock
from .kernel_runtime import open_runtime, reconcile_receipt, operation_lock, validate_stage_response
from .sdk_compat import SDK_VERSION, inspect_runtime, require_compatible_storage
from .snapshot_storage import snapshot_databases
from .payload_storage import pack_input, unpack_input, verify_operations, read_prepared_json, is_packed_input
from .storage_budget import check_storage_budget
from .models import MigrationRequest
from .business_policy import business_gates_disabled, compile_package_scope
from .artifact_verification_policy import (
    required_behavior_policy, assess_source_selection, assess_target_selection,
    archive_required_behavior_failure, required_behavior_assessments,
)
from .behavior_requirements import source_reading_policy
from .memory_admission import MemoryPolicy, MemorySnapshot, host_memory_snapshot
from .progress_policy import progress_supervised
from .research_orchestration import ResearchOrchestration, gap_identity
from .rework_orchestration import ReviewReworkOrchestration, project_rework_responses
from .gate_policy import (DownstreamGateOrchestration, available_refs, diagnostic_context, diagnostic_view, downstream_toolcall,
                          forwarded, handoff_task, passed)
from .repair_evidence import RepairEvidenceError, is_repair_artifact_ref, snapshot_repair_evidence
from .repair_reset import contract_repair_tail, invalidate_contract_tail, require_settled_review_rework
from .research_policy import (RESEARCH_STAGES as BUDGETED_RESEARCH_STAGES, initialize_research,
                              eligible_kinds, remaining, record_dispatch, observe_attempts)
from .telemetry import record_sdk_events, record_event
from .execution_progress import record_command_progress, read_execution_progress
from .startup_recovery import classify_startup_timeout
from .audit_export import export_isolated
from .application_state_storage import (
    hydrate_run_snapshot,
    is_packed_application_state,
    pack_application_state,
    unpack_application_state,
)
from .rubric import acceptance_rubric
from .workflow import (AGENT_STAGES, DEPENDENCIES, EARLY_STAGES, FATAL_ERRORS, MAIN_STAGES, NEXT_STAGE,
                       PLANNING_STAGES, REPAIR_PLANNING_STAGES, REPAIR_POLICY,
                       REPAIR_ROUTE, REVIEW_STAGES, REWORK_STAGES, SUPERVISOR_STAGE, WORKFLOW_VERSION,
                       agent_model_policy, compile_migration_workflow,
                       stage_routes, agent_stage)

TERMINAL = frozenset({"succeeded", "failed", "cancelled"})
EXECUTION_TERMINAL = TERMINAL | {"timed_out", "dead"}
FLOWTHROUGH_OPERATIONAL_ERRORS = frozenset({
    "budget_exhausted", "token_budget_exhausted", "build_sandbox_unavailable", "command_artifact_invalid",
    "agent_rules_invalid", "agent_output_invalid", "result_contract_invalid",
    "baseline_write_scope_invalid",
    "review_workspace_invalid", "source_history_invalid",
    "integration_rollback_failed", "repair_rollback_failed",
    "unsafe_repository_url", "source_clone_failed", "source_repository_mismatch",
    "source_ref_invalid", "worktree_failed", "worktree_mismatch",
    "version_resolution_failed", "mdk_clone_failed", "mdk_ref_invalid", "mdk_provenance_invalid",
    "java_toolchain_failed", "java_toolchain_mismatch", "java_toolchain_unlocked",
    "java_toolchain_unverified", "agent_launch_failed",
    "opencode_cleanup_unconfirmed",
    "prompt_compression_failed", "prompt_compression_audit_failed",
    "candidate_identity_mismatch", "codemod_input_invalid", "build_preparation_integrity",
    "inherited_harness_identity_invalid",
})
SUBSCRIPTION = "modport.policy.v2"
_RECOVERY_PAYLOAD_SCHEMA = "modport.recovery-prepared-payload.v1"
CANCEL_SETTLE_TIMEOUT_SECONDS = 30.0
CANCEL_SETTLE_POLL_SECONDS = 0.05


def _recovery_payload_digest(application_state, operations, decision):
    decision_without_binding = json_copy(decision)
    decision_without_binding.pop("prepared_payload_sha256", None)
    return digest({
        "schema": _RECOVERY_PAYLOAD_SCHEMA,
        "application_state": application_state,
        "operations": operations,
        "decision": decision_without_binding,
    })


def _verify_recovery_payload(root, packet):
    stored = packet.get("application_state")
    operations = packet.get("operations")
    decision = packet.get("decision")
    if not isinstance(stored, dict) or not isinstance(operations, list) or not isinstance(decision, dict):
        raise ValueError("malformed prepared recovery payload")
    # Resolve every compact-state blob before reopen_run can persist a recovery
    # decision or change the Run generation.
    unpack_application_state(root, stored)
    verify_operations(root, operations)
    declared = decision.get("prepared_payload_sha256")
    if declared is None:
        if is_packed_application_state(stored) or any(
                is_packed_input((item.get("command") or {}).get("payload"))
                for item in operations):
            raise ValueError("packed recovery payload has no immutable binding")
        return
    if declared != _recovery_payload_digest(stored, operations, decision):
        raise ValueError("prepared recovery payload digest mismatch")


class LegacyRunError(ValueError):
    pass


@dataclass(frozen=True)
class MigrationRun:
    run_id: str
    run_dir: Path
    snapshot: dict

    @property
    def status(self):
        if self.snapshot["state"] in TERMINAL:
            return self.snapshot["state"]
        if any(wait["state"] == "open" for wait in self.snapshot["waits"].values()):
            return "waiting"
        return "running"


class MigrationOperations(DownstreamGateOrchestration, ReviewReworkOrchestration, ResearchOrchestration):
    def __init__(self, *, handlers=None, isolation_mode="process", clock=time.time,
                 memory_probe=None, memory_policy=None):
        # Overrides support local deterministic integration tests. CLI production
        # always uses process isolation and the deployment's real handlers.
        self.handlers = handlers
        self.isolation_mode = isolation_mode
        self.clock = clock
        self.memory_probe = memory_probe or host_memory_snapshot
        self.memory_policy = memory_policy or MemoryPolicy.from_env()
        self._dynamic_memory_demand = None

    def _memory_capacity(self, snapshot, header, app, scope, *, requested_stage="coder",
                         exclude_execution_ids=(), retained_memory_execution_ids=(),
                         additional_active=0, additional_reservation_bytes=0):
        excluded = set(exclude_execution_ids)
        retained = set(retained_memory_execution_ids)
        active = 0
        active_reservation_bytes = additional_reservation_bytes
        reused_slots = 0
        requested_policy = self.memory_policy.for_stage(requested_stage)
        if requested_policy is None:
            return 0
        for task in snapshot.get("tasks", {}).values():
            if not task.get("attempts"):
                continue
            attempt = task["attempts"][-1]
            command = attempt["command"]
            # Dependency-planned work owns no reservation. Once explicitly
            # dispatched, queued/pending-dispatch work reserves its declared
            # stage capacity even before the worker obtains a lease.
            if attempt["state"] not in {
                    "pending", "pending_dispatch", "queued", "running", "leased"} or (
                    command["execution_id"] in excluded
                    and command["execution_id"] not in retained):
                continue
            stage = command.get("payload", {}).get("stage_id")
            reservation = self.memory_policy.for_stage(stage)
            if reservation is not None:
                active_reservation_bytes += reservation.heavy_slot_bytes
            if stage in {"coder", "goal_prepare", "agent_rework"}:
                active += 1
                if command["execution_id"] in excluded:
                    reused_slots += 1
        active += additional_active
        observation = self.memory_probe()
        # An active reviewer yields its coder concurrency slot while waiting
        # synchronously for a nested rework, but its process keeps a real
        # memory lease. Count it for memory and add back only the yielded
        # concurrency slot. The stage-specific active reservations are charged
        # once, using the same classification as the worker lease.
        decision = self.memory_policy.decide(observation,
            hard_cap=header["request"].get("max_parallel_coders", 3) + reused_slots,
            active=active, slot_bytes=requested_policy.heavy_slot_bytes,
            active_reservation_bytes=active_reservation_bytes)
        if self.memory_probe is host_memory_snapshot:
            from .dynamic_memory import DynamicMemoryDemand
            if decision.reason in {"memory_pressure", "memory_capacity_exhausted"}:
                if self._dynamic_memory_demand is None:
                    self._dynamic_memory_demand = DynamicMemoryDemand()
                self._dynamic_memory_demand.update(
                    active_reservation_bytes + requested_policy.heavy_slot_bytes
                    + requested_policy.host_guard_bytes, observation)
            elif self._dynamic_memory_demand is not None:
                required = self._dynamic_memory_demand.required_bytes
                if (required is not None and type(observation.available_bytes) is int
                        and observation.available_bytes >= required):
                    self._dynamic_memory_demand.close()
        # Only transitions enter persistent state. Sampling byte counts or a
        # timestamp on every tick would recreate the audit-growth problem.
        waits = app.setdefault("memory_waits", {})
        if decision.reason not in {"admitted", "hard_cap_reached"}:
            wait = {"reason": decision.reason,
                "requested_stage": requested_stage,
                "worker_reservation_bytes": requested_policy.heavy_slot_bytes,
                "host_reservation_bytes": requested_policy.host_guard_bytes}
            if waits.get(scope) != wait:
                waits[scope] = wait
        else:
            waits.pop(scope, None)
        return decision.starts

    @staticmethod
    def _audit_action(root, run_id, action):
        record_event(root, "control:" + uuid.uuid4().hex, "control.requested",
                     {"action": action}, run_id=run_id)

    @staticmethod
    def _export_audit(root):
        try:
            export_isolated(root)
        except (OSError, ValueError) as error:
            import warnings
            warnings.warn(f"audit export unavailable: {error}", RuntimeWarning)

    @staticmethod
    def _header(root: Path, run_id: str):
        marker = root / "run.json"
        if (root / "control.sqlite3").exists():
            raise LegacyRunError("legacy Run retired; start a new Run in an empty directory")
        if not marker.is_file():
            raise LegacyRunError("not a ModPort v2 Run; old Runs cannot be resumed or migrated")
        header = read_json(marker)
        if header.get("run_id") != run_id and re.fullmatch(r"[A-Za-z0-9_.:-]+", run_id):
            archived = root / "artifacts" / "continuations" / run_id / "run.json"
            if archived.is_file():
                header = read_json(verified_path(root, {"path": archived.relative_to(root).as_posix()}))
        if header.get("format_version") != FORMAT_VERSION:
            raise LegacyRunError("unsupported Run format; no legacy compatibility")
        if header.get("run_id") != run_id or header.get("run_dir") != str(root):
            raise ValueError("Run identity or workspace does not match its frozen input")
        if not (root / "kernel.sqlite3").is_file() or not (root / "orchestrator.sqlite3").is_file():
            raise ValueError("Run storage is incomplete")
        return header

    def _recovery_target(self, sdk, state, runtime):
        """Return the activated SDK deployment for a recovered generation.

        The immutable Run input remains in ``run.json`` and ``state['input']``.
        A running recovered generation executes only under the deployment that
        the SDK persisted in its recovery record.
        """
        if int(state.get("generation", 0)) <= 0:
            return None
        summary = sdk.get_run_summary(state["run_id"])
        recovery_id = summary.get("recovery_id")
        if not recovery_id:
            raise ValueError("recovered Run has no persisted SDK recovery record")
        recovery = sdk.get_recovery(recovery_id)
        if recovery.get("status") != "activated":
            raise ValueError("recovered Run deployment is not activated")
        target = recovery.get("target_deployment")
        if not isinstance(target, dict) or target.get("registry_revision") != runtime.registry_revision:
            raise ValueError("recovered Run requires its persisted deployment to be installed")
        return target

    def _execution_header(self, header, state, runtime, sdk):
        """Overlay the active recovery deployment without changing frozen input."""
        target = self._recovery_target(sdk, state, runtime)
        if target is None:
            return header
        effective = json_copy(header)
        effective["registry_revision"] = target["registry_revision"]
        effective["sdk_identity"] = inspect_runtime(Path(header["run_dir"]))["module"]
        app = state.get("application_state") or {}
        recovery_deadline = app.get("recovery_deadline_epoch")
        if recovery_deadline is not None:
            effective["deadline_epoch"] = recovery_deadline
        overrides = app.get("recovery_budget_override")
        if isinstance(overrides, dict):
            request = json_copy(effective["request"])
            budget = request.setdefault("budget", {})
            for key, value in overrides.items():
                if key in {"max_seconds", "max_agent_assignments", "max_tokens", "max_rework_rounds",
                           "execution_max_attempts"}:
                    budget[key] = value
            effective["request"] = request
        return effective

    @contextmanager
    def session(self, run_dir, run_id, *, allow_terminal_deployment=False,
                allow_cancelled_recovery=False):
        root = Path(run_dir).resolve()
        header = self._header(root, run_id)
        frozen_version = header.get("definition", {}).get("workflow_version")
        expected_definition = compile_migration_workflow(
            MigrationRequest.from_mapping(header["request"]),
            version=frozen_version,
        ).to_dict()
        if frozen_version == WORKFLOW_VERSION:
            from .workflow_upgrade import validate_upgrade_definition
            expected_definition = validate_upgrade_definition(header)
        with open_runtime(root, handlers=self.handlers, isolation_mode=self.isolation_mode, now=self.clock,
                          memory_policy=self.memory_policy) as runtime:
            sdk = Orchestrator(root / "orchestrator.sqlite3", runtime.kernel, runtime=runtime, clock=self.clock)
            try:
                state = hydrate_run_snapshot(root, sdk.get_run(run_id))
                if state["definition"] != header["definition"] or state["input"] != header:
                    raise ValueError("authoritative SDK Run disagrees with the frozen input")
                # Only status/export may inspect a settled Run under a new deployment.
                # No workers start here; all execution/recovery paths remain strict.
                if not (allow_terminal_deployment and state["state"] in TERMINAL):
                    header = self._execution_header(header, state, runtime, sdk)
                    if header["definition"] != expected_definition:
                        raise ValueError("workflow rules differ from the frozen deployment")
                    if runtime.registry_revision != header["registry_revision"]:
                        app = state.get("application_state") or {}
                        pending_states = EXECUTION_TERMINAL | {"recovery_required"}
                        tasks_settled = all(
                            bool(task.get("attempts"))
                            and task["attempts"][-1].get("state") in pending_states
                            for task in state.get("tasks", {}).values())
                        recovery_execution_ids = {
                            task["attempts"][-1].get("command", {}).get("execution_id")
                            for task in state.get("tasks", {}).values()
                            if task.get("attempts")
                            and task["attempts"][-1].get("state") == "recovery_required"
                        }
                        open_waits = [wait for wait in state.get("waits", {}).values()
                                      if wait.get("state") == "open"]
                        only_recovery_waits = all(
                            isinstance(wait.get("payload"), dict)
                            and wait["payload"].get("execution_id") in recovery_execution_ids
                            for wait in open_waits)
                        only_recovery_waits = (
                            only_recovery_waits and bool(recovery_execution_ids)
                            and len(open_waits) == len(recovery_execution_ids))
                        cancelled_reconciliation = (
                            allow_cancelled_recovery
                            and app.get("user_cancelled") is True
                            and app.get("stop_reason") == "user_cancelled"
                            and tasks_settled and only_recovery_waits)
                        if not cancelled_reconciliation:
                            raise ValueError("handler deployment changed; resume with the matching implementation")
                for ref in header["initial_refs"].values():
                    verified_path(root, ref)
                from .desktop_state import publish_observation_binding
                publish_observation_binding(header, runtime)
                yield root, header, runtime, sdk
            finally:
                sdk.close()

    def submit(self, request: MigrationRequest, *, run_dir, run_id=None, parent=None,
               budget_overrides=None, budget_reason=None, inherit_harness=False, dependency_cache=None,
               artifact_handoff=None, model_policy=None) -> MigrationRun:
        from .model_policy import load_model_config, validate_model_config
        selected_models = (load_model_config() if model_policy is None
                           else validate_model_config(model_policy))
        request.validate()
        # Freeze host path defaults for every new submission, including public
        # API callers; a resumed driver must not choose a different skill store.
        from dataclasses import replace
        from .user_paths import skill_store
        request = replace(request, skill_store=str(skill_store(request.skill_store)))
        root = Path(run_dir).resolve()
        identifier = run_id or f"migration-{uuid.uuid4().hex}"
        if not re.fullmatch(r"[A-Za-z0-9_.:-]+", identifier):
            raise ValueError("invalid run_id")
        if root.exists() and any(root.iterdir()):
            raise ValueError("new Runs require an empty directory; existing data is never reused")
        handoff_manifest = None
        if artifact_handoff is not None:
            if parent is not None:
                raise ValueError("artifact-only handoff cannot inherit a scheduler parent")
            from .artifact_handoff import validate_handoff
            from .handoff_runtime import validate_handoff_request
            handoff_manifest = validate_handoff(artifact_handoff)
            validate_handoff_request(handoff_manifest, request.to_dict())
        if request.workflow_mode == "artifact_verification":
            if artifact_handoff is None:
                raise ValueError("artifact_verification requires an artifact-only handoff")
            if parent is not None:
                raise ValueError("artifact_verification cannot inherit a scheduler parent")
            if inherit_harness:
                raise ValueError("artifact_verification requires a fresh source contract")
            from .artifact_verification import validate_submission
            validate_submission(handoff_manifest)
        # Admission must not create artifacts in a not-yet-initialized Run.
        # submit-final records the footprint after the artifact directory exists.
        check_storage_budget(root, phase="submit", record=False)
        from .skill_runtime import validate_requested_skills
        validate_requested_skills(request.to_dict(), root)
        # Validate a new-format parent before writing the child directory.
        packet = self.build_failure_packet(parent) if parent is not None else None
        budget_override = None
        inherited = None
        if type(inherit_harness) is not bool:
            raise ValueError("inherit_harness must be a boolean")
        if packet is None and (budget_overrides is not None or budget_reason is not None
                or (inherit_harness and handoff_manifest is None) or dependency_cache is not None):
            raise ValueError("retry options require a settled parent Run")
        if inherit_harness and handoff_manifest is not None:
            from .handoff_runtime import handoff_harness_files
            handoff_harness_files(handoff_manifest)
        if packet is not None:
            from .retry_policy import build_harness_snapshot, validate_retry_request
            budget_override = validate_retry_request(packet["request"], request, budget_overrides, budget_reason,
                                                     dependency_cache=dependency_cache)
            packet["budget_override"] = budget_override
            packet["child_request"] = request.to_dict()
            if inherit_harness:
                inherited = build_harness_snapshot(parent.run_dir, packet["parent_refs"],
                    parent_run_id=parent.run_id, source_commit=request.source_revision)
        root.mkdir(parents=True, exist_ok=True)
        artifacts = root / "artifacts"
        artifacts.mkdir()
        dependency_refs = {}
        if request.dependency_cache is not None:
            from .dependency_build import prepare_dependency_seed
            dependency_refs = prepare_dependency_seed(request.dependency_cache, root)
        rubric = acceptance_rubric(workflow_version=WORKFLOW_VERSION)
        atomic_json(artifacts / "acceptance-rubric.json", rubric)
        refs = {"acceptance_rubric": {"path": "artifacts/acceptance-rubric.json",
                "sha256": file_digest(artifacts / "acceptance-rubric.json"), "media_type": "application/json",
                "metadata": {"rubric_sha256": rubric["rubric_sha256"]}}}
        refs.update(dependency_refs)
        handoff_metadata = None
        if handoff_manifest is not None:
            from .artifact_handoff import install_handoff
            installed = install_handoff(artifact_handoff, root)
            refs.update(installed["refs"])
            handoff_metadata = installed["metadata"]
            if request.workflow_mode == "artifact_verification":
                from .artifact_verification import install_artifact_input
                refs.update(install_artifact_input(root, handoff_manifest, refs))
            if inherit_harness:
                from .handoff_runtime import install_handoff_harness
                refs.update(install_handoff_harness(root, handoff_manifest, refs))
        rules_root = Path(__file__).parent / "rules"
        for key, name in (("agent_rules", "AGENT_RULES.md"), ("evidence_protocol", "EVIDENCE_PROTOCOL.md")):
            source = rules_root / name
            if not source.exists():
                source = Path(__file__).resolve().parents[2] / "docs" / name
            target = artifacts / "rules" / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(source.read_bytes())
            refs[key] = {"path": target.relative_to(root).as_posix(), "sha256": file_digest(target), "media_type": "text/markdown"}
        import shutil
        debug_source = Path(__file__).parent / "vendor_skills"
        for source in sorted(debug_source.rglob("*")):
            if source.is_file():
                relative = source.relative_to(debug_source)
                target = artifacts / "rules" / "debug-skills" / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source, target)
                refs["debug:" + relative.as_posix()] = {"path": target.relative_to(root).as_posix(),
                    "sha256": file_digest(target), "media_type": "text/plain"}
        from .client_harness import client_harness_support_files
        for relative, contents in client_harness_support_files(workflow_version=WORKFLOW_VERSION).items():
            target = artifacts / "harness-support" / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(contents, encoding="utf-8")
            refs["harness_support:" + relative] = {"path": target.relative_to(root).as_posix(),
                "sha256": file_digest(target), "media_type": "text/plain"}
        if packet is not None:
            # Copy referenced parent outputs into the child without inheriting the checkout.
            copied = {}
            for key, ref in packet.pop("parent_refs").items():
                source = verified_path(parent.run_dir, ref)
                target = artifacts / "parent-evidence" / source.relative_to(parent.run_dir)
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(source.read_bytes())
                copied[key] = {**ref, "path": target.relative_to(root).as_posix(), "sha256": file_digest(target)}
                refs[f"parent:{key}"] = copied[key]
            if inherited is not None:
                def copy_inherited(ref):
                    source = verified_path(parent.run_dir, ref)
                    target = artifacts / "parent-evidence" / source.relative_to(parent.run_dir)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(source.read_bytes())
                    return {**ref, "path": target.relative_to(root).as_posix(), "sha256": file_digest(target)}
                inherited["provenance_ref"] = copy_inherited(inherited["provenance_ref"])
                for entry in inherited["files"]:
                    entry["ref"] = copy_inherited(entry["ref"])
                    refs["inherited_harness:" + entry["path"]] = entry["ref"]
                inherited.pop("snapshot_sha256", None)
                inherited["snapshot_sha256"] = digest(inherited)
                target = artifacts / "inherited-harness.json"
                atomic_json(target, inherited)
                refs["inherited_harness"] = {"path": target.relative_to(root).as_posix(),
                    "sha256": file_digest(target), "media_type": "application/json"}
                packet["inherited_harness"] = refs["inherited_harness"]
            packet["copied_evidence"] = copied
            packet["packet_sha256"] = digest(packet)
            atomic_json(artifacts / "failure-packet.json", packet)
            refs["failure_packet"] = {"path": "artifacts/failure-packet.json", "sha256": file_digest(artifacts / "failure-packet.json"), "media_type": "application/json"}
        definition = compile_migration_workflow(request).to_dict()
        check_storage_budget(root, phase="submit-final")
        with open_runtime(root, handlers=self.handlers, isolation_mode=self.isolation_mode, now=self.clock,
                          memory_policy=self.memory_policy) as runtime:
            refs = {key: seal_ref(root, ref, execution_id="submission") for key, ref in refs.items()}
            now = self.clock()
            header = {"format_version": FORMAT_VERSION, "run_id": identifier, "run_dir": str(root),
                      "request": request.to_dict(), "definition": definition,
                      "model_policy": selected_models,
                      "watchdog_policy": {"enabled": True, "inactivity_seconds": 600},
                      "registry_revision": runtime.registry_revision,
                      "sdk_identity": inspect_runtime(root)["module"],
                      "started_at": now,
                      "deadline_epoch": None if request.budget.max_seconds is None else now + request.budget.max_seconds,
                      "rubric_sha256": rubric["rubric_sha256"], "initial_refs": refs,
                      "parent_run_id": None if parent is None else parent.run_id,
                      "budget_override": budget_override, "inherit_harness": inherit_harness,
                      "prior_findings": [] if packet is None else packet["findings"]}
            if handoff_metadata is not None:
                header["artifact_handoff"] = handoff_metadata
            header["header_sha256"] = digest(header)
            atomic_json(root / "run.json", header)
            atomic_json(root / "workflow-definition.json", definition)
            sdk = Orchestrator(root / "orchestrator.sqlite3", runtime.kernel, runtime=runtime, clock=self.clock)
            try:
                state = sdk.create_run(identifier, command_id="create", input=header, definition=definition)
            finally:
                sdk.close()
        self._audit_action(root, identifier, "submit")
        return MigrationRun(identifier, root, state)

    @staticmethod
    def _new_application():
        return {"active_stage": None, "effective": {}, "processed": [], "history": [],
                "rounds": {}, "agent_assignments": 0, "locked_artifacts": {},
                "rework_context": None, "rework_evidence": {}, "knowledge_gap_context": None, "stop_reason": None, "stop_state": None,
                "cancel_sent": [], "terminal_reason": None, "active_group": None, "development_generation": 0,
                "planning_generation": 0, "repair_generation": 0, "repair_scope": None,
                "plan_documents": {},
                "repair_context": None, "repair_history": [],
                "repair_cycle_results": {},
                "repair_feedback": [],
                "format_context": None, "format_retries": {}, "diagnostic_history": {},
                "stagnant_failures": {}, "diagnosis_escalated": False,
                "supervision": {"scheduled_windows": [], "processed": [], "results": [],
                                "directive": None, "highest_applicable_window": 0},
                "research_budget": {}, "research_attempts": {}, "knowledge_revisions": {},
                "project_research_gaps": {}, "project_verification_gaps": {}, "gap_revision": 0,
                "support_pending": [], "administrator_wait_seconds": 0, "admin_imports": {}}

    @staticmethod
    def _refs(header, app):
        refs = {**header["initial_refs"], **app.get("rework_evidence", {}), **app.get("admin_artifact_refs", {})}
        for result in app["effective"].values():
            produced = result["outputs"].get("artifact_refs", {})
            scope = result["outputs"].get("regression_scope")
            if isinstance(scope, dict):
                produced = {f"regression:{result['task_id']}:{key}": ref
                            for key, ref in produced.items()}
            refs.update(produced)
            # Later acceptance builds reuse some artifact aliases. Preserve the
            # exact due-stage evidence for independently closing deferred gaps.
            if result.get("stage_id") in VERIFICATION_STAGES:
                refs.update({f"gap_evidence:{result['stage_id']}:{key}": ref for key, ref in produced.items()})
        if app.get("rework_context"):
            prior = app["rework_context"]["result"]["outputs"].get("artifact_refs", {})
            refs.update({f"rework:{key}": ref for key, ref in prior.items()})
        return refs

    @staticmethod
    def _knowledge_gaps(app):
        rows = app.get("unresolved_knowledge_gaps", app["effective"].get("mod_analysis", {}).get("outputs", {}).get("unresolved_relevant_gaps", []))
        return [{**row, "gap_id": gap_identity(row)} for row in rows
                if app.get("project_research_gaps", {}).get(gap_identity(row), {}).get(
                    "project_status", "unresolved") == "unresolved"]

    def _schedule(self, snapshot, header, app, stage, *, causation_id=None,
                  task_id=None, dependencies=None, payload=None, extra_options=None, activate=True,
                  artifact_overrides=None, upstream_overrides=None, prior_findings_override=None):
        from .workflow import SOURCE_HARNESS_STAGES
        if source_reading_policy(header) and stage in SOURCE_HARNESS_STAGES:
            return self._finish(app, 'source_harness_disabled_by_policy')
        supervisor_options = {}
        directive = app.setdefault("supervision", {}).get("directive")
        if (not business_gates_disabled(header) and stage in AGENT_STAGES
                and not app.get("active_group") and isinstance(directive, dict)):
            intervention = directive["decision"].get("intervention", {})
            selected_stage = intervention.get("stage")
            # A supervisor can choose instructions, an existing task bundle and
            # an agent profile, but it cannot bypass the workflow graph. Apply
            # the directive only when the host naturally reaches its selected
            # stage (or when no stage preference was supplied).
            if selected_stage is None or selected_stage == stage:
                payload = {**(payload or {}), "supervisor_intervention": {
                    "window_end": directive["window_end"],
                    "decision": directive["decision"]["decision"],
                    "reason": directive["decision"]["reason"],
                    "prompt": intervention.get("prompt"),
                    "task_ids": intervention.get("task_ids", []),
                    "profile": intervention.get("profile"),
                }}
                model, effort = agent_model_policy(header.get("definition", {}).get("workflow_version"), model_policy=header.get("model_policy"))
                profiles = {
                    role: {"model": model, "reasoning_effort": effort}
                    for role in ("planner", "coder", "reviewer", "researcher")
                }
                supervisor_options = profiles.get(intervention.get("profile"), {})
                directive["applied_to_stage"] = stage
                app["supervision"]["directive"] = None
        if (task_id is None and app.get("regression_generation", 0)
                and header.get("definition", {}).get("workflow_version", 11) >= 12
                and stage in {"acceptance_preflight", "acceptance_build", "client_smoke", "gap_review", "delivery"}):
            task_id = f"{stage}.g{app['regression_generation']}"
        task_id = task_id or stage
        deadline = self._effective_deadline(header, app)
        requested_deadline = (extra_options or {}).get('deadline_epoch')
        if (header.get('definition', {}).get('workflow_version', 0) >= 26
                and type(requested_deadline) in (int, float)
                and math.isfinite(requested_deadline)):
            deadline = min(deadline, requested_deadline) if deadline is not None else requested_deadline
        if deadline is not None and self.clock() >= deadline:
            return []
        budget = header["request"]["budget"]
        research_kinds = []
        if stage in BUDGETED_RESEARCH_STAGES:
            if stage == "gap_research":
                research_kinds = eligible_kinds(app, self._knowledge_gaps(app))
            else:
                kind = stage.split("_", 1)[0]
                if kind not in app.setdefault("research_budget", {}):
                    initialize_research(app, {"missing_kinds": [kind]})
                research_kinds = [kind] if remaining(app, kind) else []
            if not research_kinds:
                app["gap_failure"] = "research_budget_exhausted"
                return []
        if (agent_stage(header, stage) or stage == SUPERVISOR_STAGE
                and header.get('definition', {}).get('workflow_version', 0) >= 26):
            from .token_budget import initialize_token_budget, read_token_budget
            initialize_token_budget(header["run_dir"], budget.get("max_tokens"))
            if read_token_budget(header["run_dir"])["exhausted"]:
                app["stop_reason"], app["stop_state"] = "token_budget_exhausted", "failed"
                return []
            limit = budget["max_agent_assignments"]
            if limit is not None and app["agent_assignments"] >= limit:
                if app.get("active_group") or app.get("early_active"):
                    app["stop_reason"], app["stop_state"] = "agent_assignment_budget_exhausted", "failed"
                    return []
                return self._finish(app, "agent_assignment_budget_exhausted")
            app["agent_assignments"] += 1
        previous = snapshot["tasks"].get(task_id)
        attempt = len(previous["attempts"]) + 1 if previous else 1
        execution_id = f"{snapshot['run_id']}:{task_id}:{attempt}"
        if stage in BUDGETED_RESEARCH_STAGES and research_kinds:
            record_dispatch(app, stage, execution_id, research_kinds)
        if stage == "research_review":
            producer_id = app["effective"].get("gap_research", {}).get("command_id")
            research_kinds = app.get("research_attempts", {}).get(producer_id, {}).get("kinds", [])
        is_model_stage = agent_stage(header, stage) or stage == SUPERVISOR_STAGE
        workflow_version = header.get("definition", {}).get("workflow_version", WORKFLOW_VERSION)
        model, effort = agent_model_policy(workflow_version, stage, header.get("model_policy"))
        options = {"workflow_version": workflow_version, "deadline_epoch": deadline,
                   "acceptance_rubric_sha256": header["rubric_sha256"],
                   "model": model if is_model_stage else None,
                   "reasoning_effort": effort if is_model_stage else None,
                   "agent_assignment": app["agent_assignments"],
                   "rework_round": app["rounds"].get(stage, 0)}
        validation_policy = header.get("definition", {}).get("validation_policy")
        if isinstance(validation_policy, Mapping):
            options["validation_policy"] = json_copy(validation_policy)
        progress_policy = header.get("definition", {}).get("progress_supervision_policy")
        if isinstance(progress_policy, Mapping):
            options["progress_supervision_policy"] = json_copy(progress_policy)
        dialogue_policy = header.get("definition", {}).get("agent_dialogue_policy")
        if dialogue_policy is not None:
            options["agent_dialogue_policy"] = json_copy(dialogue_policy)
        if business_gates_disabled(header):
            options["business_gates_disabled"] = True
        if downstream_toolcall(header):
            options["gate_policy"] = "downstream_toolcall"
        if stage in {"platform_diff", "java_diff", "platform_skill_review", "java_skill_review"}:
            options["workspace"] = "workspaces/skills/" + stage.split("_")[0]
        if stage in {"test_design", "test_review", "test_execute"}:
            design = app["effective"].get("test_design", {}).get("outputs", {})
            options["workspace"] = design.get("workspace", f"workspaces/tests/{app['agent_assignments']}")
        if self._final_cleanup_enabled(header) and app.get('final_cleanup', {}).get('phase') == 'revalidating':
            options['final_cleanup_revalidation'] = True
        options.update(extra_options or {})
        options.update(supervisor_options)
        if header.get("model_policy") is not None:
            options["model_policy"] = json_copy(header["model_policy"])
        else:
            options.pop("model_policy", None)
        if is_model_stage and workflow_version >= 15:
            options["model"], options["reasoning_effort"] = model, effort
        findings = list(header["prior_findings"] if prior_findings_override is None
                        else prior_findings_override)
        if app["rework_context"]:
            findings += app["rework_context"].get("findings", [])
        context = app.get("repair_context")
        migration_repair = bool(context and context["repair_scope"] == "migration")
        repair_stage = (stage in (*REPAIR_PLANNING_STAGES, "contract_revise", "target_revise",
                                 "contract_repair_integrate", "target_repair_integrate")
                        or bool(context) and stage == "code_cleanup"
                        and context["repair_scope"] == "target"
                        or bool(context) and stage in {"goal_prepare", "coder"}
                        and context["repair_scope"] == (payload or {}).get("goal_scope")
                        or migration_repair and stage in (*PLANNING_STAGES,
                            "implementation", "development_prepare", "development_prepare_integrate", "coder"))
        refs = self._refs(header, app)
        workflow_version = header.get("definition", {}).get("workflow_version", WORKFLOW_VERSION)
        plan_scope = (stage.split("_", 1)[0] if stage.startswith(("contract_", "target_"))
                      else (payload or {}).get("goal_scope", "migration"))
        plan_state = app.get("plan_documents", {}).get(plan_scope)
        expected_plan_generation = (app.get("planning_generation", 0) if plan_scope == "migration"
                                    else app.get("repair_generation", 0))
        generation_field = "planning_generation" if plan_scope == "migration" else "repair_generation"
        if (isinstance(plan_state, dict)
                and plan_state.get(generation_field) != expected_plan_generation):
            app.setdefault("plan_documents", {}).pop(plan_scope, None)
            plan_state = None
        if workflow_version >= 15 and stage in {
                "migration_inventory", "contract_diagnose", "target_diagnose"}:
            app.setdefault("plan_documents", {}).pop(plan_scope, None)
            plan_state = None
        if isinstance(plan_state, dict) and isinstance(plan_state.get("current_ref"), dict):
            refs["current_plan"] = json_copy(plan_state["current_ref"])
        rework = app["rework_context"]
        if repair_stage and context:
            rework = context["current_failure"]
            refs = json_copy(context["artifact_refs"])
            refs.pop("repair_work_package", None)
            planners = (PLANNING_STAGES + ("development_prepare",) if migration_repair
                        else tuple(s for s in REPAIR_PLANNING_STAGES
                                   if s.startswith(context["repair_scope"] + "_")))
            for planner in (*REPAIR_PLANNING_STAGES, *(PLANNING_STAGES if migration_repair else ())):
                refs.pop(planner, None)
            if migration_repair:
                refs.pop("development_plan", None)
                refs.pop("development_prepare", None)
            # Only canonical documents settled in this cycle can augment the
            # original inputs. Live source/contract aliases never override them.
            for planner in planners:
                result = app["effective"].get(planner, {})
                if (result.get("status") == "completed"
                        and result.get("command_id") in app.get("repair_cycle_results", {})):
                    ref = result.get("outputs", {}).get("artifact_refs", {}).get(planner)
                    if ref is not None:
                        refs[planner] = ref
                    extras = ("development_plan", "development_prepare", "repair_work_package") if migration_repair else (
                        # The Markdown repair chain emits the canonical
                        # development plan with the tasks pass.  The review
                        # pass consumes it, so carry both aliases forward.
                        ("development_plan", "repair_work_package") if planner.endswith("_repair_tasks") else
                        ("development_plan",) if planner.endswith("_repair_review") else ())
                    for alias in extras:
                        ref = result.get("outputs", {}).get("artifact_refs", {}).get(alias)
                        if ref is not None:
                            refs[alias] = ref
            for feedback in app.get("repair_feedback", []):
                for alias, ref in feedback["result"].get("outputs", {}).get("artifact_refs", {}).items():
                    refs.setdefault(f"rework_evidence:{feedback['execution_id']}:{alias}", ref)
        if repair_stage:
            refs.update(header.get("continuation", {}).get("support_refs", {}))
            # The diagnostic packet keeps its original rules as provenance;
            # agents follow the procedural rules frozen for this segment.
            for key in ("agent_rules", "evidence_protocol"):
                if key in header["initial_refs"]:
                    refs[key] = json_copy(header["initial_refs"][key])
            from .host_interface import publish_host_interface
            refs["continuation:host_launch_interface"] = publish_host_interface(
                Path(header["run_dir"]), execution_id)
            for failure in app.get("continuation_feedback", {}).get("failed_results", []):
                for alias, ref in failure.get("outputs", {}).get("artifact_refs", {}).items():
                    refs[f"continuation:{failure['command_id']}:{alias}"] = ref
        if isinstance(plan_state, dict) and isinstance(plan_state.get("current_ref"), dict):
            refs["current_plan"] = json_copy(plan_state["current_ref"])
        refs.update(artifact_overrides or {})
        if workflow_version >= 26 and stage in {'coder', 'goal_prepare'}:
            from .supervised_goals import target_key
            task = (payload or {}).get('development_task')
            plan_ref = refs.get('development_plan')
            if isinstance(task, dict) and isinstance(plan_ref, dict):
                revision = app.get('supervision', {}).get('goal_revisions', {}).get(
                    target_key(task, plan_ref))
                if isinstance(revision, dict):
                    refs['supervised_goal_revision'] = json_copy(revision['revision_ref'])
        if downstream_toolcall(header) or business_gates_disabled(header):
            refs.update(header.get("continuation", {}).get("support_refs", {}))
            for alias in ("agent_rules", "evidence_protocol"):
                if alias in header["initial_refs"]:
                    refs[alias] = json_copy(header["initial_refs"][alias])
        contract_rework_followup = (workflow_version >= 26
            and stage in {'contract_verify', 'contract_review', 'contract_freeze'}
            and isinstance((payload or {}).get('reviewer_rework'), dict))
        upstream = {**app['effective'], **(upstream_overrides or {})}
        locked_for_command = json_copy(app['locked_artifacts'])
        if contract_rework_followup:
            # Preserve the old result in SDK history, but do not present its
            # lock or review as evidence for the candidate under rework.
            refs.pop('functional_contract_lock', None)
            refs.pop('baseline_test_selection', None)
            upstream.pop('contract_freeze', None)
            for key in ('contract_lock_sha256', 'contract_sha256'):
                locked_for_command.pop(key, None)
            if stage != 'contract_freeze':
                refs.pop('contract_review', None)
                upstream.pop('contract_review', None)
        if workflow_version >= 15 and stage in {
                "migration_inventory", "contract_diagnose", "target_diagnose"}:
            # A new draft is the first document in its generation.  Never let
            # an effective result from an older cycle smuggle in current_plan.
            refs.pop("current_plan", None)
        if stage == "gate_handoff" or business_gates_disabled(header):
            refs, unavailable = available_refs(header["run_dir"], refs)
            payload = {**(payload or {}), "unavailable_artifact_refs": unavailable}
        if workflow_version >= 40:
            from .diagnostic_repair_routing import bind
            payload = bind(app, stage, payload, refs)
        operation = OperationInput(
            run_id=header.get("logical_run_id", snapshot["run_id"]), task_id=task_id, stage_id=stage, command_id=execution_id,
            run_dir=header["run_dir"], attempt=attempt,
            payload={"request": header["request"], "locked_artifacts": locked_for_command,
                     "rework_context": app["rework_context"],
                     "planning_generation": app.get("planning_generation", 0),
                     "plan_refinement_round": (plan_state or {}).get("rounds", 0),
                     "repair_generation": app.get("repair_generation", 0),
                     "repair_scope": app.get("repair_scope"),
                     "format_context": app.get("format_context"),
                     "diagnosis_escalated": app.get("diagnosis_escalated", False),
                     "knowledge_gap_context": app.get("knowledge_gap_context"),
                     "unresolved_knowledge_gaps": ([] if stage.startswith("contract_")
                        or (payload or {}).get("goal_scope") == "contract" else self._knowledge_gaps(app)),
                     "known_knowledge_gap_ids": app.get("known_knowledge_gap_ids", []),
                     "gap_identity_aliases": json_copy(app.get("gap_identity_aliases", {})),
                     "research_kinds": research_kinds,
                     "gate_diagnostics": diagnostic_context(app),
                     "research_budget": app.get("research_budget", {}),
                     "knowledge_revisions": app.get("knowledge_revisions", {}),
                     "project_research_gaps": list(app.get("project_research_gaps", {}).values()),
                     "approved_gap_resolutions": app.get("approved_gap_resolutions", []),
                     "gap_obligations": list(app.get("project_verification_gaps", {}).values()) or app["effective"].get("mod_analysis", {}).get("outputs", {}).get("deferred_verification_gaps", []),
                     **(payload or {}),
                     "rework_context": json_copy(rework),
                     "repair_context": json_copy(context) if repair_stage else None,
                     "continuation_feedback": json_copy(app.get("continuation_feedback")) if repair_stage else None,
                     "repair_feedback": json_copy(app.get("repair_feedback", []))
                     if context and repair_stage else []},
            options=options, upstream_results=upstream, artifact_refs=refs,
            prior_findings=tuple(findings))
        if stage == "gate_handoff" or business_gates_disabled(header):
            from dataclasses import replace
            operation = replace(operation,
                payload=diagnostic_view(header["run_dir"], operation.payload),
                upstream_results=diagnostic_view(header["run_dir"], operation.upstream_results))
        from .rework_tools import rework_targets
        targets = rework_targets(snapshot, operation)
        if targets:
            from dataclasses import replace
            operation = replace(operation, payload={**operation.payload, 'review_rework_targets': targets})
        from .rework_tools import interactive_review_timeout_cap
        timeout = (interactive_review_timeout_cap(operation, now=self.clock())
                   if is_model_stage else 900.0 if stage == "client_smoke" else 7200.0)
        if deadline is not None:
            timeout = max(0.01, min(timeout, deadline - self.clock()))
        command = ExecutionCommandV2(
            execution_id=execution_id, idempotency_key=execution_id,
            registry_revision=header["registry_revision"], correlation_id=operation.run_id,
            causation_id=causation_id, handler_id=f"modport.{stage}", handler_contract_version=1,
            retry_policy=RetryPolicy(max_attempts=1 if stage in BUDGETED_RESEARCH_STAGES else budget["execution_max_attempts"],
                                     initial_backoff_seconds=1.0, backoff_multiplier=2.0,
                                     max_backoff_seconds=30.0),
            timeout_seconds=timeout, payload=pack_input(header["run_dir"], operation.to_dict())).to_dict()
        if (stage == "contract_verify" and dependencies is None and "contract_draft" not in snapshot["tasks"]
                and "contract_draft" not in header.get("continuation", {}).get("carried_stages", [])):
            # Inherited harnesses enter through restoration instead of drafting.
            dependencies = ("contract_restore",)
        if previous:
            operations = [{"kind": "new_attempt", "task_id": task_id, "command": command}]
        else:
            if dependencies is None:
                dependencies = stage_routes(header)[3][stage]
                if stage == 'test_execute' and options['workflow_version'] < 13:
                    dependencies = ('test_design',)
                if header.get("definition", {}).get("workflow_version", 11) >= 12:
                    dependencies = [app["effective"].get(dependency, {}).get("task_id", dependency)
                                    for dependency in dependencies]
            dependencies = list(dependencies)
            carried = {key for key in header.get("continuation", {}).get("carried_stages", [])
                       if app["effective"].get(key, {}).get("status") == "completed"
                       and (key not in REVIEW_STAGES or
                            app["effective"][key].get("outputs", {}).get("verdict") == "approved")}
            if downstream_toolcall(header):
                carried.update(app["effective"][key].get("task_id", key) for key in tuple(carried))
                admitted = {key for key, value in app["effective"].items() if forwarded(app, value)}
                admitted.update(value.get("task_id") for value in app["effective"].values()
                                if forwarded(app, value))
                # SDK success dependencies cannot represent diagnostic use of
                # a failed (or previous-segment) task. Its real result is still
                # supplied in upstream_results; no success is fabricated.
                dependencies = [dependency for dependency in dependencies if dependency not in admitted]
            if business_gates_disabled(header) and stage != 'coder_revival_plan':
                # The host still reaches stages in its frozen order. SDK success
                # dependencies cannot express consumption of a failed diagnostic,
                # so v17 uses application routing and passes the real result in
                # upstream_results without fabricating a successful predecessor.
                dependencies = []
            dependencies = [dependency for dependency in dependencies
                            if dependency in snapshot["tasks"] or dependency not in carried]
            operations = [{"kind": "add_task", "task_id": task_id, "command": command,
                           "dependencies": dependencies}]
        from .watchdog_events import enabled as watchdog_enabled
        if watchdog_enabled(header, app) and stage != 'supervisor':
            operations.append({'kind': 'watch_task', 'task_id': task_id,
                'watch_id': execution_id, 'target': {'run_id': header['run_id'], 'task_id': task_id}})
        operations.append({"kind": "dispatch", "task_id": task_id})
        if activate:
            app["active_stage"] = task_id
        return operations

    @staticmethod
    def _supervision_records(snapshot, header=None):
        """Project SDK attempts into assignment-ordered, host-observed evidence."""
        records = []
        for task_id, task in snapshot["tasks"].items():
            for attempt in task["attempts"]:
                command = OperationInput.from_dict(attempt["command"]["payload"])
                if (command.stage_id == SUPERVISOR_STAGE or
                        (not agent_stage(header, command.stage_id) if header is not None
                         and header.get('definition', {}).get('workflow_version', 0) >= 26
                         else command.stage_id not in AGENT_STAGES)):
                    continue
                assignment = command.options.get("agent_assignment")
                if type(assignment) is not int or assignment <= 0:
                    continue
                value = (attempt.get("result") or {}).get("value")
                full_result = value if isinstance(value, dict) else None
                outputs = full_result.get("outputs", {}) if full_result else {}
                detail = full_result.get("detail") if full_result else None
                result = None if full_result is None else {
                    "status": full_result.get("status"),
                    "error_code": full_result.get("error_code"),
                    "detail": detail[:4000] if isinstance(detail, str) else "",
                    "outputs": {key: outputs.get(key) for key in
                                ("verdict", "parallel_decision", "supervision_window")
                                if key in outputs},
                }
                location = None
                if isinstance(detail, str):
                    match = re.search(r"(?:[A-Za-z0-9_.-]+/)+[A-Za-z0-9_.-]+", detail)
                    location = match.group(0) if match else None
                blocker = None
                if result and (result.get("status") in {"failed", "blocked"} or result.get("error_code")):
                    blocker = {"stage": command.stage_id,
                               "error": result.get("error_code") or result.get("status")}
                    if location:
                        blocker["file"] = location
                refs = outputs.get("artifact_refs", {})
                projected_refs = {}
                if isinstance(refs, dict):
                    for alias in sorted(refs)[:64]:
                        ref = refs[alias]
                        projected_refs[alias] = ({key: ref.get(key) for key in ("path", "sha256")
                                                  if key in ref} if isinstance(ref, dict) else ref)
                records.append((assignment, {
                    "execution_id": command.command_id, "task_id": task_id,
                    "stage": command.stage_id, "state": attempt["state"],
                    "result": result,
                    "artifact_refs": json_copy(projected_refs),
                    "progress": {}, "blocker": blocker,
                }))
                if command.options.get('workflow_version', 0) >= 26:
                    from .supervision import execution_refs
                    projected_refs.update(execution_refs(command))
                    records[-1][1]['artifact_refs'] = json_copy(projected_refs)
        records.sort(key=lambda item: item[0])
        return [record for _, record in records]

    def _progress_supervision_decision(self, snapshot, header, app):
        from .progress_routing import decide
        return decide(self, snapshot, header, app)

    def _supervision_decision(self, snapshot, header, app):
        """Observe finished reviews and launch each five-assignment review once.

        Supervisors are ordinary SDK tasks but are excluded from business
        assignment counters. Their execution never blocks the next business
        round. A validated directive is consumed at a later agent scheduling
        boundary; pause remains an immediate host cancellation decision.
        """
        if progress_supervised(header):
            # The current path samples before recovery/rework early returns.
            return []
        from .supervision import build_evidence_packet, due_windows, deep_supervision

        deep = deep_supervision(header)

        state = app.setdefault("supervision", {"scheduled_windows": [], "processed": [],
                                               "results": [], "directive": None,
                                               "highest_applicable_window": 0})
        state.setdefault("highest_applicable_window", 0)
        operations = []
        supervisor_tasks = []
        for task_id, task in snapshot["tasks"].items():
            attempt = task["attempts"][-1]
            command = OperationInput.from_dict(attempt["command"]["payload"])
            if command.stage_id == SUPERVISOR_STAGE and 'desktop_chat' not in command.payload:
                supervisor_tasks.append((int(command.payload.get("supervision_window", 0)),
                                         task_id, task))
        # Apply the newest completed window first when several asynchronous
        # supervisors settle between host decisions. Older arrivals are still
        # archived below, but cannot replace or cancel a newer intervention.
        supervisor_tasks.sort(reverse=True, key=lambda item: item[0])
        for _, task_id, task in supervisor_tasks:
            attempt = task["attempts"][-1]
            command = OperationInput.from_dict(attempt["command"]["payload"])
            if attempt["state"] not in EXECUTION_TERMINAL:
                continue
            if command.command_id in state["processed"]:
                continue
            state["processed"].append(command.command_id)
            evidence = {}
            if deep:
                from .supervision import execution_refs
                raw_result = (attempt.get('result') or {}).get('value') or {}
                if not isinstance(raw_result, dict):
                    raw_result = {}
                evidence = {'execution_id': command.command_id,
                            'detail': raw_result.get('detail'),
                            'execution_error': json_copy(attempt.get('error')),
                            'artifact_refs': execution_refs(command, raw_result.get('outputs', {}))}
            if attempt["state"] != "succeeded":
                state["results"].append({"window_end": command.payload.get("supervision_window"),
                                         **evidence,
                                         "status": "execution_" + attempt["state"]})
                continue
            outcome = OperationResult.from_dict(attempt["result"]["value"])
            outcome.validate_for(command)
            if outcome.status != "completed":
                state["results"].append({"window_end": command.payload.get("supervision_window"),
                    **evidence,
                    "status": outcome.status, "error_code": outcome.error_code})
                continue
            window_end = command.payload["supervision_window"]
            if deep:
                from .evidence import verified_path
                from .supervised_goals import target_key
                revisions = state.setdefault('goal_revisions', {})
                diagnostics = []
                authorized = {item['key']: item for item in command.payload.get('supervised_goal_targets', [])}
                for revision in outcome.outputs.get('supervised_goal_revisions', []):
                    try:
                        from hashlib import sha256
                        ref = revision['revision_ref']
                        raw = verified_path(Path(header['run_dir']), ref).read_bytes()
                        if sha256(raw).hexdigest() != ref.get('sha256'):
                            raise ValueError('supervised goal revision digest mismatch')
                        document = json.loads(raw)
                        key = revision['key']
                        target = authorized[key]
                        if (key != target_key(target['task'], target['plan_ref'])
                                or document['supervisor_execution_id'] != command.command_id
                                or document['task_id'] != target['task']['id']
                                or document['plan_ref'] != target['plan_ref']
                                or document['original_objective'] != target['task']['objective']):
                            raise ValueError('supervised goal revision identity mismatch')
                        if (document.get('previous_revision_ref')
                                != revisions.get(key, {}).get('revision_ref')):
                            raise ValueError('supervised goal revision is based on a superseded document')
                        if revisions.get(key, {}).get('window_end', 0) < window_end:
                            revisions[key] = {'revision_ref': json_copy(ref), 'window_end': window_end}
                    except (OSError, ValueError, KeyError, TypeError) as exc:
                        diagnostics.append(str(exc))
                state['results'].append({'window_end': window_end, 'status': 'completed',
                    **evidence,
                    'revision_diagnostics': diagnostics})
                continue
            decision = outcome.outputs.get("supervisor_decision")
            if not isinstance(decision, dict):
                state["results"].append({"window_end": window_end, "status": "failed",
                                         "error_code": "supervision_output_invalid"})
                continue
            decision = json_copy(decision)
            state["results"].append({"window_end": window_end, "status": "completed",
                                     "decision": decision})
            if window_end <= state["highest_applicable_window"]:
                continue
            state["highest_applicable_window"] = window_end
            if decision["decision"] == "pause":
                app["stop_reason"], app["stop_state"] = "supervisor_pause", "cancelled"
            elif (decision["decision"] in {"targeted_fix", "replan"}
                  and not downstream_toolcall(header)):
                state["directive"] = {"window_end": window_end, "decision": decision}
            else:
                state["directive"] = None

        records = self._supervision_records(snapshot, header)
        improvements = []
        for result in state["results"]:
            for item in (result.get("decision") or {}).get("process_improvements", []):
                if item not in improvements:
                    improvements.append(item)
        for window_end in due_windows(len(records), state["scheduled_windows"]):
            packet = build_evidence_packet(records, window_end=window_end,
                                           process_improvements=improvements[-20:])
            allowed_tasks = sorted({str(row.get("task_id")) for row in packet["attempts"]
                                    if row.get("task_id")})
            group = app.get("active_group") or {}
            allowed_tasks += [str(task["id"]) for task in group.get("tasks", [])
                              if str(task["id"]) not in allowed_tasks]
            extra_payload = {}
            if deep:
                from .supervision import goal_targets
                extra_payload['supervised_goal_targets'] = goal_targets(snapshot, app, packet)
                packet['previous_supervision'] = [
                    {'window_end': item['window_end'], 'execution_id': item.get('execution_id'),
                     'status': item['status'], 'detail': item.get('detail'),
                     'execution_error': item.get('execution_error'),
                     'artifact_refs': item.get('artifact_refs', {})}
                    for item in state['results'][-5:]]
            scheduled = self._schedule(snapshot, header, app, SUPERVISOR_STAGE,
                task_id=f"supervisor.window.{window_end}", dependencies=[], activate=False,
                payload={"supervision_window": window_end, "supervision_packet": packet,
                         **extra_payload,
                         "supervisor_allowed_stages": sorted(AGENT_STAGES),
                         "supervisor_allowed_profiles": ["planner", "coder", "reviewer", "researcher"],
                         "supervisor_allowed_task_ids": allowed_tasks})
            if any(op['kind'] in {'add_task', 'new_attempt'} for op in scheduled):
                state['scheduled_windows'].append(window_end)
            operations += scheduled
            if app.get('stop_reason') or any(op['kind'] == 'finish' for op in scheduled):
                break
        return operations

    @staticmethod
    def _finish(app, reason, state="failed"):
        app.pop("memory_waits", None)
        app["terminal_reason"] = reason
        app["active_stage"] = None
        app["active_group"] = None
        return [{"kind": "finish", "state": state}]

    @staticmethod
    def _flowthrough_diagnostic(app, result, *, note=None):
        outputs = result.get("outputs", {}) if isinstance(result, dict) else {}
        diagnostic = {
            "stage": result.get("stage_id"), "task_id": result.get("task_id"),
            "execution_id": result.get("command_id"), "status": result.get("status"),
            "error_code": result.get("error_code"), "verdict": outputs.get("verdict"),
            "diagnostic_code": outputs.get("diagnostic_code"),
            "detail": result.get("detail", ""),
            "artifact_refs": json_copy(outputs.get("artifact_refs", {})),
        }
        if note:
            diagnostic["routing_note"] = note
        rows = app.setdefault("business_diagnostics", [])
        identity = (diagnostic["execution_id"], diagnostic.get("routing_note"))
        if not any((row.get("execution_id"), row.get("routing_note")) == identity for row in rows):
            rows.append(diagnostic)

    def _flowthrough_outcome(self, attempt):
        command = OperationInput.from_dict(attempt["command"]["payload"])
        if attempt["state"] == "succeeded":
            try:
                outcome = OperationResult.from_dict(attempt["result"]["value"])
                outcome.validate_for(command)
                return command, outcome
            except (KeyError, TypeError, ValueError) as error:
                return command, OperationResult(
                    "failed", command.run_id, command.task_id, command.stage_id,
                    command.command_id, detail=str(error), error_code="result_contract_invalid")
        return command, OperationResult(
            "failed", command.run_id, command.task_id, command.stage_id,
            command.command_id, error_code="execution_" + attempt["state"])

    def _flowthrough_record(self, app, command, outcome, *, canonical=True):
        result = outcome.to_dict()
        from .diagnostic_repair_routing import record_result
        record_result(app, command, outcome)
        app["effective"][command.task_id] = result
        if canonical:
            app["effective"][command.stage_id] = result
        if command.command_id not in app["processed"]:
            app["processed"].append(command.command_id)
        app["history"].append({
            "stage": command.stage_id, "task_id": command.task_id,
            "execution_id": command.command_id, "state": outcome.status,
            "error_code": outcome.error_code, "verdict": outcome.outputs.get("verdict"),
        })
        if (outcome.status != "completed" or outcome.error_code
                or outcome.outputs.get("diagnostic_code")
                or outcome.outputs.get("business_diagnostics")
                or outcome.outputs.get("diagnostics")
                or (command.stage_id in REVIEW_STAGES
                    and outcome.outputs.get("verdict") != "approved")):
            self._flowthrough_diagnostic(app, result)
        return result

    @staticmethod
    def _flowthrough_operational_failure(attempt, outcome):
        """Keep executor and sandbox availability as operational controls."""
        if attempt["state"] != "succeeded":
            return "execution_" + attempt["state"]
        if outcome.error_code in FLOWTHROUGH_OPERATIONAL_ERRORS:
            return outcome.error_code
        return None

    def _artifact_required_failure(self, header, app, reason):
        archive_required_behavior_failure(header, app, reason)
        app['execution_status'] = 'finished'
        return self._finish(app, reason)

    def _artifact_schedule(self, snapshot, header, app, stage, **kwargs):
        operations = self._schedule(snapshot, header, app, stage, **kwargs)
        finish = next((op for op in operations if op['kind'] == 'finish'), None)
        if finish is not None:
            reason = app.get('terminal_reason') or 'artifact_required_behavior_dispatch_failed'
            return self._artifact_required_failure(header, app, reason)
        if not any(op['kind'] in {'add_task', 'new_attempt', 'dispatch'} for op in operations):
            deadline = self._effective_deadline(header, app)
            reason = ('wall_clock_budget_exhausted'
                      if deadline is not None and self.clock() >= deadline else
                      'artifact_required_behavior_dispatch_unavailable')
            return self._artifact_required_failure(header, app, reason)
        return operations

    def _artifact_required_repair(self, snapshot, header, app, scope, assessment, execution_id,
                                  *, upstream_overrides=None):
        if source_reading_policy(header) and scope == 'source':
            return self._artifact_required_failure(header, app, 'source_harness_disabled_by_policy')
        author = 'contract_draft' if scope == 'source' else 'artifact_test_design'
        terminated = app.get('progress_supervision', {}).get('terminated_executions', {})
        if any(row.get('task_id') == author or row.get('scope') == scope
               for row in terminated.values()):
            return self._artifact_required_failure(header, app,
                'progress_supervisor_terminated_' + scope)
        family = 'review_rework:' + author
        state = app.setdefault('artifact_required_repairs', {})
        state[scope] = {'assessment': json_copy(assessment), 'boundary_execution_id': execution_id}
        budget = header['request']['budget']
        deadline = self._effective_deadline(header, app)
        if deadline is not None and self.clock() >= deadline:
            return self._artifact_required_failure(header, app, 'wall_clock_budget_exhausted')
        exhausted = (not progress_supervised(header)
                     and app['rounds'].get(family, 0) >= budget['max_rework_rounds'])
        limit = budget['max_agent_assignments']
        if exhausted or limit is not None and app['agent_assignments'] >= limit:
            reason = ('artifact_' + scope + '_repair_rounds_exhausted' if exhausted
                      else 'agent_assignment_budget_exhausted')
            app['artifact_required_failure'] = reason
            return self._artifact_schedule(snapshot, header, app, 'artifact_test_report',
                dependencies=[], causation_id=execution_id)
        # This is the narrowly authorized host repair route. Preserve every
        # prior attempt in SDK history and pass the actual failed observations
        # to the existing source/target harness author under the same budget.
        upstream = json_copy({**app['effective'], **(upstream_overrides or {})})
        previous = upstream.get(author, {})
        request = {'request_id': family + '.' + str(app['rounds'].get(family, 0) + 1),
                   'source_execution_id': previous.get('command_id'),
                   'reviewer_execution_id': execution_id,
                   'instructions': (
                       'Repair the missing or failed required ' + scope + ' behavior harness. '
                       'Inspect the supplied actual verifier case, assertion and evidence results '
                       'and raw failure logs. Implement missing version-specific runtime adapters '
                       'and repair fixtures rather than marking required cases optional or skipped. '
                       'Preserve behavior/test/assertion IDs and all existing assertions. '
                       'Do not edit product code, remove required coverage or invent source defects. '
                       'The host reruns fresh verification after your assignment. Required gaps: '
                       + json.dumps(assessment['gaps'], ensure_ascii=False))}
        tail = ({'contract_verify', 'contract_review', 'contract_freeze',
                 'artifact_test_design', 'artifact_test_execute', 'artifact_test_report'}
                if scope == 'source' else {'target_contract_freeze', 'artifact_test_execute', 'artifact_test_report'})
        for key, value in list(app['effective'].items()):
            if value.get('stage_id') in tail:
                del app['effective'][key]
        if scope == 'source':
            for key in ('contract_lock_sha256', 'contract_sha256'):
                app['locked_artifacts'].pop(key, None)
        dispatched = self._artifact_schedule(snapshot, header, app, author,
            dependencies=[], causation_id=execution_id,
            payload={'reviewer_rework': request,
                     'required_behavior_repair': {'scope': scope, 'assessment': assessment}},
            upstream_overrides=upstream)
        if any(op['kind'] in {'add_task', 'new_attempt'} for op in dispatched):
            self._charge_rework(header, app, family)
        return dispatched

    def _artifact_required_successor(self, snapshot, header, app, stage, execution_id):
        outcome = app['effective'].get(stage, {})
        if outcome.get('error_code') in {'artifact_candidate_changed', 'artifact_binary_shadowed'}:
            # Retrying adaptation would snapshot the author's already-modified
            # product as its new baseline. Archive the violation instead.
            app['artifact_required_failure'] = outcome['error_code']
            return self._artifact_schedule(snapshot, header, app, 'artifact_test_report',
                dependencies=[], causation_id=execution_id)
        if stage in {'contract_review', 'contract_freeze'} and not source_reading_policy(header):
            assessment = assess_source_selection(header['run_dir'], app['effective'])
            app.setdefault('required_behavior_assessments', {})['source'] = assessment
            if assessment['status'] != 'passed':
                return self._artifact_required_repair(
                    snapshot, header, app, 'source', assessment, execution_id)
        if source_reading_policy(header) and stage in {'behavior_extract', 'behavior_freeze'}:
            if outcome.get('status') != 'completed':
                return self._artifact_required_failure(header, app,
                    outcome.get('error_code') or 'source_behavior_requirements_unavailable')
        if source_reading_policy(header) and stage == 'target_contract_freeze':
            if outcome.get('status') != 'completed':
                assessment = {'status': 'failed', 'gaps': [outcome.get('detail')
                    or 'target executable declarations do not cover the behavior requirements']}
                return self._artifact_required_repair(snapshot, header, app,
                    'target', assessment, execution_id)
        if stage == 'artifact_test_execute':
            assessment = assess_target_selection(header['run_dir'], app['effective'])
            app.setdefault('required_behavior_assessments', {})['target'] = assessment
            if assessment['status'] != 'passed':
                return self._artifact_required_repair(
                    snapshot, header, app, 'target', assessment, execution_id)
        if stage == 'artifact_test_report':
            return self._flowthrough_finish(app, header=header)
        next_stage = stage_routes(header)[2].get(stage)
        if next_stage is None:
            return self._artifact_required_failure(header, app, 'artifact_verification_route_incomplete')
        return self._artifact_schedule(snapshot, header, app, next_stage,
            dependencies=[], causation_id=execution_id)

    def _flowthrough_finish(self, app, *, header=None, reason="workflow_execution_finished"):
        if header is not None and self._final_cleanup_enabled(header):
            if app.get('final_cleanup', {}).get('phase') != 'complete':
                return self._finish(app, 'final_cleanup_revalidation_incomplete')
            assessment = self._final_cleanup_acceptance(header, app)
            app['final_cleanup']['assessment'] = assessment
            if assessment['status'] != 'passed':
                return self._finish(app, 'required_target_acceptance_incomplete')
            delivery = app['effective'].get('delivery', {})
            jars = delivery.get('outputs', {}).get('jars', [])
            package_root = project_path(Path(header['run_dir']).resolve(), 'worktree') / 'build' / 'libs'
            packaged = False
            for row in jars if isinstance(jars, list) else []:
                if not isinstance(row, Mapping) or not isinstance(row.get('path'), str):
                    continue
                relative = Path(row['path'])
                path = Path(header['run_dir']).resolve() / relative
                try:
                    packaged = (not relative.is_absolute() and '..' not in relative.parts
                        and not path.is_symlink() and path.is_file() and path.suffix == '.jar'
                        and path.resolve() == path.absolute() and path.resolve().is_relative_to(package_root)
                        and path.stat().st_size > 0)
                except OSError:
                    packaged = False
                if packaged:
                    break
            if delivery.get('status') != 'completed' or not packaged:
                return self._finish(app, 'final_delivery_package_missing')
        if header is not None and required_behavior_policy(header):
            assessments = required_behavior_assessments(header, header['run_dir'], app['effective'])
            app['required_behavior_assessments'] = assessments
            failure = app.get('artifact_required_failure')
            report = app['effective'].get('artifact_test_report', {})
            ref = report.get('outputs', {}).get('artifact_refs', {}).get('artifact_verification_report')
            try:
                if not isinstance(ref, Mapping):
                    raise ValueError('artifact verification report is missing')
                verified_path(Path(header['run_dir']), ref)
            except (OSError, ValueError, TypeError):
                failure = failure or 'artifact_verification_report_unavailable'
            if failure or any(row['status'] != 'passed' for row in assessments.values()):
                return self._artifact_required_failure(header, app,
                    failure or 'artifact_required_behavior_incomplete')
            if report.get('status') != 'completed':
                return self._artifact_required_failure(header, app, 'artifact_verification_report_failed')
            app['required_behavior_status'] = 'passed'
            reason = 'artifact_required_behaviors_passed'
        app["acceptance_status"] = "unverified"
        app["diagnostic_count"] = len(app.get("business_diagnostics", []))
        app["execution_status"] = "finished"
        return self._finish(app, reason, "succeeded")

    def _flowthrough_stage_effects(self, header, app, command, outcome):
        """Project usable output without interpreting it as acceptance."""
        stage = command.stage_id
        outputs = outcome.outputs
        locked = outputs.get("locked_artifacts", {})
        if isinstance(locked, dict):
            for key, value in locked.items():
                app["locked_artifacts"][key] = value
        if stage == "skill_lookup":
            try:
                initialize_research(app, outputs)
            except (KeyError, TypeError, ValueError) as error:
                self._flowthrough_diagnostic(app, outcome.to_dict(), note=str(error))
        if stage in {"skill_publish", "knowledge_publish"}:
            revisions = outputs.get("knowledge_revisions")
            if isinstance(revisions, dict):
                app["knowledge_revisions"].update(revisions)
        if stage == "mod_analysis":
            try:
                self._ingest_analysis(app, outputs)
            except (KeyError, TypeError, ValueError) as error:
                self._flowthrough_diagnostic(app, outcome.to_dict(), note=str(error))
        try:
            self._ingest_deferred_obligations(app, outcome)
        except (KeyError, TypeError, ValueError) as error:
            self._flowthrough_diagnostic(app, outcome.to_dict(), note=str(error))
        if (stage in {"migration_inventory", "migration_plan", "contract_diagnose",
                      "contract_repair_plan", "target_diagnose", "target_repair_plan"}
                and not (header.get("definition", {}).get("workflow_version", 0) >= 19
                         and stage in {"migration_inventory", "migration_plan"})):
            _, error = self._record_fixed_markdown_revision(
                header, app, stage, outcome, command.command_id)
            if error:
                self._flowthrough_diagnostic(app, outcome.to_dict(), note=error)

    @staticmethod
    def _final_cleanup_enabled(header):
        definition = header.get('definition', {})
        return (definition.get('workflow_version', 0) >= 37
                and header.get('request', {}).get('workflow_mode', 'migration') == 'migration'
                and definition.get('final_cleanup_policy', {}).get('enabled') is True)

    @staticmethod
    def _final_cleanup_acceptance(header, app):
        from .artifact_verification_policy import read_locked_contract, assess_required_cases
        effective = app['effective']
        try:
            lock = read_locked_contract(header['run_dir'], effective, workflow_version=37)
            runtime = effective.get('acceptance_build', {})
            assessment = assess_required_cases(lock['contract'], runtime)
        except (OSError, ValueError, TypeError, KeyError) as exc:
            assessment = {'status': 'failed', 'gaps': [str(exc)]}
        gaps = list(assessment['gaps'])
        build = effective.get('target_build', {})
        outputs = build.get('outputs', {})
        if (build.get('status') != 'completed' or outputs.get('build_status') != 'completed'
                or outputs.get('build_executed') is not True):
            gaps.append('target compile/package build did not execute successfully')
        tasks = outputs.get('build_gradle_tasks')
        if isinstance(tasks, list) and 'build' not in {task.split(':')[-1] for task in tasks}:
            gaps.append('target package build task did not execute')
        review = effective.get('code_review', {})
        review_finished = (review.get('status') == 'completed'
            or (review.get('status') == 'failed' and review.get('error_code') == 'code_review_rejected'
                and review.get('outputs', {}).get('verdict') == 'rejected'))
        if not review_finished:
            gaps.append('independent code review did not finish')
        runtime = effective.get('acceptance_build', {})
        if runtime.get('status') != 'completed':
            gaps.append('frozen target verification did not complete')
        return {**assessment, 'gaps': gaps, 'status': 'failed' if gaps else 'passed'}

    def _final_cleanup_successor(self, snapshot, header, app, stage, execution_id):
        if not self._final_cleanup_enabled(header):
            return None
        checkpoint = app.setdefault('final_cleanup', {})
        phase = checkpoint.get('phase')
        if stage == 'final_cleanup':
            outcome = app['effective'].get(stage, {})
            if outcome.get('status') != 'completed':
                checkpoint['phase'] = 'failed'
                return self._finish(app, outcome.get('error_code') or 'final_cleanup_failed')
            checkpoint.update(phase='revalidating', execution_id=outcome.get('command_id'))
            # Preserve the target contract and historical evidence; discard every
            # active result that could otherwise certify the pre-cleanup product.
            self._invalidate_from(app, 'target_build')
            for key, result in list(app['effective'].items()):
                if result.get('stage_id') in {'code_review', 'test_design', 'test_review',
                        'test_execute', 'acceptance_preflight', 'acceptance_build',
                        'client_smoke', 'gap_review', 'delivery'}:
                    app['effective'].pop(key)
            app['rework_context'] = None
            return self._schedule(snapshot, header, app, 'target_build',
                dependencies=[], causation_id=execution_id)
        if stage == 'code_review' and phase in {'revalidating', 'complete'}:
            return self._schedule(snapshot, header, app, 'acceptance_preflight',
                dependencies=[], causation_id=execution_id)
        if stage == 'gap_review':
            assessment = self._final_cleanup_acceptance(header, app)
            checkpoint['assessment'] = assessment
            if assessment['status'] != 'passed':
                return self._finish(app, 'required_target_acceptance_incomplete')
            if phase in {'revalidating', 'complete'}:
                checkpoint['phase'] = 'complete'
                return self._schedule(snapshot, header, app, 'delivery',
                    dependencies=[], causation_id=execution_id)
            if phase == 'failed':
                return self._finish(app, 'final_cleanup_failed')
            checkpoint.update(phase='scheduled', initial_acceptance_execution_id=execution_id)
            return self._schedule(snapshot, header, app, 'final_cleanup',
                dependencies=[], causation_id=execution_id)
        if stage == 'delivery' and phase != 'complete':
            return self._finish(app, 'final_cleanup_revalidation_incomplete')
        return None

    def _integration_boundary(self, snapshot, header, app, stage, execution_id, outcome=None):
        """An unfinished merge needs an actual repair before its consumers run."""
        from .integration_repair import INTEGRATION_STAGES, start_or_resume
        if stage not in INTEGRATION_STAGES:
            return None
        result = outcome.to_dict() if isinstance(outcome, OperationResult) else outcome
        if result is None:
            result = app.get('effective', {}).get(stage)
        if not isinstance(result, dict) or result.get('status') == 'completed':
            return None
        self._flowthrough_diagnostic(app, result, note='unfinished integration boundary')
        if app.get('stop_reason'):
            return self._finish(app, app['stop_reason'], app.get('stop_state') or 'failed')
        if result.get('error_code') == 'integration_merge_required':
            operations = start_or_resume(self, snapshot, header, app, outcome=result)
            if operations is not None:
                return operations
        # Raw patch, workspace, identity and transport errors are not evidence
        # of a semantic conflict and never authorize another coder attempt.
        return self._finish(app, result.get('error_code') or 'integration_failed')

    def _flowthrough_schedule_successor(self, snapshot, header, app, stage, execution_id):
        integration = self._integration_boundary(snapshot, header, app, stage, execution_id)
        if integration is not None:
            return integration
        final = self._final_cleanup_successor(snapshot, header, app, stage, execution_id)
        if final is not None:
            return final
        if required_behavior_policy(header):
            return self._artifact_required_successor(snapshot, header, app, stage, execution_id)
        if source_reading_policy(header) and stage in {'behavior_freeze', 'target_contract_freeze'}:
            outcome = app.get('effective', {}).get(stage, {})
            if outcome.get('status') != 'completed':
                return self._finish(app, outcome.get('error_code') or 'required_machine_input_missing')
        if (header.get('definition', {}).get('workflow_version', 0) >= 20
                and header.get('request', {}).get('workflow_mode', 'migration') == 'migration'
                and stage in stage_routes(header)[1]):
            # Administrator wakeups / resumed boundaries must re-enter the
            # frozen DAG, not reinterpret its display order as dependencies.
            app['early_active'] = True
            app['active_stage'] = None
            return self._flowthrough_early_decision(snapshot, header, app)
        if stage == "delivery":
            reason = "workflow_execution_finished"
            delivery = app.get("effective", {}).get("delivery", {})
            outputs = delivery.get("outputs", {}) if isinstance(delivery, Mapping) else {}
            required = outputs.get("required_checks", {}) if isinstance(outputs, Mapping) else {}
            if compile_package_scope(header):
                required_names = ("target_compile", "target_package")
                if header.get("definition", {}).get("workflow_version", 0) < 28:
                    required_names = ("source_baseline_behavior_tests", *required_names)
                if (not isinstance(required, Mapping)
                        or any(name not in required
                               or not isinstance(required.get(name), Mapping)
                               or required[name].get("status") != "passed"
                               for name in required_names)):
                    reason = "delivery_compile_package_requirements_not_met"
                else:
                    reason = "delivery_completed_acceptance_unverified"
            return self._flowthrough_finish(app, header=header, reason=reason)
        if (stage == "skill_publish"
                and header["request"].get("workflow_mode") == "skill_generation"):
            return self._flowthrough_finish(app, header=header, reason="skill_workflow_execution_finished")
        next_stage = stage_routes(header)[2].get(stage)
        if next_stage is None:
            return self._flowthrough_finish(app, header=header)
        if header.get("request", {}).get("workflow_mode") == "artifact_verification":
            prior = app.get("effective", {}).get(stage, {})
            dependency = prior.get("task_id") or stage
            return self._schedule(snapshot, header, app, next_stage,
                                  dependencies=[dependency], causation_id=execution_id)
        return self._schedule(snapshot, header, app, next_stage,
                              dependencies=[], causation_id=execution_id)

    def _flowthrough_early_decision(self, snapshot, header, app):
        _, early_stages, next_stages, dependencies_by_stage = stage_routes(header)
        pending = app.setdefault("early_pending", [])
        operations = []
        for task_id in list(pending):
            task = snapshot["tasks"].get(task_id)
            if task is None:
                continue
            attempt = task["attempts"][-1]
            if attempt["state"] not in EXECUTION_TERMINAL:
                continue
            pending.remove(task_id)
            command, outcome = self._flowthrough_outcome(attempt)
            if command.command_id in app["processed"]:
                continue
            self._flowthrough_record(app, command, outcome)
            self._flowthrough_stage_effects(header, app, command, outcome)
            integration = self._integration_boundary(
                snapshot, header, app, command.stage_id, command.command_id, outcome)
            if integration is not None:
                return operations + integration
            if (source_reading_policy(header) and command.stage_id == 'behavior_freeze'
                    and outcome.status != 'completed'):
                return operations + self._finish(app, outcome.error_code or 'behavior_requirements_missing')
            operational = self._flowthrough_operational_failure(attempt, outcome)
            if operational:
                return operations + self._finish(app, operational)
            if command.stage_id not in early_stages:
                if (command.stage_id in {"contract_revise", "development_prepare"}
                        and isinstance(outcome.outputs.get("development_tasks"), list)
                        and outcome.outputs.get("development_tasks")):
                    self._start_development_group(
                        header, app, command.stage_id, outcome.outputs)
                    return operations + self._flowthrough_group_decision(
                        snapshot, header, app)
                successor = next_stages.get(command.stage_id)
                if successor is not None:
                    if successor in set(early_stages) | set(REWORK_STAGES):
                        operations += self._early_schedule(snapshot, header, app, successor,
                            dependencies=[], causation_id=command.command_id)
                    else:
                        app["early_active"] = False
                        operations += self._schedule(snapshot, header, app, successor,
                            dependencies=[], causation_id=command.command_id)
                    continue

        def observed(stage):
            return stage in app["effective"]

        for stage in early_stages:
            if stage in pending or observed(stage):
                continue
            if stage == "contract_draft" and header.get("inherit_harness"):
                continue
            dependencies = dependencies_by_stage[stage]
            if stage == "skill_publish":
                # Skill generation and review are useful diagnostics, never
                # prerequisites for continuing the migration in v17.
                dependencies = ("skill_lookup",)
            if stage == "contract_verify" and header.get("inherit_harness"):
                dependencies = ("contract_restore",)
                if not observed("contract_restore") and "contract_restore" not in pending:
                    if all(observed(dep) for dep in DEPENDENCIES["contract_restore"]):
                        operations += self._early_schedule(
                            snapshot, header, app, "contract_restore", dependencies=[])
                if not observed("contract_restore"):
                    continue
            if all(observed(dep) for dep in dependencies):
                operations += self._early_schedule(
                    snapshot, header, app, stage, dependencies=[])

        if all(observed(dep) for dep in dependencies_by_stage["migration_inventory"]):
            app["gap_pending"] = list(pending)
            app["early_pending"] = []
            app["early_active"] = False
            if header.get('definition', {}).get('workflow_version', 0) >= 20:
                if not observed('migration_inventory') and 'migration_inventory' in snapshot['tasks']:
                    app['active_stage'] = 'migration_inventory'
                    return operations
                if observed('migration_inventory'):
                    # Adopt existing main work rather than replaying authors
                    # after a restored early boundary or administrator wakeup.
                    main = stage_routes(header)[0]
                    tail = main[main.index('migration_inventory'):]
                    latest = [item for item in tail if observed(item) or item in snapshot['tasks']][-1]
                    if not observed(latest):
                        app['active_stage'] = latest
                        return operations
                    return operations + self._flowthrough_schedule_successor(
                        snapshot, header, app, latest, app['effective'][latest].get('command_id'))
            app["planning_generation"] += 1
            app.setdefault("plan_documents", {}).pop("migration", None)
            operations += self._schedule(
                snapshot, header, app, "migration_inventory", dependencies=[])
        return operations

    def _flowthrough_group_decision(self, snapshot, header, app):
        from . import coder_revival
        group = app["active_group"]
        revival_enabled = coder_revival.enabled(header) and group['kind'] == 'development'
        if group["kind"] == "regression":
            from .regression import regression_decision
            return regression_decision(self, snapshot, header, app)
        if group["kind"] == "skills":
            if (header.get("definition", {}).get("workflow_version", 0) < 25
                    or header["request"].get("workflow_mode") != "skill_generation"):
                # Preserve the frozen v17-v24 diagnostic continuation route.
                active = []
                for task_id in group.get("members", []):
                    task = snapshot["tasks"].get(task_id)
                    if task and task["attempts"][-1]["state"] not in EXECUTION_TERMINAL:
                        active.append(task_id)
                if active:
                    return []
                app["active_group"] = None
                return self._schedule(snapshot, header, app, "skill_publish", dependencies=[])
            operations = []
            for task_id in list(group.get("members", [])):
                task = snapshot["tasks"].get(task_id)
                if task is None or task["attempts"][-1]["state"] not in EXECUTION_TERMINAL:
                    continue
                attempt = task["attempts"][-1]
                command, outcome = self._flowthrough_outcome(attempt)
                if command.command_id in app["processed"]:
                    continue
                group["results"][task_id] = self._flowthrough_record(app, command, outcome)
                self._flowthrough_stage_effects(header, app, command, outcome)
                operational = self._flowthrough_operational_failure(attempt, outcome)
                if operational:
                    return self._finish(app, operational)
            for kind in group["missing_kinds"]:
                generated, review = kind + "_diff", kind + "_skill_review"
                if generated in group["results"] and review not in group["members"]:
                    group["members"].append(review)
                    operations += self._schedule(snapshot, header, app, review,
                        activate=False, dependencies=[generated])
            reviews = [kind + "_skill_review" for kind in group["kinds"]]
            if all(review in group["results"] for review in reviews):
                app["active_group"] = None
                return operations + self._schedule(snapshot, header, app,
                    "skill_publish", dependencies=reviews)
            return operations

        # Bind every new command (including continued groups) to the actual
        # scheduled DAG. A historical review ref may describe an older DAG.
        group.setdefault('execution_payload', {})['execution_development_plan'] = {
            'schema_version': 1, 'base_commit': group['base'],
            'shared_paths': [], 'tasks': group['tasks'],
        }
        integrity_errors = {
            'command_artifact_invalid', 'candidate_identity_mismatch',
            'result_contract_invalid', 'build_sandbox_unavailable',
            'baseline_write_scope_invalid', 'review_workspace_invalid',
            'coder_isolation_violation', 'locked_artifact_invalid',
            'acceptance_rubric_invalid', 'source_history_invalid',
            'contract_source_mismatch', 'planning_artifact_invalid',
            'agent_rules_invalid', 'opencode_cleanup_unconfirmed'}
        if revival_enabled:
            # A supported continuation may carry settled results without new
            # SDK attempts in this segment. Failed carried results still need
            # a persisted planner decision before the unfinished coder resumes.
            for task in group['tasks']:
                result = group['results'].get(f"coder.g{group['generation']}.{task['id']}")
                if result is not None:
                    if result.get('error_code') in integrity_errors:
                        return self._finish(app, result['error_code'])
                    coder_revival.record_result(group, task['id'], result)
        operations = []
        active = []
        for task_id in group["members"]:
            task = snapshot["tasks"].get(task_id)
            if task is None or task["attempts"][-1]["state"] not in EXECUTION_TERMINAL:
                active.append(task_id)
                continue
            attempt = task["attempts"][-1]
            execution_id = attempt["command"]["execution_id"]
            if execution_id in app["processed"]:
                continue
            command, outcome = self._flowthrough_outcome(attempt)
            result = self._flowthrough_record(app, command, outcome, canonical=False)
            group["results"][task_id] = result
            operational = self._flowthrough_operational_failure(attempt, outcome)
            if revival_enabled and command.stage_id == 'coder':
                name = command.payload['development_task']['id']
                coder_revival.record_result(group, name, result)
                # Settled coder failures become planner requests. Evidence and
                # sandbox integrity failures still stop execution immediately.
                operational = outcome.error_code if outcome.error_code in integrity_errors else None
            if (command.stage_id == "coder" and operational == "budget_exhausted"
                    and outcome.outputs.get("artifact_refs", {}).get("coder_patch")
                    and isinstance(header.get("deadline_epoch"), (int, float))
                    and header["deadline_epoch"] > self.clock()):
                # A coder's local cap is shorter than the Run cap. Its stopped
                # partial patch is already authenticated; keep the diagnostic
                # and let the remaining DAG proceed.
                operational = None
            if operational:
                return operations + self._finish(app, operational)

        tasks = {task["id"]: task for task in group["tasks"]}
        generation = group["generation"]
        coder_id = lambda name: f"coder.g{generation}.{name}"
        completed = {name for name in tasks if coder_id(name) in group["results"]}
        if revival_enabled:
            operations += coder_revival.advance(self, snapshot, header, app)
            completed = coder_revival.completed(group)
            if app.get('stop_reason'):
                return operations
            planner_error = group['revival'].get('terminal_error')
            planner_blocked = set(group['revival'].get('terminal_blocked', ()))
            if coder_revival.stopped_group(snapshot, group):
                app['failed_development_group'] = json_copy(group)
                return operations + self._finish(app, 'coder_revival_stopped')
        else:
            planner_error, planner_blocked = None, set()
        if len(completed) == len(tasks) and not planner_error:
            if revival_enabled:
                app.setdefault('coder_revival_history', []).append(json_copy(group.get('revival', {})))
            results = [group["results"][coder_id(task["id"])] for task in group["tasks"]]
            app["active_group"] = None
            scope = group.get("goal_scope", "migration")
            stage = "development_integrate" if scope == "migration" else scope + "_repair_integrate"
            if group.get("execution_payload", {}).get("development_kind") == "preparation":
                stage = "development_prepare_integrate"
            return operations + self._schedule(
                snapshot, header, app, stage, dependencies=[],
                artifact_overrides=group.get("artifact_refs"),
                payload={**group.get("execution_payload", {}), "goal_scope": scope,
                    "development_results": results, "development_generation": generation,
                    "development_base": group["base"]})

        capacity = self._memory_capacity(
            snapshot, header, app, "development", requested_stage="goal_prepare")
        goal_dispatches = 0
        for task in group["tasks"]:
            if capacity <= 0:
                break
            name = task["id"]
            if name in planner_blocked or name in group.setdefault("goal_scheduled", []):
                continue
            goal_id = f"goal.g{generation}.{name}"
            dispatched = self._schedule(
                snapshot, header, app, "goal_prepare", task_id=goal_id,
                activate=False, dependencies=[], artifact_overrides=group.get("artifact_refs"),
                payload={"development_task": task,
                    "planning_context": group.get("planning_context", {}),
                    "goal_scope": group.get("goal_scope", "migration"),
                    "goal_generation": generation})
            operations += dispatched
            if any(op["kind"] == "dispatch" for op in dispatched):
                group["goal_scheduled"].append(name)
                group["members"].append(goal_id)
                capacity -= 1
                goal_dispatches += 1

        goal_policy = self.memory_policy.for_stage("goal_prepare")
        capacity = self._memory_capacity(
            snapshot, header, app, "development", requested_stage="coder",
            additional_active=goal_dispatches,
            additional_reservation_bytes=(0 if goal_policy is None else
                                          goal_dispatches * goal_policy.heavy_slot_bytes))
        for task in group["tasks"]:
            if capacity <= 0:
                break
            name = task["id"]
            if name in planner_blocked:
                continue
            dependencies_ready = set(task.get("dependencies", ())) <= completed
            if revival_enabled:
                dependencies_ready = coder_revival.dependencies_ready(group, name, completed)
            if (name in group["scheduled"] or not dependencies_ready
                    or revival_enabled and not coder_revival.allowed(group, name)):
                continue
            from .development import development_workspace
            goal_id = f"goal.g{generation}.{name}"
            goal_result = group["results"].get(goal_id)
            if goal_result is None:
                continue
            goal_ref = goal_result.get("outputs", {}).get("artifact_refs", {}).get("coder_goal")
            ancestors = set()
            def visit(dependency):
                if dependency in ancestors or dependency not in tasks:
                    return
                ancestors.add(dependency)
                for parent in tasks[dependency].get("dependencies", ()):
                    visit(parent)
            for dependency in task.get("dependencies", ()):
                visit(dependency)
            ordered = [item["id"] for item in group["tasks"] if item["id"] in ancestors]
            patches = [group["results"].get(coder_id(dep), {}).get("outputs", {}).get(
                       "artifact_refs", {}).get("coder_patch") for dep in ordered]
            refs = dict(group.get("artifact_refs", {}))
            if isinstance(goal_ref, dict):
                refs["coder_goal"] = goal_ref
            revival_payload = (coder_revival.coder_payload(snapshot, header, group, name)
                               if revival_enabled else {})
            execution_payload = {**group.get('execution_payload', {}), **revival_payload}
            if revival_payload:
                refs['coder_revival_request'] = revival_payload['coder_revival']['request_ref']
            model, effort = agent_model_policy(header.get("definition", {}).get("workflow_version"), model_policy=header.get("model_policy"))
            dispatched = self._schedule(
                snapshot, header, app, "coder", task_id=coder_id(name), activate=False,
                causation_id=revival_payload.get('coder_revival', {}).get('planner_execution_id'),
                dependencies=[], artifact_overrides=refs,
                payload={**execution_payload,
                    "goal_scope": group.get("goal_scope", "migration"),
                    "goal_generation": generation,
                    "planning_context": group.get("planning_context", {}),
                    "development_task": task, "development_base": group["base"],
                    "development_generation": generation, "dependency_patches": patches,
                    "recovered_partial_patch": (revival_payload['recovered_partial_patch'] if revival_payload
                        else group.get('execution_payload', {}).get('recovered_partial_patches', {}).get(name)),
                    "recovered_rework_request": group.get('execution_payload', {}).get(
                        'recovered_rework_requests', {}).get(name)},
                extra_options={"workspace": development_workspace(
                    generation, name, execution_payload),
                    "model": task.get("model", model),
                    "reasoning_effort": task.get("reasoning_effort", effort)})
            operations += dispatched
            if any(op["kind"] == "dispatch" for op in dispatched):
                group["scheduled"].append(name)
                if coder_id(name) not in group['members']:
                    group["members"].append(coder_id(name))
                if revival_enabled:
                    coder_revival.dispatched(group, name)
                capacity -= 1
        if planner_error and not active and not operations:
            app['failed_development_group'] = json_copy(group)
            return self._finish(app, planner_error)
        return operations

    def _advance_without_business_gates(self, snapshot, header, app):
        from .integration_repair import INTEGRATION_STAGES, start_or_resume
        if app.get('stop_reason'):
            # The outer driver owns exact cancellation and cleanup settlement.
            return []
        replay = app.get('integration_replay')
        if isinstance(replay, dict):
            original = OperationInput.from_dict(read_json(
                verified_path(Path(header['run_dir']), replay['command_ref'])))
            if (original.stage_id not in INTEGRATION_STAGES
                    or original.stage_id != replay.get('stage')
                    or original.command_id != replay.get('source_execution_id')):
                raise ValueError('integration replay differs from its selected execution')
            payload = json_copy(original.payload)
            carried = payload.setdefault('carried_development_results', {})
            for result in payload.get('development_results', []):
                if isinstance(result, dict) and isinstance(result.get('command_id'), str):
                    carried.setdefault(result['command_id'], {'source_execution_id': original.command_id})
            host_options = {'workflow_version', 'deadline_epoch', 'agent_assignment', 'rework_round',
                            'model', 'reasoning_effort', 'model_policy', 'gate_policy',
                            'business_gates_disabled', 'progress_supervision_policy',
                            'validation_policy', 'agent_dialogue_policy', 'acceptance_rubric_sha256'}
            replay_operations = self._schedule(
                snapshot, header, app, original.stage_id, task_id=original.task_id,
                dependencies=[], causation_id=original.command_id,
                payload=payload, artifact_overrides=original.artifact_refs,
                upstream_overrides=original.upstream_results,
                prior_findings_override=original.prior_findings,
                extra_options={key: value for key, value in original.options.items()
                               if key not in host_options})
            if any(op['kind'] in {'add_task', 'new_attempt'} for op in replay_operations):
                app.pop('integration_replay', None)
                app.pop('flowthrough_resume', None)
            return replay_operations
        repair = start_or_resume(self, snapshot, header, app)
        if repair is not None:
            resume = app.get('flowthrough_resume')
            if isinstance(resume, dict) and resume.get('stage') in INTEGRATION_STAGES:
                app.pop('flowthrough_resume', None)
            return repair
        current_skill_generation = (
            header.get("definition", {}).get("workflow_version", 0) >= 25
            and header["request"].get("workflow_mode") == "skill_generation"
        )
        main_stages, early_stages, next_stages, dependencies_by_stage = stage_routes(header)
        app.setdefault("acceptance_status", "unverified")
        operations = []
        if app.get("flowthrough_finish_pending"):
            active = False
            for task in snapshot["tasks"].values():
                attempt = task["attempts"][-1]
                if attempt["state"] not in EXECUTION_TERMINAL:
                    active = True
                    continue
                execution_id = attempt["command"]["execution_id"]
                if execution_id in app["processed"]:
                    continue
                command, outcome = self._flowthrough_outcome(attempt)
                self._flowthrough_record(app, command, outcome, canonical=False)
                operational = self._flowthrough_operational_failure(attempt, outcome)
                if operational:
                    app.pop("flowthrough_finish_pending", None)
                    if required_behavior_policy(header):
                        return self._artifact_required_failure(header, app, operational)
                    return self._finish(app, operational)
            if active:
                return operations
            app.pop("flowthrough_finish_pending", None)
            return self._flowthrough_finish(app, header=header)
        resume = app.pop("flowthrough_resume", None)
        if isinstance(resume, dict):
            stage = resume.get("stage")
            location = resume.get("location")
            result = app.get("effective", {}).get(resume.get("task_id")) or app.get(
                "effective", {}).get(stage)
            if isinstance(result, dict):
                self._flowthrough_diagnostic(app, result, note="continued failed boundary")
            integration = self._integration_boundary(
                snapshot, header, app, stage, resume.get('command_id'), result)
            if integration is not None:
                return integration
            if (required_behavior_policy(header) and source_reading_policy(header)
                    and resume.get("required_target_restart") is True):
                restart = app.get("continuation_feedback", {}).get("target_restart", {})
                next_stage = resume.get("next_stage")
                if next_stage != restart.get("start_stage"):
                    raise ValueError("target continuation restart stage differs from its evidence")
                if next_stage == "artifact_test_design":
                    settlement = restart.get("previous_settlement", {})
                    assessment = settlement.get("required_behavior_assessments", {}).get("target")
                    if not isinstance(assessment, dict):
                        assessment = {"status": "failed", "gaps": [
                            "Explicit continuation requires fresh target harness repair"]}
                    return self._artifact_required_repair(snapshot, header, app, "target",
                        assessment, resume.get("command_id"),
                        upstream_overrides=restart.get("previous_results", {}))
                if next_stage != "target_contract_freeze":
                    raise ValueError("target continuation has unknown restart stage")
                return self._artifact_schedule(snapshot, header, app, next_stage,
                    dependencies=[], causation_id=resume.get("command_id"))
            if required_behavior_policy(header) and isinstance(stage, str):
                # Re-enter required-result routing even if a saved diagnostic
                # continuation recorded the ordinary next stage explicitly.
                return self._flowthrough_schedule_successor(
                    snapshot, header, app, stage, resume.get('command_id'))
            if location == "group" and isinstance(app.get("active_group"), dict):
                # A continued group keeps every settled peer and partial patch.
                # The old failure marker has no routing authority in v17.
                app["active_group"].pop("failure", None)
                return self._flowthrough_group_decision(snapshot, header, app)
            if isinstance(stage, str):
                final = self._final_cleanup_successor(snapshot, header, app, stage, resume.get('command_id'))
                if final is not None:
                    return final
                if (stage == 'code_review' and self._final_cleanup_enabled(header)
                        and app.get('final_cleanup', {}).get('phase') in {'revalidating', 'complete'}):
                    return self._schedule(snapshot, header, app, 'acceptance_preflight',
                        dependencies=[], causation_id=resume.get('command_id'))
                next_stage = resume.get("next_stage")
                if isinstance(next_stage, str):
                    if next_stage not in dependencies_by_stage:
                        raise ValueError("flowthrough continuation has unknown next stage")
                    if location == "early" and next_stage in set(early_stages) | set(REWORK_STAGES):
                        app["early_active"] = True
                        app["active_stage"] = None
                        return self._early_schedule(snapshot, header, app, next_stage,
                            dependencies=[], causation_id=resume.get("command_id"))
                    app["early_active"] = False
                    return self._schedule(snapshot, header, app, next_stage,
                        dependencies=[], causation_id=resume.get("command_id"))
                successor = next_stages.get(stage)
                if (header.get('definition', {}).get('workflow_version', 0) >= 20
                        and header.get('request', {}).get('workflow_mode', 'migration') == 'migration'
                        and stage in early_stages):
                    return self._flowthrough_schedule_successor(
                        snapshot, header, app, stage, resume.get('command_id'))
                if (location == "early" and successor is not None
                        and successor in set(early_stages) | set(REWORK_STAGES)):
                    app["early_active"] = True
                    app["active_stage"] = None
                    return self._early_schedule(snapshot, header, app, successor,
                        dependencies=[], causation_id=resume.get("command_id"))
                return self._flowthrough_schedule_successor(
                    snapshot, header, app, stage, resume.get("command_id"))

        if app.get("active_group"):
            return self._flowthrough_group_decision(snapshot, header, app)
        if (header.get("request", {}).get("workflow_mode") == "artifact_verification"
                and app.get("active_stage") is None and not app.get("effective")):
            if required_behavior_policy(header):
                return self._artifact_schedule(snapshot, header, app, "source", dependencies=[])
            return self._schedule(snapshot, header, app, "source", dependencies=[])
        if app.get("early_active") or (app["active_stage"] is None and not app["effective"]
                and not current_skill_generation):
            app["early_active"] = True
            return self._flowthrough_early_decision(snapshot, header, app)
        stage = app.get("active_stage")
        if stage is None:
            if app.get("flowthrough_finish_pending"):
                return self._flowthrough_finish(app, header=header)
            # A continuation may have preserved the failed boundary without the
            # explicit v17 resume marker. Advance from the latest known stage.
            known = [name for name in main_stages if name in app["effective"]]
            if known:
                latest = known[-1]
                if self._final_cleanup_enabled(header) and app.get('final_cleanup', {}).get('phase') in {'revalidating', 'complete'}:
                    # Display order places final_cleanup after acceptance, but
                    # recovery must follow the actual post-cleanup execution.
                    for row in reversed(app.get('history', [])):
                        candidate = row.get('stage')
                        result = app['effective'].get(candidate, {})
                        if (candidate in known and result.get('command_id') == row.get('execution_id')):
                            latest = candidate
                            break
                result = app["effective"][latest]
                if (latest == 'code_review' and self._final_cleanup_enabled(header)
                        and app.get('final_cleanup', {}).get('phase') in {'revalidating', 'complete'}):
                    return self._schedule(snapshot, header, app, 'acceptance_preflight',
                        dependencies=[], causation_id=result.get('command_id'))
                return self._flowthrough_schedule_successor(
                    snapshot, header, app, latest, result.get("command_id"))
            entry = "skill_lookup" if current_skill_generation else "source"
            return self._schedule(snapshot, header, app, entry, dependencies=[])
        task = snapshot["tasks"].get(stage)
        if task is None:
            return operations
        attempt = task["attempts"][-1]
        if attempt["state"] not in EXECUTION_TERMINAL:
            return operations
        command, outcome = self._flowthrough_outcome(attempt)
        if command.command_id in app["processed"]:
            return operations
        result = self._flowthrough_record(app, command, outcome)
        self._flowthrough_stage_effects(header, app, command, outcome)
        operational = self._flowthrough_operational_failure(attempt, outcome)
        if operational:
            if required_behavior_policy(header):
                return self._artifact_required_failure(header, app, operational)
            return self._finish(app, operational)
        stage = command.stage_id
        if stage == "skill_lookup" and current_skill_generation:
            kinds = outcome.outputs.get("missing_kinds", [])
            reviews = outcome.outputs.get("needs_review_kinds", [])
            if (not isinstance(kinds, list) or not isinstance(reviews, list)
                    or any(kind not in {"platform", "java"} for kind in kinds + reviews)
                    or len(kinds + reviews) != len(set(kinds + reviews))):
                return self._finish(app, "skill_lookup_invalid")
            if kinds or reviews:
                app["active_stage"] = None
                app["active_group"] = {
                    "kind": "skills", "phase": "pipeline", "kinds": kinds + reviews,
                    "missing_kinds": kinds,
                    "members": [kind + "_diff" for kind in kinds]
                               + [kind + "_skill_review" for kind in reviews],
                    "results": {},
                }
                operations = []
                for member in app["active_group"]["members"]:
                    operations += self._schedule(snapshot, header, app, member,
                        activate=False, dependencies=["skill_lookup"])
                return operations
        if (stage == "code_review" and self._final_cleanup_enabled(header)
                and app.get('final_cleanup', {}).get('phase') in {'revalidating', 'complete'}):
            return self._schedule(snapshot, header, app, 'acceptance_preflight',
                dependencies=[], causation_id=command.command_id)
        if stage == "code_review" and not compile_package_scope(header):
            from .regression import start_regression
            return start_regression(self, snapshot, header, app, outcome)
        if (stage in {"implementation", "contract_revise", "target_revise", "development_prepare"}
                and isinstance(outcome.outputs.get("development_tasks"), list)
                and outcome.outputs.get("development_tasks")):
            self._start_development_group(header, app, stage, outcome.outputs)
            return self._flowthrough_group_decision(snapshot, header, app)
        if stage == "development_prepare_integrate":
            app["effective"]["development_prepare"] = result
        return self._flowthrough_schedule_successor(
            snapshot, header, app, stage, command.command_id)

    def _decision(self, snapshot, header, *, sdk=None, stop_reason=None, stop_state="cancelled"):
        try:
            operations, app = self._advance_decision(snapshot, header, sdk=sdk,
                stop_reason=stop_reason, stop_state=stop_state)
        except RepairEvidenceError as error:
            app = json_copy(snapshot.get("application_state") or self._new_application())
            app["repair_evidence_error"] = str(error)
            operations = self._finish(app, "repair_evidence_snapshot_failed")
        from .watchdog_routing import intercept_failure
        operations = intercept_failure(self, snapshot, header, operations, app)
        finish = next((op for op in operations if op["kind"] == "finish"), None)
        if finish is None and not app.get('stop_reason'):
            from .desktop_supervisor import chat_decision
            try:
                operations += chat_decision(self, snapshot, header, app,
                                           business_operations=operations)
            except Exception as error:
                from .desktop_state import note_desktop_error
                note_desktop_error(header, 'chat_observation', error,
                                   revision=snapshot.get('revision'))
            finish = next((op for op in operations if op['kind'] == 'finish'), None)
        if finish is None:
            return operations, app
        active = {task_id: task["attempts"][-1]["command"]["execution_id"]
                  for task_id, task in snapshot["tasks"].items()
                  if task["attempts"][-1]["state"] not in EXECUTION_TERMINAL}
        # A background branch may have produced a new attempt in this same
        # decision. It too must settle before the SDK will accept finish.
        active.update({op["task_id"]: op["command"]["execution_id"] for op in operations
                       if op["kind"] in {"add_task", "new_attempt"}})
        if active:
            for task_id, execution_id in active.items():
                if task_id.startswith('desktop.chat.') and execution_id not in app['cancel_sent']:
                    operations.append({'kind': 'cancel', 'task_id': task_id,
                                       'reason': 'migration_finished'})
                    app['cancel_sent'].append(execution_id)
            if business_gates_disabled(header) and finish["state"] == "succeeded":
                # A completed business route never cancels independent diagnostic
                # work. Wait for already-dispatched tasks to settle, then publish
                # the execution result without converting it to acceptance.
                app["flowthrough_finish_pending"] = True
                app["terminal_reason"] = None
                operations = [op for op in operations if op["kind"] != "finish"]
                return operations, app
            app["stop_reason"] = app["terminal_reason"] or "run_failed"
            app["stop_state"] = finish["state"]
            app["terminal_reason"] = None
            operations = [op for op in operations if op["kind"] != "finish"]
            for task_id, execution_id in active.items():
                if execution_id not in app["cancel_sent"]:
                    operations.append({"kind": "cancel", "task_id": task_id, "reason": app["stop_reason"]})
                    app["cancel_sent"].append(execution_id)
        return operations, app

    @staticmethod
    def _audit_startup_recovery_changes(root, app):
        recoveries = app.get("startup_recoveries")
        recoveries = recoveries if isinstance(recoveries, dict) else {}
        required = app.get("startup_recovery_required")
        required = required if isinstance(required, dict) else {}
        for task_id, record in recoveries.items():
            if not isinstance(record, dict):
                continue
            source = record.get("source_execution_id")
            if isinstance(source, str):
                record_event(root, "startup-recovery:" + source,
                    "startup.recovery_authorized", {"task_id": task_id, **record})
        for task_id, record in required.items():
            if not isinstance(record, dict):
                continue
            source = record.get("execution_id")
            if not isinstance(source, str):
                source = str(task_id)
            record_event(root, "startup-recovery-required:" + source,
                "startup.recovery_required", {"task_id": task_id, **record})

    def _startup_recovery_decision(self, snapshot, header, app, sdk):
        """Park uncertain startup timeouts or authorize one proof-bound retry."""
        operations = []
        if header.get("definition", {}).get("workflow_version", 0) < 22:
            return operations, False

        def park(task_id, execution_id, reason, *, evidence=None):
            wait_id = ("startup-recovery:" + execution_id
                       if isinstance(execution_id, str) and execution_id
                       else "startup-recovery-state-invalid")
            record = {
                "run_id": snapshot.get("run_id"), "task_id": task_id,
                "execution_id": execution_id, "action": "recovery_required",
                "reason": reason, "policy": "pre_handler_once_v1",
                "wait_id": wait_id, "required_at": self.clock(),
            }
            if isinstance(evidence, dict):
                record["evidence"] = evidence
            app.setdefault("startup_recovery_required", {})[task_id] = record
            wait = snapshot.get("waits", {}).get(wait_id)
            if wait is None or wait.get("state") != "open":
                operations.append({"kind": "wait", "wait_id": wait_id,
                    # Keep the execution identity in application evidence only;
                    # the generic stop path must not release this wait twice.
                    "payload": {"kind": "startup_recovery_required",
                                "reason": reason}})
            return operations, True

        required = app.get("startup_recovery_required")
        valid_required = (required is None or isinstance(required, dict))
        if valid_required and isinstance(required, dict):
            valid_required = all(
                isinstance(task_id, str) and isinstance(record, dict)
                and record.get("action") == "recovery_required"
                and isinstance(record.get("reason"), str)
                and isinstance(record.get("wait_id"), str)
                for task_id, record in required.items())
        if not valid_required:
            app["startup_recovery_required"] = {}
            return park("__invalid_state__", None, "startup_recovery_state_invalid")
        if required is None:
            required = {}
        if required:
            if app.get("stop_reason"):
                for record in required.values():
                    wait_id = record.get("wait_id") if isinstance(record, dict) else None
                    wait = snapshot.get("waits", {}).get(wait_id) if wait_id else None
                    if wait and wait.get("state") == "open":
                        operations.append({"kind": "release_wait", "wait_id": wait_id})
                required.clear()
                return operations, False
            for record in required.values():
                wait_id = record["wait_id"]
                wait = snapshot.get("waits", {}).get(wait_id)
                if wait is None or wait.get("state") != "open":
                    operations.append({"kind": "wait", "wait_id": wait_id,
                        # Do not duplicate the execution_id here: the generic
                        # stop path releases execution-scoped waits itself.
                        "payload": {"kind": "startup_recovery_required",
                                    "reason": record["reason"]}})
            return operations, True
        if sdk is None or app.get("stop_reason"):
            return operations, False

        root = Path(header["run_dir"])
        prior_recoveries = app.get("startup_recoveries", {})
        recovery_state_invalid = not isinstance(prior_recoveries, dict)
        if not recovery_state_invalid:
            for prior_task, record in prior_recoveries.items():
                proof = record.get("proof") if isinstance(record, dict) else None
                deadline = record.get("deadline_epoch") if isinstance(record, dict) else None
                authorized = record.get("authorized_at") if isinstance(record, dict) else None
                if (not isinstance(prior_task, str) or not isinstance(record, dict)
                        or not isinstance(record.get("source_execution_id"), str)
                        or not isinstance(record.get("recovery_execution_id"), str)
                        or record["source_execution_id"] == record["recovery_execution_id"]
                        or record.get("policy") != "pre_handler_once_v1"
                        or record.get("reason") != "pre_handler_timeout_proven"
                        or not isinstance(proof, dict) or proof.get("action") != "retry"
                        or proof.get("task_id") != prior_task
                        or proof.get("execution_id") != record.get("source_execution_id")
                        or isinstance(authorized, bool) or not isinstance(authorized, (int, float))
                        or (deadline is not None and
                            (isinstance(deadline, bool) or not isinstance(deadline, (int, float))))):
                    recovery_state_invalid = True
                    break
        for task_id, task in snapshot.get("tasks", {}).items():
            attempts = task.get("attempts", [])
            if not attempts or attempts[-1].get("state") != "timed_out":
                continue
            previous = attempts[-1]
            old_command = previous.get("command", {})
            execution_id = old_command.get("execution_id")
            try:
                operation = OperationInput.from_dict(
                    unpack_input(root, old_command["payload"]))
            except (KeyError, TypeError, ValueError):
                handler_id = old_command.get("handler_id", "")
                stage = handler_id.removeprefix("modport.") if isinstance(handler_id, str) else ""
                if (isinstance(execution_id, str) and agent_stage(header, stage)):
                    return park(task_id, execution_id, "operation_input_unavailable")
                continue
            if not agent_stage(header, operation.stage_id):
                continue
            if not isinstance(execution_id, str):
                continue
            if recovery_state_invalid:
                return park(task_id, execution_id, "startup_recovery_state_invalid")
            deadline = self._effective_deadline(header, app)
            progress = read_execution_progress(root, execution_id)
            from . import coder_revival
            group = app.get("active_group")
            development_task = operation.payload.get("development_task")
            development_name = (development_task.get("id")
                                if isinstance(development_task, dict) else None)
            settled_coder = (coder_revival.enabled(header)
                             and isinstance(group, dict)
                             and group.get("kind") == "development"
                             and isinstance(group.get("members"), list)
                             and task_id in group.get("members", ())
                             and operation.stage_id == "coder"
                             and isinstance(development_name, str)
                             and isinstance(group.get("tasks"), list)
                             and any(isinstance(item, dict) and item.get("id") == development_name
                                     for item in group.get("tasks", ()))
                             and task_id == f"coder.g{group.get('generation')}.{development_name}")
            receipt_verified = False
            settled_effect = None
            receipt_ref = None
            expected_request = {"input_sha256": digest(operation.to_dict()),
                                "run_dir": str(root), "stage": operation.stage_id}
            if settled_coder and isinstance(progress, dict) and progress.get("phase") == "finished":
                receipt_path = root / "artifacts" / "executions" / execution_id / "receipt.json"
                try:
                    if (receipt_path.parent.resolve() == receipt_path.parent.absolute()
                            and receipt_path.is_file() and not receipt_path.is_symlink()):
                        settled_effect = sdk.kernel.get_effect(f"modport:{execution_id}")
                        validate_stage_response(
                            root, operation, settled_effect.response, expected_request)
                        receipt_ref = seal_ref(root, {
                            "path": str(receipt_path.relative_to(root)),
                            "media_type": "application/json",
                        }, execution_id=execution_id)
                        receipt_verified = True
                except (AttributeError, OSError, RuntimeError, ValueError, TypeError,
                        KeyError, sqlite3.Error):
                    # Missing or invalid evidence keeps the attempt parked.
                    pass
            decision = classify_startup_timeout(
                sdk, run_id=snapshot["run_id"], execution_id=execution_id,
                task_id=task_id, application_attempt=operation.attempt,
                command=old_command,
                progress=progress,
                process_isolated=self.isolation_mode == "process",
                already_recovered=task_id in prior_recoveries,
                current_registry_revision=header["registry_revision"],
                deadline_epoch=deadline, now=self.clock(),
                allow_settled_diagnosis=settled_coder,
                receipt_verified=receipt_verified,
                settled_effect=settled_effect,
                expected_effect_request=expected_request)
            if decision["action"] == "diagnose":
                app.setdefault("settled_timeout_diagnoses", {})[task_id] = {
                    **decision,
                    "stage_receipt_ref": receipt_ref,
                }
                continue
            if decision["action"] == "retry":
                next_attempt = len(attempts) + 1
                next_execution_id = f"{snapshot['run_id']}:{task_id}:{next_attempt}"
                remaining = None if deadline is None else deadline - self.clock()
                timeout = float(old_command["timeout_seconds"])
                if remaining is not None:
                    timeout = min(timeout, remaining)
                if timeout <= 0:
                    decision = {**decision, "action": "recovery_required",
                                "reason": "original_run_deadline_exhausted"}
                else:
                    from dataclasses import replace
                    recovered_operation = replace(operation,
                        command_id=next_execution_id, attempt=next_attempt)
                    command = dict(old_command)
                    command.update(
                        execution_id=next_execution_id,
                        idempotency_key=next_execution_id,
                        causation_id=execution_id,
                        timeout_seconds=timeout,
                        payload=pack_input(root, recovered_operation.to_dict()),
                    )
                    prior_recoveries[task_id] = {
                        "source_execution_id": execution_id,
                        "recovery_execution_id": next_execution_id,
                        "policy": decision["policy"],
                        "reason": decision["reason"],
                        "proof": decision,
                        "authorized_at": self.clock(),
                        "assignment_count_unchanged": app.get("agent_assignments", 0),
                        "deadline_epoch": deadline,
                    }
                    app["startup_recoveries"] = prior_recoveries
                    return [{"kind": "new_attempt", "task_id": task_id,
                             "command": command},
                            {"kind": "dispatch", "task_id": task_id}], True

            if decision["action"] == "recovery_required":
                return park(task_id, execution_id, decision["reason"], evidence=decision)
        return operations, False

    def _advance_decision(self, snapshot, header, *, sdk=None, stop_reason=None, stop_state="cancelled"):
        app = json_copy(snapshot["application_state"] or self._new_application())
        operations = []
        if snapshot["state"] in TERMINAL:
            return operations, app
        if stop_reason == "user_cancelled":
            app["user_cancelled"] = True
        if stop_reason and not app["stop_reason"]:
            app["stop_reason"], app["stop_state"] = stop_reason, stop_state
        observe_attempts(app, snapshot)
        from .token_budget import read_token_budget
        token_budget = read_token_budget(header["run_dir"])
        if token_budget["exhausted"] and not app["stop_reason"]:
            app["stop_reason"], app["stop_state"] = "token_budget_exhausted", "failed"
        deadline = self._effective_deadline(header, app)
        administrator_wait = app.get("administrator_wait")
        if (administrator_wait and administrator_wait["deadline_epoch"] is not None
                and self.clock() >= administrator_wait["deadline_epoch"]):
            operations += self._release_administrator_wait(snapshot, app)
            app["stop_reason"], app["stop_state"] = "administrator_wait_expired", "failed"
        if deadline is not None and self.clock() >= deadline and not app["stop_reason"]:
            app["stop_reason"], app["stop_state"] = "wall_clock_budget_exhausted", "failed"
            operations.append({"kind": "signal", "signal_id": "deadline", "payload": {"deadline_epoch": deadline}})
        from .watchdog_routing import decide as watchdog_decide
        watchdog_operations, watchdog_pending = watchdog_decide(self, snapshot, header, app, sdk)
        operations += watchdog_operations
        if watchdog_pending:
            return operations, app
        if progress_supervised(header) and not app['stop_reason']:
            operations += self._progress_supervision_decision(snapshot, header, app)
        startup_operations, startup_parked = self._startup_recovery_decision(
            snapshot, header, app, sdk)
        if startup_parked:
            return operations + startup_operations, app
        operations += startup_operations
        # Persist uncertainty as a wait; no polling heuristic or business retry can resolve it.
        recovering = False
        for stage, task in snapshot["tasks"].items():
            current = task["attempts"][-1]
            execution_id = current["command"]["execution_id"]
            wait_id = f"recovery:{execution_id}"
            wait = snapshot["waits"].get(wait_id)
            if current["state"] == "recovery_required":
                recovering = True
                if wait is None or wait["state"] != "open":
                    # Each return to recovery gets its own wait identity.
                    wait_id = f"recovery:{execution_id}:{current.get('kernel_revision', 0)}"
                    if wait_id not in snapshot["waits"]:
                        operations.append({"kind": "wait", "wait_id": wait_id, "payload": {"execution_id": execution_id}})
            for key, existing in snapshot["waits"].items():
                if existing["state"] == "open" and existing.get("payload", {}).get("execution_id") == execution_id and current["state"] != "recovery_required":
                    operations.append({"kind": "release_wait", "wait_id": key})
        if recovering:
            reason = app["stop_reason"]
            group = app.get("active_group")
            if group and not reason:
                for task_id in group["members"]:
                    task = snapshot["tasks"].get(task_id)
                    if not task:
                        continue
                    current = task["attempts"][-1]
                    if current["state"] == "succeeded":
                        result = OperationResult.from_dict(current["result"]["value"])
                        result.validate_for(OperationInput.from_dict(current["command"]["payload"]))
                        if (not business_gates_disabled(header) and not downstream_toolcall(header) and (result.status != "completed"
                                or (result.stage_id in REVIEW_STAGES and result.outputs.get("verdict") != "approved"))):
                            group.setdefault("failure", result.to_dict())
                    elif (current["state"] in EXECUTION_TERMINAL
                          and not business_gates_disabled(header) and not downstream_toolcall(header)):
                        reason = "group_member_failed"
                if (group.get("failure") and not business_gates_disabled(header)
                        and not downstream_toolcall(header)):
                    reason = "group_member_failed"
            if reason:
                for task_id, task in snapshot["tasks"].items():
                    current = task["attempts"][-1]
                    execution_id = current["command"]["execution_id"]
                    can_cancel_recovery = (current["state"] != "recovery_required"
                                           or app.get("user_cancelled") is True)
                    if (current["state"] not in EXECUTION_TERMINAL
                            and can_cancel_recovery
                            and execution_id not in app["cancel_sent"]):
                        operations.append({"kind": "cancel", "task_id": task_id, "reason": reason})
                        app["cancel_sent"].append(execution_id)
            return operations, app
        if business_gates_disabled(header):
            if app["stop_reason"]:
                if header.get('definition', {}).get('workflow_version', 0) >= 21:
                    for record in app.get('review_rework', {}).get('requests', {}).values():
                        if record.get('waiting_resources'):
                            record.update(state='failed', waiting_resources=False,
                                          error=app['stop_reason'])
                # Deadline exhaustion and explicit host cancellation remain real
                # operational controls in a gate-free workflow.
                operations += self._release_administrator_wait(snapshot, app)
                active = [(stage, task["attempts"][-1])
                          for stage, task in snapshot["tasks"].items()
                          if task["attempts"][-1]["state"] not in EXECUTION_TERMINAL]
                for stage, attempt in active:
                    execution_id = attempt["command"]["execution_id"]
                    if execution_id not in app["cancel_sent"]:
                        operations.append({"kind": "cancel", "task_id": stage,
                                           "reason": app["stop_reason"]})
                        app["cancel_sent"].append(execution_id)
                self._settle_user_cancelled_review_requests(snapshot, app)
                if not active:
                    if required_behavior_policy(header):
                        archive_required_behavior_failure(header, app, app['stop_reason'])
                    operations += self._finish(app, app["stop_reason"], app["stop_state"])
                return operations, app
            if header.get('definition', {}).get('workflow_version', 0) >= 26:
                operations += self._supervision_decision(snapshot, header, app)
                if app.get('stop_reason') or any(op['kind'] == 'finish' for op in operations):
                    return operations, app
            operations += self._release_administrator_wait(snapshot, app)
            operations += self._review_rework_decision(snapshot, header, app)
            if app.get('stop_reason') or any(row['state'] == 'running'
                    for row in app.get('review_rework', {}).get('requests', {}).values()):
                return operations, app
            # Explicitly imported evidence still gets its independent review;
            # its verdict has no authority to pause the main workflow.
            try:
                operations += self._support_decision(snapshot, header, app)
            except (KeyError, TypeError, ValueError) as error:
                app.setdefault('business_diagnostics', []).append({
                    'stage': 'support', 'detail': str(error),
                    'status': 'failed', 'error_code': 'support_observation_invalid'})
            return operations + self._advance_without_business_gates(snapshot, header, app), app
        operations += self._supervision_decision(snapshot, header, app)
        if app["stop_reason"]:
            operations += self._release_administrator_wait(snapshot, app)
            active = [(stage, task["attempts"][-1]) for stage, task in snapshot["tasks"].items()
                      if task["attempts"][-1]["state"] not in EXECUTION_TERMINAL]
            for stage, attempt in active:
                execution_id = attempt["command"]["execution_id"]
                if execution_id not in app["cancel_sent"]:
                    operations.append({"kind": "cancel", "task_id": stage, "reason": app["stop_reason"]})
                    app["cancel_sent"].append(execution_id)
            if not active:
                operations += self._finish(app, app["stop_reason"], app["stop_state"])
            return operations, app
        operations += self._review_rework_decision(snapshot, header, app)
        handoffs = self._gate_handoff_decision(snapshot, header, app)
        if handoffs:
            # Newly dispatched consumers are not in this SDK snapshot yet.
            return operations + handoffs, app
        operations += self._support_decision(snapshot, header, app)
        if app.get('stop_reason') or any(row['state'] == 'running'
                for row in app.get('review_rework', {}).get('requests', {}).values()):
            return operations, app
        if app.get("administrator_wait"):
            return operations, app
        if app.pop("knowledge_rescan_pending", False) and not downstream_toolcall(header):
            if app.get("gap_pending"):
                app["knowledge_rescan_pending"] = True
            else:
                operations += self._schedule(snapshot, header, app, "mod_scan", activate=False, dependencies=[],
                    payload={"knowledge_revision_refresh": True})
                app["gap_pending"] = ["mod_scan"]
        if app.get("gap_pending"):
            operations += self._background_gap_decision(snapshot, header, app)
            if app["stop_reason"]:
                return operations, app
        if app.get("gap_join_stage") and downstream_toolcall(header):
            next_stage = app.pop("gap_join_stage")
            app.pop("gap_join_ids", None)
            return operations + self._schedule(snapshot, header, app, next_stage), app
        if app.get("gap_join_stage"):
            unresolved = {gap["gap_id"] for gap in self._knowledge_gaps(app)}
            waiting_on = set(app.get("gap_join_ids", unresolved))
            if unresolved & waiting_on:
                if not app.get("gap_pending"):
                    planned = self._maybe_gap_plan(snapshot, header, app)
                    return operations + planned + ([] if planned else self._park_administrator(snapshot, header, app)), app
                return operations, app
            next_stage = app.pop("gap_join_stage")
            app.pop("gap_join_ids", None)
            return operations + self._schedule(snapshot, header, app, next_stage), app
        if app.get("active_group"):
            return operations + self._group_decision(snapshot, header, app), app
        if (app["active_stage"] is None and not app.get("early_active")
                and any(row["state"] in {"pending", "running"}
                        for row in app.get("gate_diagnostics", {}).values())):
            return operations, app
        if app.get("early_active") or (app["active_stage"] is None
                and not app["effective"] and header["request"].get("workflow_mode") != "skill_generation"):
            app["early_active"] = True
            return operations + self._early_decision(snapshot, header, app), app
        stage = app["active_stage"]
        if stage is None:
            entry = "skill_lookup" if header["request"].get("workflow_mode") == "skill_generation" else "source"
            return operations + self._schedule(snapshot, header, app, entry, dependencies=[]), app
        original_task = snapshot["tasks"].get(stage)
        if original_task is None and stage not in app.get("gate_resumptions", {}):
            return operations, app
        attempt, resumed = self._resumed_attempt(snapshot, app, stage,
            original_task["attempts"][-1] if original_task else {})
        if attempt["state"] not in EXECUTION_TERMINAL:
            return operations, app
        execution_id = attempt["command"]["execution_id"]
        if execution_id in app["processed"] and not resumed:
            return operations, app
        if not resumed:
            app["processed"].append(execution_id)
        if attempt["state"] != "succeeded":
            app["history"].append({"stage": stage, "execution_id": execution_id, "state": attempt["state"]})
            if downstream_toolcall(header):
                command = OperationInput.from_dict(attempt["command"]["payload"])
                outcome = OperationResult("blocked", command.run_id, command.task_id,
                    command.stage_id, command.command_id, error_code="execution_" + attempt["state"])
                app["effective"][stage] = outcome.to_dict()
                return operations + self._repair_failure(snapshot, header, app, command.stage_id,
                    outcome, execution_id), app
            return operations + self._finish(app, f"execution_{attempt['state']}"), app
        outcome = OperationResult.from_dict(attempt["result"]["value"])
        outcome.validate_for(OperationInput.from_dict(attempt["command"]["payload"]))
        active_task_id = stage
        stage = outcome.stage_id
        result = outcome.to_dict()
        self._capture_repair_result(header, app, result)
        app["history"].append({"stage": stage, "execution_id": execution_id, "state": outcome.status,
                               "task_id": active_task_id, "error_code": outcome.error_code,
                               "verdict": outcome.outputs.get("verdict")})
        # All historical outcomes remain authoritative in their SDK attempts.
        app["effective"].pop(stage, None)
        app["effective"][stage] = result
        if active_task_id != stage:
            app["effective"][active_task_id] = result
        successful = outcome.status == "completed" and (stage not in REVIEW_STAGES or outcome.outputs.get("verdict") == "approved")
        if successful:
            app["format_context"] = None
            app.get("format_retries", {}).pop(stage, None)
            self._ingest_deferred_obligations(app, outcome)
            workflow_version = header.get("definition", {}).get("workflow_version", 0)
            repeat_plan_check, planning_error = self._record_fixed_markdown_revision(
                header, app, stage, outcome, execution_id)
            if planning_error:
                if downstream_toolcall(header):
                    diagnostic = OperationResult("blocked", outcome.run_id, outcome.task_id,
                        stage, execution_id, outcome.outputs, error_code=planning_error)
                    return operations + self._repair_failure(snapshot, header, app, stage,
                        diagnostic, execution_id), app
                return operations + self._finish(app, planning_error), app
            if repeat_plan_check:
                return operations + self._schedule(snapshot, header, app, stage,
                    causation_id=execution_id,
                    payload={"plan_refinement_round": app["plan_documents"][
                        stage.split("_", 1)[0] if stage.startswith(("contract_", "target_"))
                        else "migration"]["rounds"]}), app
            if stage == "mod_analysis":
                self._ingest_analysis(app, outcome.outputs)
                app["knowledge_gap_context"] = None
                app["rework_context"] = None
                if header.get("inherit_harness"):
                    return operations + self._schedule(snapshot, header, app, "contract_restore",
                        causation_id=execution_id), app
            for key, value in outcome.outputs.get("locked_artifacts", {}).items():
                app["locked_artifacts"][key] = value
            if stage == "skill_lookup":
                initialize_research(app, outcome.outputs)
            if stage in {"skill_publish", "knowledge_publish"}:
                app["knowledge_revisions"].update(outcome.outputs.get("knowledge_revisions", {}))
            if stage == "skill_lookup" and (outcome.outputs.get("missing_kinds") or outcome.outputs.get("needs_review_kinds")):
                kinds = outcome.outputs.get("missing_kinds", [])
                reviews = outcome.outputs.get("needs_review_kinds", [])
                if (not isinstance(kinds, list) or not isinstance(reviews, list)
                        or any(kind not in {"platform", "java"} for kind in kinds + reviews)
                        or len(kinds + reviews) != len(set(kinds + reviews))):
                    return operations + self._finish(app, "skill_lookup_invalid"), app
                app["active_stage"] = None
                app["active_group"] = {"kind": "skills", "phase": "pipeline", "kinds": kinds + reviews,
                    "missing_kinds": kinds,
                    "members": [kind + "_diff" for kind in kinds] + [kind + "_skill_review" for kind in reviews],
                    "results": {}}
                for member in app["active_group"]["members"]:
                    operations += self._schedule(snapshot, header, app, member, activate=False, dependencies=["skill_lookup"])
                return operations, app
            if stage == "skill_publish" and header["request"].get("workflow_mode") == "skill_generation":
                if downstream_toolcall(header):
                    lookup = app["effective"].get("skill_lookup", {}).get("outputs", {})
                    generated = set(lookup.get("missing_kinds", []))
                    required = generated | set(lookup.get("needs_review_kinds", []))
                    prior_group = app.get("failed_development_group") or {}
                    if prior_group.get("kind") == "skills":
                        required.update(prior_group.get("kinds", []))
                        generated.update(prior_group.get("missing_kinds", []))
                    if any(not passed(app["effective"].get(kind + "_skill_review", {}))
                           for kind in required) or any(
                            not passed(app["effective"].get(kind + "_diff", {})) for kind in generated):
                        return operations + self._finish(app, "skills_incomplete"), app
                return operations + self._finish(app, "skills_published", "succeeded"), app
            if stage == "gap_review":
                for identity in outcome.outputs.get("verified_gap_obligations", []):
                    if identity in app.get("project_verification_gaps", {}):
                        app["project_verification_gaps"][identity]["project_status"] = "passed"
            if stage == "code_review" and header.get("definition", {}).get("workflow_version", 11) >= 12:
                from .regression import start_regression
                return operations + start_regression(self, snapshot, header, app, outcome), app
            if stage == "contract_freeze":
                app["planning_generation"] += 1
                app.setdefault("plan_documents", {}).pop("migration", None)
            if stage.endswith("_repair_review") and outcome.outputs.get("parallel_decision") == "replan":
                if downstream_toolcall(header):
                    return operations + self._schedule(snapshot, header, app, NEXT_STAGE[stage],
                        causation_id=execution_id), app
                if workflow_version >= 15:
                    return operations + self._finish(
                        app, "planning_dispatch_decision_invalid"), app
                return operations + self._repair_review_replan(snapshot, header, app, stage, outcome), app
            if stage == "parallel_review":
                decision = outcome.outputs.get("parallel_decision")
                if downstream_toolcall(header) and decision not in {"parallel", "sequential"}:
                    diagnostic = OperationResult("blocked", outcome.run_id, outcome.task_id,
                        stage, execution_id, outcome.outputs, error_code="planning_dispatch_decision_invalid")
                    return operations + self._repair_failure(snapshot, header, app, stage,
                        diagnostic, execution_id), app
                if workflow_version >= 15 and decision not in {"parallel", "sequential"}:
                    return operations + self._finish(app, "planning_dispatch_decision_invalid"), app
                if decision == "prepare_first":
                    # One preparation may be followed by a new independent assessment.
                    # Repeated preparation requests are business rework, never hidden calls.
                    key = "development_prepare"
                    prepared = app["effective"].get(key)
                    if prepared and not self._charge_rework(header, app, key):
                        return operations + self._finish(app, key + "_rounds_exhausted"), app
                    return operations + self._schedule(snapshot, header, app, key, causation_id=execution_id), app
                if decision == "replan":
                    route = outcome.outputs.get("replan_stage")
                    if route not in PLANNING_STAGES[:-1]:
                        return operations + self._finish(app, "parallel_decision_invalid"), app
                    if not self._charge_rework(header, app, "migration_plan"):
                        return operations + self._finish(app, "migration_plan_rounds_exhausted"), app
                    self._rework_context(app, stage, outcome, execution_id)
                    self._record_repair_feedback(app)
                    route = self._invalidate_planning(app, route)
                    return operations + self._schedule(snapshot, header, app, route, causation_id=execution_id), app
                if decision not in {"parallel", "sequential"}:
                    return operations + self._finish(app, "parallel_decision_invalid"), app
            if stage == "development_prepare" and outcome.outputs.get("replan_stage"):
                if downstream_toolcall(header):
                    diagnostic = OperationResult("blocked", outcome.run_id, outcome.task_id,
                        stage, execution_id, outcome.outputs, error_code="preparation_requests_revision")
                    return operations + self._repair_failure(snapshot, header, app, stage,
                        diagnostic, execution_id), app
                if workflow_version >= 15:
                    return operations + self._finish(
                        app, "scoped_development_repair_required"), app
                route = outcome.outputs["replan_stage"]
                if route not in PLANNING_STAGES[:-1]:
                    return operations + self._finish(app, "preparation_decision_invalid"), app
                if not self._charge_rework(header, app, "migration_plan"):
                    return operations + self._finish(app, "migration_plan_rounds_exhausted"), app
                self._rework_context(app, stage, outcome, execution_id)
                self._record_repair_feedback(app)
                route = self._invalidate_planning(app, route)
                return operations + self._schedule(snapshot, header, app, route, causation_id=execution_id), app
            if stage == "development_prepare_integrate":
                app["effective"]["development_prepare"] = result
            if stage in {"implementation", "contract_revise", "target_revise", "development_prepare"} and outcome.outputs.get("development_tasks"):
                self._start_development_group(header, app, stage, outcome.outputs)
                return operations + self._group_decision(snapshot, header, app), app
            if stage == "delivery":
                gates = [s for s in MAIN_STAGES if s not in {"contract_draft", "implementation"}]
                if header['definition'].get('workflow_version', 0) < 13:
                    gates.remove('test_review')
                implementation_passed = any(app["effective"].get(s, {}).get("status") == "completed"
                                            for s in ("implementation", "target_revise"))
                unresolved_group = downstream_toolcall(header) and bool(app.get("failed_development_group"))
                if unresolved_group or not implementation_passed or not all(s in app["effective"] and passed(app["effective"][s]) for s in gates):
                    return operations + self._finish(app, "acceptance_gates_incomplete"), app
                validation_policy = header.get("definition", {}).get("validation_policy", {})
                if (isinstance(validation_policy, Mapping)
                        and validation_policy.get("acceptance_status") == "unverified"):
                    reason = "delivery_completed_acceptance_unverified"
                else:
                    reason = "all_acceptance_gates_passed"
                return operations + self._finish(app, reason, "succeeded"), app
            if (NEXT_STAGE[stage] == "development_integrate" and self._knowledge_gaps(app)
                    and not downstream_toolcall(header)):
                app["gap_join_stage"] = "development_integrate"
                return operations, app
            next_stage = NEXT_STAGE[stage]
            if stage == 'test_design' and header['definition'].get('workflow_version', 0) < 13:
                next_stage = 'test_execute'
            return operations + self._schedule(snapshot, header, app, next_stage, causation_id=execution_id), app
        return operations + self._repair_failure(snapshot, header, app, stage, outcome, execution_id), app

    @staticmethod
    def _ingest_deferred_obligations(app, outcome):
        if outcome.stage_id not in {"parallel_review", "contract_repair_review", "target_repair_review",
                                    "migration_tasks", "contract_repair_tasks", "target_repair_tasks"}:
            return
        if outcome.outputs.get("parallel_decision") not in {"parallel", "sequential"}:
            return
        for obligation in outcome.outputs.get("deferred_obligations", []):
            identity = obligation["id"]
            existing = app.setdefault("project_verification_gaps", {}).get(identity)
            row = {**obligation, "gap_id": identity, "kind": "verification", "applicable": True,
                   "project_status": "pending", "status": "unresolved",
                   "planning_evidence": outcome.outputs.get("artifact_refs", {}).get(outcome.stage_id),
                   "producer_execution_id": outcome.command_id}
            if existing:
                # A later plan cannot waive criteria carried by an earlier one.
                row["closure_criteria"] = list(dict.fromkeys(
                    existing.get("closure_criteria", []) + row["closure_criteria"]))
            app["project_verification_gaps"][identity] = row

    @staticmethod
    def _record_fixed_markdown_revision(header, app, stage, outcome, execution_id):
        """Persist v15's three fixed plan revisions and request the second check.

        The legacy stage identifiers remain stable, so the refinement stage is
        used twice.  Its first execution improves the draft and its second checks
        only for major omissions.  A model cannot shorten or extend this sequence.
        """
        if header.get("definition", {}).get("workflow_version", 0) < 15:
            return False, None
        drafts = {"migration_inventory", "contract_diagnose", "target_diagnose"}
        refiners = {"migration_plan", "contract_repair_plan", "target_repair_plan"}
        if stage not in drafts | refiners:
            return False, None
        scope = (stage.split("_", 1)[0]
                 if stage.startswith(("contract_", "target_")) else "migration")
        ref = outcome.outputs.get("artifact_refs", {}).get("current_plan")
        if business_gates_disabled(header):
            if not isinstance(ref, dict):
                return False, "planning_markdown_unavailable"
            revision = 1 if stage in drafts else 2
            app.setdefault("plan_documents", {})[scope] = {
                "rounds": revision, "status": "continue" if revision == 1 else "ready",
                "current_ref": json_copy(ref), "producer_execution_id": execution_id,
                "planning_generation": app.get("planning_generation", 0),
                "repair_generation": app.get("repair_generation", 0),
            }
            return False, None
        revision = outcome.outputs.get("plan_revision")
        status = outcome.outputs.get("plan_status")
        if (not isinstance(ref, dict) or type(revision) is not int
                or revision not in {1, 2, 3}):
            return False, "planning_markdown_state_invalid"
        expected_status = "ready" if revision == 3 or (downstream_toolcall(header) and revision == 2) else "continue"
        if status != expected_status:
            return False, "planning_markdown_state_invalid"
        previous = app.setdefault("plan_documents", {}).get(scope)
        if stage in drafts:
            if revision != 1:
                return False, "planning_markdown_revision_invalid"
        elif (not isinstance(previous, dict)
              or revision != previous.get("rounds", 0) + 1
              or ref.get("metadata", {}).get("parent_sha256")
              != previous.get("current_ref", {}).get("sha256")):
            return False, "planning_markdown_revision_invalid"
        app["plan_documents"][scope] = {
            "rounds": revision, "status": status, "current_ref": json_copy(ref),
            "producer_execution_id": execution_id,
            "planning_generation": app.get("planning_generation", 0),
            "repair_generation": app.get("repair_generation", 0),
        }
        return stage in refiners and revision < 3 and not downstream_toolcall(header), None

    def _repair_review_replan(self, snapshot, header, app, stage, outcome):
        if downstream_toolcall(header):
            return self._diagnostic_handoff(snapshot, header, app, stage, outcome,
                outcome.command_id, location="early" if app.get("early_active") else "main")
        scope = stage.split("_", 1)[0]
        chain = [item for item in REPAIR_PLANNING_STAGES if item.startswith(scope + "_")]
        route = outcome.outputs.get("replan_stage")
        if route not in chain[:-1]:
            return self._finish(app, "repair_review_decision_invalid")
        if not self._charge_rework(header, app, scope + "_revise"):
            return self._finish(app, scope + "_revise_rounds_exhausted")
        self._rework_context(app, stage, outcome, outcome.command_id)
        self._record_repair_feedback(app)
        for item in chain[chain.index(route):]:
            app["effective"].pop(item, None)
            app["format_retries"].pop(item, None)
        app["format_context"] = None
        return self._schedule(snapshot, header, app, route, causation_id=outcome.command_id)

    def _start_development_group(self, header, app, stage, outputs):
        app["development_generation"] += 1
        app["active_stage"] = None
        scope = outputs.get("goal_scope", "migration")
        stages = PLANNING_STAGES if scope == "migration" else tuple(
            item for item in REPAIR_PLANNING_STAGES if item.startswith(scope + "_"))
        refs = self._refs(header, app)
        refs.update(outputs.get("artifact_refs", {}))
        if header.get("definition", {}).get("workflow_version", 0) >= 15:
            context_aliases = ("current_plan", stages[2], stages[3], "development_plan")
        else:
            context_aliases = stages
        workflow_version = header.get("definition", {}).get("workflow_version", 0)
        if workflow_version >= 19:
            # v19+ migration planning is a single planner handoff. The legacy
            # migration_tasks/parallel_review aliases are not emitted on that
            # route, so pass the actual planner report and its authenticated input
            # references to goal_prepare and, through coder_goal, to each coder.
            planner_stage = ("migration_plan" if scope == "migration"
                             else scope + "_repair_plan")
            context_aliases = (*context_aliases, planner_stage,
                "planning_report:" + planner_stage, "planning_input_index",
                "planning_input_report")
            if scope == "migration":
                context_aliases = (*context_aliases, "migration_inventory",
                    "mod_scan_report", "early_compile", "codemod")
            context_aliases = tuple(dict.fromkeys(context_aliases))
        app["active_group"] = {
            "kind": "development", "generation": app["development_generation"],
            "tasks": outputs["development_tasks"], "base": outputs["development_base"],
            "parallel": True, "members": [], "scheduled": [], "results": {},
            "goal_scheduled": [], "entry_stage": stage, "goal_scope": scope,
            "planning_context": {item: refs[item] for item in context_aliases if item in refs},
            "artifact_refs": refs,
            "execution_payload": {key: value for key, value in outputs.items()
                                  if key not in {"development_tasks", "development_base", "artifact_refs"}},
        }

    def _early_schedule(self, snapshot, header, app, stage, **kwargs):
        if app["stop_reason"]:
            return []
        branch = ("analysis" if stage in {"mod_analysis", "gap_research"}
                  else "contract" if stage.startswith("contract_") else "setup")
        app["rework_context"] = app.setdefault("early_rework", {}).get(branch)
        operations = self._schedule(snapshot, header, app, stage, activate=False, **kwargs)
        if any(op["kind"] == "dispatch" for op in operations):
            app.setdefault("early_pending", []).append(stage)
        return operations

    def _early_decision(self, snapshot, header, app):
        """Advance independent preparation, research and characterization branches.

        A business failure blocks its consumers. Other ready branches continue,
        including characterization while migration knowledge is being researched.
        The persisted pending list supports restart without duplicate dispatches.
        """
        pending = app.setdefault("early_pending", [])
        failures = app.setdefault("early_failures", {})
        operations = []

        def completed(stage):
            result = app["effective"].get(stage, {})
            if downstream_toolcall(header) and forwarded(app, result):
                return True
            return result.get("status") == "completed" and (
                stage not in REVIEW_STAGES or result.get("outputs", {}).get("verdict") == "approved")

        for stage in list(pending):
            task = snapshot["tasks"].get(stage)
            if task is None and stage not in app.get("gate_resumptions", {}):
                continue
            attempt, resumed = self._resumed_attempt(snapshot, app, stage,
                task["attempts"][-1] if task else {})
            if attempt["state"] not in EXECUTION_TERMINAL:
                continue
            execution_id = attempt["command"]["execution_id"]
            if execution_id in app["processed"] and not resumed:
                continue
            pending.remove(stage)
            if not resumed:
                app["processed"].append(execution_id)
            command = OperationInput.from_dict(attempt["command"]["payload"])
            stage = command.stage_id
            if attempt["state"] == "succeeded":
                outcome = OperationResult.from_dict(attempt["result"]["value"])
                outcome.validate_for(command)
            else:
                outcome = OperationResult("blocked", command.run_id, command.task_id, stage,
                    execution_id, error_code="execution_" + attempt["state"])
            previous_analysis = app["effective"].get("mod_analysis")
            self._capture_repair_result(header, app, outcome.to_dict())
            app["effective"][stage] = outcome.to_dict()
            if (stage == "mod_analysis" and outcome.status != "completed"
                    and outcome.error_code != "relevant_skill_gap" and previous_analysis
                    and previous_analysis.get("error_code") == "relevant_skill_gap"):
                app["effective"][stage] = previous_analysis
            if stage == "mod_analysis" and (outcome.status == "completed" or outcome.error_code == "relevant_skill_gap"):
                self._ingest_analysis(app, outcome.outputs)
            app["history"].append({"stage": stage, "execution_id": execution_id,
                "state": outcome.status, "error_code": outcome.error_code,
                "verdict": outcome.outputs.get("verdict")})
            if outcome.status != "completed" or (stage in REVIEW_STAGES and outcome.outputs.get("verdict") != "approved"):
                branch = ("analysis" if stage in {"mod_analysis", "gap_research"}
                          else "contract" if stage.startswith("contract_") else "setup")
                app["rework_context"] = app.setdefault("early_rework", {}).get(branch)
                repair = self._repair_failure(snapshot, header, app, stage, outcome, execution_id,
                                              location="early")
                app["early_rework"][branch] = app["rework_context"]
                app["active_stage"] = None
                if any(op["kind"] == "finish" for op in repair):
                    failures[stage] = app.pop("terminal_reason", None) or outcome.error_code or stage + "_failed"
                    app["terminal_reason"] = None
                    # _finish clears the scalar stage/group, but a failed branch
                    # must not abandon other tasks already running on the host.
                    if "budget" in failures[stage] and failures[stage] != "research_budget_exhausted":
                        app["stop_reason"], app["stop_state"] = failures[stage], "failed"
                else:
                    operations += repair
                    pending.extend(op["task_id"] for op in repair if op["kind"] == "dispatch"
                                   and not handoff_task(op["task_id"]))
                continue
            failures.pop(stage, None)
            repeat_plan_check, planning_error = self._record_fixed_markdown_revision(
                header, app, stage, outcome, execution_id)
            if planning_error:
                if downstream_toolcall(header):
                    diagnostic = OperationResult("blocked", outcome.run_id, outcome.task_id,
                        stage, execution_id, outcome.outputs, error_code=planning_error)
                    operations += self._repair_failure(snapshot, header, app, stage, diagnostic,
                        execution_id, location="early")
                    continue
                failures[stage] = planning_error
                app["stop_reason"], app["stop_state"] = planning_error, "failed"
                continue
            if repeat_plan_check:
                scope = stage.split("_", 1)[0]
                operations += self._early_schedule(snapshot, header, app, stage,
                    causation_id=execution_id, dependencies=[stage],
                    payload={"plan_refinement_round": app["plan_documents"][scope]["rounds"]})
                continue
            self._ingest_deferred_obligations(app, outcome)
            if (header.get("definition", {}).get("workflow_version", 0) < 15
                    and stage.endswith("_repair_review")
                    and outcome.outputs.get("parallel_decision") == "replan"):
                repair = self._repair_review_replan(snapshot, header, app, stage, outcome)
                app["active_stage"] = None
                operations += repair
                pending.extend(op["task_id"] for op in repair if op["kind"] == "dispatch")
                if any(op["kind"] == "finish" for op in repair):
                    return operations
                continue
            if (app.get("format_context") or {}).get("stage") == stage:
                app["format_context"] = None
            app["format_retries"].pop(stage, None)
            app["locked_artifacts"].update(outcome.outputs.get("locked_artifacts", {}))
            if stage == "skill_publish":
                app.setdefault("knowledge_revisions", {}).update(outcome.outputs.get("knowledge_revisions", {}))
            if stage == "mod_analysis":
                app["knowledge_gap_context"] = None
                app.setdefault("early_rework", {})["analysis"] = None
            if stage == "skill_lookup":
                initialize_research(app, outcome.outputs)
                kinds = outcome.outputs.get("missing_kinds", [])
                reviews = outcome.outputs.get("needs_review_kinds", [])
                if (not isinstance(kinds, list) or not isinstance(reviews, list)
                        or any(kind not in {"platform", "java"} for kind in kinds + reviews)
                        or len(kinds + reviews) != len(set(kinds + reviews))):
                    failures[stage] = "skill_lookup_invalid"
                    continue
                app["early_missing_skills"] = kinds
                app["early_skill_reviews"] = kinds + reviews
                for kind in kinds:
                    operations += self._early_schedule(snapshot, header, app, kind + "_diff")
                for kind in reviews:
                    operations += self._early_schedule(snapshot, header, app, kind + "_skill_review",
                        dependencies=["skill_lookup"])
            elif stage in {"platform_diff", "java_diff"}:
                operations += self._early_schedule(snapshot, header, app, stage.replace("_diff", "_skill_review"))
            elif stage == "gap_research":
                operations += self._early_schedule(snapshot, header, app, "research_review", dependencies=[stage], causation_id=execution_id)
            elif stage == "research_review":
                if outcome.outputs.get("verdict") == "approved":
                    self._apply_gap_resolutions(app, outcome.outputs.get("approved_gap_resolutions", []), execution_id)
                    self._ingest_reviewed_requirements(app, outcome, execution_id,
                        normalize_duplicates=downstream_toolcall(header))
                    generic = outcome.outputs.get("approved_generic_knowledge_entries", {})
                    if generic:
                        operations += self._early_schedule(snapshot, header, app, "knowledge_publish", dependencies=[stage],
                            payload={"generic_knowledge_entries": generic}, causation_id=execution_id)
                    elif not downstream_toolcall(header):
                        operations += self._early_schedule(snapshot, header, app, "mod_analysis", dependencies=[stage], causation_id=execution_id)
                else:
                    app["gap_failure"] = "research_review_rejected"
            elif stage == "knowledge_publish":
                app["knowledge_revisions"].update(outcome.outputs.get("knowledge_revisions", {}))
                if downstream_toolcall(header):
                    continue
                app["early_knowledge_rescan"] = True
                operations += self._early_schedule(snapshot, header, app, "mod_scan", dependencies=[stage], causation_id=execution_id)
            elif stage == "mod_scan" and app.pop("early_knowledge_rescan", False):
                operations += self._early_schedule(snapshot, header, app, "mod_analysis", dependencies=[stage], causation_id=execution_id)
            elif stage == "contract_revise" and outcome.outputs.get("development_tasks"):
                self._start_development_group(header, app, stage, outcome.outputs)
                return operations + self._group_decision(snapshot, header, app)
            elif stage in {"contract_restore", "contract_revise", "contract_repair_integrate",
                           "contract_diagnose", "contract_repair_plan", "contract_repair_tasks",
                           "contract_repair_review"}:
                operations += self._early_schedule(snapshot, header, app, NEXT_STAGE[stage],
                    causation_id=execution_id,
                    dependencies=[stage])

        if app["stop_reason"]:
            return operations
        contract_repair_stages = {"contract_diagnose", "contract_repair_plan",
                                  "contract_repair_tasks", "contract_repair_review", "contract_revise",
                                  "contract_repair_integrate"}
        for stage in EARLY_STAGES:
            # Invalidated verification must consume the repaired harness, not
            # the still-completed draft while its repair branch is in flight.
            if (stage in {"contract_verify", "contract_review", "contract_freeze"}
                    and contract_repair_stages.intersection(set(pending) | set(failures))):
                continue
            if stage in pending or stage in app["effective"] or stage in failures:
                continue
            dependencies = DEPENDENCIES[stage]
            if stage == "skill_publish":
                dependencies = ("skill_lookup", *[kind + "_skill_review"
                    for kind in app.get("early_skill_reviews", [])])
            if stage == "contract_draft" and header.get("inherit_harness"):
                if "contract_restore" not in app["effective"] and "contract_restore" not in pending:
                    if all(completed(dep) for dep in DEPENDENCIES["contract_restore"]):
                        operations += self._early_schedule(snapshot, header, app, "contract_restore")
                continue
            if stage == "contract_verify" and header.get("inherit_harness"):
                dependencies = ("contract_restore",)
            if all(completed(dep) and dep not in failures for dep in dependencies):
                operations += self._early_schedule(snapshot, header, app, stage, dependencies=dependencies)
            if app["stop_reason"]:
                return operations
        analysis = app["effective"].get("mod_analysis", {})
        partial_analysis = (analysis.get("error_code") == "relevant_skill_gap"
                            and analysis.get("outputs", {}).get("research_repairable") is True)
        if completed("contract_freeze") and (completed("mod_analysis") or partial_analysis):
            # Planning consumes the valid initial analysis, including its explicit
            # unknowns. Research/reassessment continues beside unrelated coders.
            if set(pending) <= {"mod_analysis", "gap_research", "research_review", "knowledge_publish", "mod_scan"} and set(failures) <= {"mod_analysis", "gap_research", "research_review", "knowledge_publish", "mod_scan"}:
                app["gap_pending"] = list(pending)
                app["gap_failure"] = next(iter(failures.values()), app.get("gap_failure"))
                app["gap_rework"] = app.get("early_rework", {}).get("analysis")
                app["early_pending"] = []
                app["early_active"] = False
                app["rework_context"] = None
                app["format_context"] = None
                app["planning_generation"] += 1
                app.setdefault("plan_documents", {}).pop("migration", None)
                return operations + self._schedule(snapshot, header, app, "migration_inventory",
                    # The valid initial analysis is already in upstream_results.
                    # Depending on its task here would wait for a newer ongoing
                    # reassessment instead of the report this command consumes.
                    dependencies=["contract_freeze"] if partial_analysis else None)
        if not pending:
            if downstream_toolcall(header) and any(row["state"] in {"pending", "running"}
                    for row in app.get("gate_diagnostics", {}).values()):
                return operations
            if failures:
                if downstream_toolcall(header):
                    for failed_stage in list(failures):
                        value = app["effective"].get(failed_stage)
                        if value is None:
                            continue
                        if passed(value) or forwarded(app, value):
                            failures.pop(failed_stage, None)
                            continue
                        outcome = OperationResult.from_dict(value)
                        operations += self._diagnostic_handoff(snapshot, header, app,
                            failed_stage, outcome, outcome.command_id, location="early")
                    return operations
                if "research_budget_exhausted" in failures.values():
                    # A limited, independently reviewed catalog lets analysis
                    # express actual mod gaps even when initial research failed.
                    if not app.get("initial_research_fallback") and not downstream_toolcall(header):
                        app["initial_research_fallback"] = True
                        for failed_stage in list(failures):
                            if failures[failed_stage] == "research_budget_exhausted":
                                del failures[failed_stage]
                        app["effective"].pop("skill_lookup", None)
                        return operations + self._early_schedule(snapshot, header, app, "skill_lookup", dependencies=[],
                            payload={"allow_empty_research_material": True})
                    return operations + self._park_administrator(snapshot, header, app, "initial_research_incomplete")
                return operations + self._finish(app, next(iter(failures.values())))
        return operations

    def _background_gap_decision(self, snapshot, header, app):
        """Run the bounded research loop without overwriting coder repair context."""
        operations = []
        for stage in list(app["gap_pending"]):
            task = snapshot["tasks"].get(stage)
            if task is None and stage not in app.get("gate_resumptions", {}):
                continue
            attempt, resumed = self._resumed_attempt(snapshot, app, stage,
                task["attempts"][-1] if task else {})
            execution_id = attempt["command"]["execution_id"]
            if attempt["state"] not in EXECUTION_TERMINAL or (execution_id in app["processed"] and not resumed):
                continue
            app["gap_pending"].remove(stage)
            if not resumed:
                app["processed"].append(execution_id)
            command = OperationInput.from_dict(attempt["command"]["payload"])
            stage = command.stage_id
            if attempt["state"] == "succeeded":
                outcome = OperationResult.from_dict(attempt["result"]["value"])
                outcome.validate_for(command)
            else:
                outcome = OperationResult("blocked", command.run_id, command.task_id, stage,
                    execution_id, error_code="execution_" + attempt["state"])
            if (stage != "mod_analysis" or outcome.status == "completed"
                    or outcome.error_code == "relevant_skill_gap"):
                app["effective"][stage] = outcome.to_dict()
            if stage == "mod_analysis" and (outcome.status == "completed" or outcome.error_code == "relevant_skill_gap"):
                self._ingest_analysis(app, outcome.outputs)
            app["history"].append({"stage": stage, "execution_id": execution_id,
                "state": outcome.status, "error_code": outcome.error_code})
            local = json_copy(app)
            local["rework_context"] = app.get("gap_rework")
            local["active_group"] = None
            local["active_stage"] = None
            if downstream_toolcall(header) and (outcome.status != "completed"
                    or (stage in REVIEW_STAGES and outcome.outputs.get("verdict") != "approved")):
                operations += self._repair_failure(snapshot, header, app, stage, outcome,
                    execution_id, location="gap")
                continue
            if outcome.status == "completed":
                if stage == "gap_research":
                    produced = self._schedule(snapshot, header, local, "research_review", dependencies=[stage],
                        causation_id=execution_id, activate=False)
                elif stage == "research_review":
                    if outcome.outputs.get("verdict") == "approved":
                        self._apply_gap_resolutions(local, outcome.outputs.get("approved_gap_resolutions", []), execution_id)
                        self._ingest_reviewed_requirements(local, outcome, execution_id,
                            normalize_duplicates=downstream_toolcall(header))
                        generic = outcome.outputs.get("approved_generic_knowledge_entries", {})
                        produced = ([] if downstream_toolcall(header) and not generic else
                            self._schedule(snapshot, header, local, "knowledge_publish" if generic else "mod_analysis",
                            dependencies=[stage], payload={"generic_knowledge_entries": generic},
                            causation_id=execution_id, activate=False))
                    else:
                        app["gap_failure"] = "research_review_rejected"
                        produced = []
                elif stage == "knowledge_publish":
                    local["knowledge_revisions"].update(outcome.outputs.get("knowledge_revisions", {}))
                    produced = ([] if downstream_toolcall(header) else
                        self._schedule(snapshot, header, local, "mod_scan", dependencies=[stage],
                        causation_id=execution_id, activate=False))
                elif stage == "mod_scan":
                    produced = self._schedule(snapshot, header, local, "mod_analysis", dependencies=[stage],
                        causation_id=execution_id, activate=False)
                else:
                    app["knowledge_gap_context"] = None
                    app["gap_rework"] = None
                    app["gap_failure"] = None
                    produced = []
            else:
                produced = self._repair_failure(snapshot, header, local, stage, outcome, execution_id)
                app["gap_rework"] = local["rework_context"]
                app["knowledge_gap_context"] = local["knowledge_gap_context"]
            for key in ("agent_assignments", "rounds", "rework_evidence", "research_budget", "research_attempts", "project_research_gaps", "project_verification_gaps", "knowledge_revisions"):
                local.setdefault(key, app.get(key, {}))
                app[key] = local[key]
            app["approved_gap_resolutions"] = local.get("approved_gap_resolutions", app.get("approved_gap_resolutions", []))
            if local.get("gap_failure") == "research_budget_exhausted":
                app["gap_failure"] = local["gap_failure"]
            if any(op["kind"] == "finish" for op in produced):
                app["gap_failure"] = local["terminal_reason"]
                if "budget" in (app["gap_failure"] or ""):
                    app["stop_reason"], app["stop_state"] = app["gap_failure"], "failed"
            else:
                operations += produced
                app["gap_pending"].extend(op["task_id"] for op in produced if op["kind"] == "dispatch")
        return operations

    @staticmethod
    def _charge_rework(header, app, family):
        if (not progress_supervised(header)
                and app["rounds"].get(family, 0) >= header["request"]["budget"]["max_rework_rounds"]):
            return False
        app["rounds"][family] = app["rounds"].get(family, 0) + 1
        return True

    @staticmethod
    def _preserve_repair_results(app, results):
        for result in results:
            execution_id = result["command_id"]
            for key, ref in result.get("outputs", {}).get("artifact_refs", {}).items():
                aliases = app.setdefault("rework_evidence", {})
                alias = f"rework_evidence:{execution_id}:{key}"
                if not aliases.get(alias, {}).get("path", "").startswith("artifacts/repair-evidence/"):
                    aliases[alias] = json_copy(ref)

    @staticmethod
    def _without_repair_context(value):
        if isinstance(value, list):
            return [MigrationOperations._without_repair_context(item) for item in value]
        if isinstance(value, dict):
            return {key: MigrationOperations._without_repair_context(item)
                    for key, item in value.items()
                    if key not in {"repair_context", "repair_history", "prior_attempts", "repair_feedback"}}
        return value

    @staticmethod
    def _repair_cycle_results(snapshot, app, context):
        # Read settled result values only, never command payloads (which carry
        # prior_attempts). SDK attempts retain results overwritten in effective.
        ids = set()
        in_cycle = False
        for row in app["history"]:
            if in_cycle:
                ids.add(row["execution_id"])
            if row["execution_id"] == context["failure_execution_id"]:
                in_cycle = True
        results = {}
        for task in snapshot["tasks"].values():
            for attempt in task["attempts"]:
                value = (attempt.get("result") or {}).get("value")
                if isinstance(value, dict) and value.get("command_id") in ids:
                    results[value["command_id"]] = json_copy(value)
        results.update(json_copy(app.get("repair_cycle_results", {})))
        return MigrationOperations._without_repair_context(results)

    def _capture_repair_result(self, header, app, result):
        if app.get("repair_context"):
            preserved = snapshot_repair_evidence(header["run_dir"], self._without_repair_context(result))
            app.setdefault("repair_cycle_results", {})[result["command_id"]] = preserved
            self._preserve_repair_results(app, [preserved])

    def _record_repair_feedback(self, app):
        context = app.get("repair_context")
        if not context:
            return
        feedback = json_copy(app["rework_context"])
        execution_id = feedback["execution_id"]
        # Settlement already captured these bytes before any subsequent worker
        # can reuse the paths. Keep this feedback separate from the failure.
        feedback["result"] = json_copy(app["repair_cycle_results"][execution_id])
        feedback["findings"] = feedback["result"].get("outputs", {}).get("prior_findings") or feedback["findings"]
        rows = app.setdefault("repair_feedback", [])
        if not any(row["execution_id"] == execution_id for row in rows):
            rows.append(feedback)
        self._preserve_repair_results(app, [feedback["result"]])

    def _start_repair_context(self, snapshot, header, app, scope):
        previous = app.get("repair_context")
        history = app.setdefault("repair_history", [])
        if previous and not any(row["failure_execution_id"] == previous["failure_execution_id"]
                                for row in history):
            results = self._repair_cycle_results(snapshot, app, previous)
            history.append(json_copy({
                "run_id": previous["run_id"],
                "failure_execution_id": previous["failure_execution_id"],
                "repair_generation": previous["repair_generation"],
                "repair_scope": previous["repair_scope"],
                "current_failure": previous["current_failure"],
                "stage_results": results,
            }))
            self._preserve_repair_results(app, results.values())
        frozen_results = snapshot_repair_evidence(header["run_dir"], self._without_repair_context({
            key: app.get("repair_cycle_results", {}).get(result["command_id"], result)
            for key, result in app["effective"].items()}))
        self._preserve_repair_results(app, frozen_results.values())
        failure_execution_id = app["rework_context"]["execution_id"]
        failure_input = next((self._without_repair_context(attempt["command"].get("payload"))
            for task in snapshot["tasks"].values() for attempt in task["attempts"]
            if attempt["command"].get("execution_id") == failure_execution_id), None)
        app["repair_context"] = snapshot_repair_evidence(header["run_dir"], {
            "schema_version": 1, "run_id": header.get("logical_run_id", snapshot["run_id"]),
            "failure_execution_id": app["rework_context"]["execution_id"],
            "repair_generation": app["repair_generation"], "repair_scope": scope,
            "request": header["request"], "current_failure": app["rework_context"],
            "prior_findings": header["prior_findings"], "prior_attempts": history,
            "upstream_results": frozen_results,
            "artifact_refs": self._refs(header, {**app, "effective": frozen_results}),
            "parent_run_id": header.get("parent_run_id"),
            "parent_context": self._parent_repair_context(header),
            "stage_history": json_copy(app["history"]),
            "failure_input": failure_input,
        })
        if header.get('definition', {}).get('workflow_version', 0) >= 12:
            from .planning_references import reference_history
            app['repair_context'] = reference_history(header['run_dir'], app['repair_context'])
        # Include frozen initial/admin refs in retry copying as well as outputs.
        self._preserve_repair_results(app, [{
            "command_id": app["rework_context"]["execution_id"],
            "outputs": {"artifact_refs": {
                key: ref for key, ref in app["repair_context"]["artifact_refs"].items()
                if not key.startswith("rework_evidence:")}},
        }])
        app["repair_cycle_results"] = {}
        app["repair_feedback"] = []

    @staticmethod
    def _parent_repair_context(header):
        if not header.get("parent_run_id"):
            return None
        try:
            packet_ref = header["initial_refs"]["failure_packet"]
            packet = read_json(verified_path(Path(header["run_dir"]), packet_ref))
            if not isinstance(packet, dict):
                raise ValueError("parent failure packet must be an object")
            source_refs = packet["source_refs"]
            copied = packet["copied_evidence"]
            if not isinstance(source_refs, dict) or not isinstance(copied, dict):
                raise ValueError("parent evidence mappings must be objects")
            def identity(ref):
                return (str(Path(ref["path"])), ref.get("sha256"))
            mapping = {identity(ref): copied[key] for key, ref in source_refs.items()}

            def remap(value):
                if isinstance(value, list):
                    return [remap(item) for item in value]
                if not isinstance(value, dict):
                    return value
                if is_repair_artifact_ref(value):
                    if identity(value) not in mapping:
                        raise ValueError("parent context reference has no copied evidence mapping")
                    target = mapping[identity(value)]
                    return {**json_copy(value), "path": target["path"],
                            **({"sha256": target["sha256"]} if "sha256" in target else {})}
                return {key: remap(item) for key, item in value.items()}

            context = remap({key: packet.get(key) for key in (
                "parent_run_id", "request", "findings", "repair_context",
                "repair_history", "repair_cycle_results", "stage_history", "stage_results")})
            # Historical JSON bytes stay untouched. Consumers can resolve the
            # parent-relative paths embedded inside them through this map.
            context["evidence_path_map"] = {
                ref["path"]: copied[key]["path"] for key, ref in source_refs.items()}
            if header.get('definition', {}).get('workflow_version', 0) >= 12:
                from .planning_references import archive_context
                return archive_context(header['run_dir'], context)
            return context
        except (OSError, ValueError, KeyError, TypeError) as error:
            raise RepairEvidenceError("parent repair context: " + str(error)) from error

    @staticmethod
    def _rework_context(app, stage, outcome, execution_id):
        # Local replanning may retain an upstream document from an earlier
        # repair. Keep its evidence addressable after replacing current context.
        for key, ref in outcome.outputs.get("artifact_refs", {}).items():
            app.setdefault("rework_evidence", {})[f"rework_evidence:{execution_id}:{key}"] = json_copy(ref)
        app["rework_context"] = {
            "stage": stage, "execution_id": execution_id, "result": outcome.to_dict(),
            "findings": outcome.outputs.get("prior_findings", []) or [
                {"stage": stage, "detail": outcome.detail, "error_code": outcome.error_code}],
        }

    @staticmethod
    def _invalidate_from(app, start):
        invalid_stages = MAIN_STAGES[MAIN_STAGES.index(start):]
        for invalid in invalid_stages:
            app["effective"].pop(invalid, None)
        # v30 cleanup precedes target_build, but every target repair/rebuild
        # must run cleanup again on the revised integrated candidate.
        if start == "target_build":
            app["effective"].pop("code_cleanup", None)
        for key in list(app["effective"]):
            if (key.startswith(("coder.", "goal."))
                    or app["effective"][key].get("stage_id") in invalid_stages):
                app["effective"].pop(key)

    def _invalidate_planning(self, app, start):
        if "development_prepare" in app["effective"]:
            # Shared preparation changed the candidate. Start a new inventory to
            # bind the new source instead of silently retaining an old snapshot.
            start = "migration_inventory"
        self._invalidate_from(app, start)
        app["effective"].pop("development_prepare", None)
        app["format_context"] = None
        for key in PLANNING_STAGES[PLANNING_STAGES.index(start):]:
            app["format_retries"].pop(key, None)
        if start == "migration_inventory":
            app["planning_generation"] += 1
            app.setdefault("plan_documents", {}).pop("migration", None)
        return start

    def _repair_failure(self, snapshot, header, app, stage, outcome, execution_id, *, location="main"):
        if downstream_toolcall(header):
            # Failure/rejection is diagnostic data. Only the downstream MCP
            # request ledger may authorize another author assignment.
            return self._diagnostic_handoff(snapshot, header, app, stage, outcome,
                                            execution_id, location=location)
        route = REPAIR_ROUTE.get(stage)
        if header.get("definition", {}).get("workflow_version", 0) >= 15:
            if stage in {"coder", "goal_prepare", "development_prepare",
                         "development_prepare_integrate", "development_integrate"}:
                # These failures occur before the accepted DAG has been fully
                # integrated.  Starting target repair here would discard peer
                # patches and skip unfinished tasks. Normal check failures are
                # already corrected inside the same native coder goal; a
                # terminal session/isolation/integration failure has no safe
                # automatic task continuation yet.
                reason = ("scoped_coder_repair_required"
                          if stage in {"coder", "goal_prepare"}
                          else "scoped_integration_repair_required")
                return self._finish(app, reason)
            elif stage == "implementation":
                # This deterministic handoff failed before any coder task
                # existed, so there is no safe local task to repair.
                return self._finish(app, "development_dispatch_contract_invalid")
        if stage in {"platform_diff", "java_diff"}:
            if remaining(app, stage.split("_", 1)[0]):
                return self._schedule(snapshot, header, app, stage, causation_id=execution_id)
            app["gap_failure"] = "research_budget_exhausted"
            return self._finish(app, "research_budget_exhausted")
        if stage == "mod_analysis":
            if outcome.error_code == "relevant_skill_gap" and outcome.outputs.get("research_repairable") is True:
                route = "gap_research"
            elif not (outcome.error_code == "analysis_output_invalid"
                      and outcome.outputs.get("format_repairable") is True):
                route = None
        if (route is None or outcome.error_code in FATAL_ERRORS or outcome.status == "blocked"):
            return self._finish(app, outcome.error_code or f"{stage}_failed")
        call_limit = header["request"]["budget"]["max_agent_assignments"]
        if call_limit is not None and app["agent_assignments"] >= call_limit:
            return self._finish(app, "agent_assignment_budget_exhausted")

        if outcome.error_code == "planning_handoff_conflict":
            if header.get("definition", {}).get("workflow_version", 0) >= 15:
                return self._finish(app, "fixed_planning_handoff_conflict")
            routes = {"migration_tasks": "migration_plan",
                      "contract_repair_tasks": "contract_repair_plan",
                      "target_repair_tasks": "target_repair_plan"}
            route = routes.get(stage)
            if route is None or outcome.outputs.get("replan_stage") != route:
                return self._finish(app, "planning_handoff_decision_invalid")
            scope = stage.split("_", 1)[0]
            family = "migration_plan" if scope == "migration" else scope + "_revise"
            if not self._charge_rework(header, app, family):
                return self._finish(app, family + "_rounds_exhausted")
            # Keep semantic feedback separate from the failure that opened this
            # repair cycle, and freeze it before subsequent planners reuse paths.
            preserved = snapshot_repair_evidence(header["run_dir"],
                self._without_repair_context(outcome.to_dict()))
            self._rework_context(app, stage, OperationResult.from_dict(preserved), execution_id)
            self._record_repair_feedback(app)
            if scope == "migration":
                route = self._invalidate_planning(app, route)
            else:
                chain = [scope + suffix for suffix in ("_diagnose", "_repair_plan",
                    "_repair_tasks", "_repair_review", "_revise", "_repair_integrate")]
                for invalid in chain[chain.index(route):]:
                    app["effective"].pop(invalid, None)
                    app["format_retries"].pop(invalid, None)
                app["format_context"] = None
                self._invalidate_from(app, "contract_verify" if scope == "contract" else "target_build")
            return self._schedule(snapshot, header, app, route, causation_id=execution_id)

        # A malformed planning output retries this conversation only. The original
        # build/test failure remains available for every conversation in a repair cycle.
        if stage in (*PLANNING_STAGES, *REPAIR_PLANNING_STAGES):
            retries = app["format_retries"].get(stage, 0)
            maximum = header["definition"].get("repair_policy", REPAIR_POLICY)["format_retries_per_stage"]
            if retries >= maximum:
                return self._finish(app, f"{stage}_format_retries_exhausted")
            app["format_retries"][stage] = retries + 1
            app["format_context"] = {"stage": stage, "execution_id": execution_id,
                                     "result": outcome.to_dict()}
            if stage in PLANNING_STAGES:
                self._invalidate_from(app, stage)
            else:
                scope = stage.split("_", 1)[0]
                chain = [scope + suffix for suffix in ("_diagnose", "_repair_plan", "_repair_tasks", "_repair_review", "_revise", "_repair_integrate")]
                for invalid in chain[chain.index(stage):]:
                    app["effective"].pop(invalid, None)
            return self._schedule(snapshot, header, app, stage, causation_id=execution_id)

        self._rework_context(app, stage, outcome, execution_id)
        if stage == "mod_analysis" and route == "gap_research":
            app["knowledge_gap_context"] = app["rework_context"]

        diagnostic = outcome.outputs.get("diagnostics")
        if isinstance(diagnostic, dict) and route in {"contract_diagnose", "target_diagnose"}:
            from .diagnostics import compare_characterization_progress
            if diagnostic.get("repair_allowed") is False:
                return self._finish(app, diagnostic.get("error_code") or outcome.error_code or "characterization_blocked")
            scope = route.split("_", 1)[0]
            previous = app["diagnostic_history"].get(scope)
            progress = compare_characterization_progress(previous, diagnostic) if previous else {}
            stalled = bool(progress.get("stalled"))
            count = app["stagnant_failures"].get(scope, 1) + 1 if stalled else 1
            app["stagnant_failures"][scope] = count
            app["diagnostic_history"][scope] = diagnostic
            policy = header["definition"].get("repair_policy", REPAIR_POLICY)
            app["diagnosis_escalated"] = count >= policy["stagnant_failures_before_diagnosis"]
            app["rework_context"]["progress"] = progress
            if count >= policy["stagnant_failures_before_stop"]:
                return self._finish(app, "characterization_stalled")
        else:
            app["diagnosis_escalated"] = False

        if route == "gap_research":
            return self._schedule(snapshot, header, app, route, causation_id=execution_id)
        family = {"contract_diagnose": "contract_revise", "target_diagnose": "target_revise"}.get(route, route)
        if route in PLANNING_STAGES:
            family = "migration_plan"
        if not self._charge_rework(header, app, family):
            return self._finish(app, f"{family}_rounds_exhausted")
        if route in {"contract_diagnose", "target_diagnose"}:
            scope = route.split("_", 1)[0]
            app["repair_generation"] += 1
            app["repair_scope"] = scope
            self._start_repair_context(snapshot, header, app, scope)
            app.setdefault("plan_documents", {}).pop(scope, None)
            app["format_context"] = None
            for invalid in REPAIR_PLANNING_STAGES:
                app["effective"].pop(invalid, None)
                app["format_retries"].pop(invalid, None)
            self._invalidate_from(app, "contract_verify" if scope == "contract" else "target_build")
            if scope == "contract":
                app["effective"].pop("development_prepare", None)
        elif route in PLANNING_STAGES:
            app["repair_generation"] += 1
            app["repair_scope"] = "migration"
            self._start_repair_context(snapshot, header, app, "migration")
            app.setdefault("plan_documents", {}).pop("migration", None)
            route = self._invalidate_planning(app, "migration_inventory")
        return self._schedule(snapshot, header, app, route, causation_id=execution_id)

    def _group_decision(self, snapshot, header, app):
        """Join business successes, not merely terminal SDK dependencies."""
        group = app["active_group"]
        if group["kind"] == "regression":
            from .regression import regression_decision
            return regression_decision(self, snapshot, header, app)
        operations = []
        active = []
        for task_id in group["members"]:
            task = snapshot["tasks"].get(task_id)
            if task is None:
                continue
            attempt = task["attempts"][-1]
            execution_id = attempt["command"]["execution_id"]
            if attempt["state"] not in EXECUTION_TERMINAL:
                active.append((task_id, execution_id))
                continue
            if execution_id in app["processed"]:
                continue
            app["processed"].append(execution_id)
            operation = OperationInput.from_dict(attempt["command"]["payload"])
            if attempt["state"] == "succeeded":
                outcome = OperationResult.from_dict(attempt["result"]["value"])
                outcome.validate_for(operation)
            else:
                outcome = OperationResult("blocked", run_id=operation.run_id, task_id=task_id,
                    stage_id=operation.stage_id, command_id=execution_id,
                    error_code="execution_" + attempt["state"], detail="group execution did not succeed")
            result = outcome.to_dict()
            self._capture_repair_result(header, app, result)
            app["history"].append({"stage": operation.stage_id, "task_id": task_id,
                "execution_id": execution_id, "state": outcome.status, "error_code": outcome.error_code})
            app["effective"][task_id] = result
            group["results"][task_id] = result
            replanning = attempt["state"] == "cancelled" and task_id in group.get("replan_cancelled", [])
            if not replanning and (outcome.status != "completed" or
                    (operation.stage_id in REVIEW_STAGES and outcome.outputs.get("verdict") != "approved")):
                group.setdefault("failure", result)
        if downstream_toolcall(header):
            # A tool may repair one of several already-settled failures. Rebuild
            # the diagnostic from retained peer results instead of losing them.
            failures = [row for row in group["results"].values() if not passed(row)]
            if failures:
                group["failure"] = failures[0]
        if group.get("failure"):
            if downstream_toolcall(header):
                # Let independent peers finish. A diagnostic does not cancel
                # their work or restart the accepted task graph.
                if active:
                    return operations
                failure = OperationResult.from_dict(group["failure"])
                return operations + self._repair_failure(snapshot, header, app,
                    failure.stage_id, failure, failure.command_id, location="group")
            # Cancel siblings and wait for them to settle before replanning or finishing.
            for task_id, execution_id in active:
                if execution_id not in app["cancel_sent"]:
                    operations.append({"kind": "cancel", "task_id": task_id, "reason": "group_member_failed"})
                    app["cancel_sent"].append(execution_id)
            if active:
                return operations
            failure = OperationResult.from_dict(group["failure"])
            app["active_group"] = None
            if group["kind"] == "skills":
                return self._finish(app, failure.error_code or "skill_review_rejected")
            if header.get("definition", {}).get("workflow_version", 0) >= 15:
                # Preserve the whole unfinished DAG, including accepted sibling
                # patches. All scopes stop here before legacy scope rewriting.
                app["failed_development_group"] = json_copy(group)
                return self._finish(app, "scoped_coder_repair_required")
            route_stage = (group.get("goal_scope", "migration") + "_revise"
                           if group.get("goal_scope", "migration") != "migration" else "coder")
            repairs = self._repair_failure(snapshot, header, app, route_stage, failure, failure.command_id)
            if app.get("early_active"):
                app["active_stage"] = None
                app.setdefault("early_pending", []).extend(
                    op["task_id"] for op in repairs if op["kind"] == "dispatch")
            return repairs
        if group["kind"] == "skills":
            if group["phase"] == "pipeline":
                for kind in group["missing_kinds"]:
                    generated, review = kind + "_diff", kind + "_skill_review"
                    if generated in group["results"] and review not in group["members"]:
                        group["members"].append(review)
                        operations += self._schedule(snapshot, header, app, review,
                            activate=False, dependencies=[generated])
                reviews = [kind + "_skill_review" for kind in group["kinds"]]
                if all(review in group["results"] for review in reviews):
                    app["active_group"] = None
                    return operations + self._schedule(snapshot, header, app, "skill_publish", dependencies=reviews)
                return operations
            if active or len(group["results"]) < len(group["members"]):
                return operations
            if group["phase"] == "generation":
                group.update(phase="review", members=[kind + "_skill_review" for kind in group["kinds"]], results={})
                for member in group["members"]:
                    operations += self._schedule(snapshot, header, app, member, activate=False)
                return operations
            app["active_group"] = None
            return self._schedule(snapshot, header, app, "skill_publish", dependencies=group["members"])

        if group.get("pending_task_updates") and downstream_toolcall(header):
            group["diagnostic_task_updates"] = group.pop("pending_task_updates")
        if group.get("pending_task_updates"):
            from .gap_planning import reconcile_task_updates
            prior_results = {task["id"]: group["results"][f"coder.g{group['generation']}.{task['id']}"]
                             for task in group["tasks"] if f"coder.g{group['generation']}.{task['id']}" in group["results"]}
            merged, invalidated, preserved = reconcile_task_updates(group["tasks"], group["pending_task_updates"],
                prior_results, group.get("update_seeds", []))
            # Changed implementation contracts require fresh four-round approval.
            # Keep old deltas as evidence; never reopen a stale coder checkout.
            invalidated_active = active if invalidated else []
            group["replan_cancelled"] = [task_id for task_id, _ in invalidated_active]
            for task_id, execution_id in invalidated_active:
                if execution_id not in app["cancel_sent"]:
                    operations.append({"kind": "cancel", "task_id": task_id, "reason": "local_gap_replan"})
                    app["cancel_sent"].append(execution_id)
            if invalidated_active:
                return operations
            if invalidated:
                evidence = {f"previous_goal:{task_id}:{alias}": ref
                            for task_id, result in group["results"].items()
                            for alias, ref in result.get("outputs", {}).get("artifact_refs", {}).items()}
                failure = OperationResult("failed", header.get("logical_run_id", snapshot["run_id"]),
                    "gap-task-update", "coder", snapshot["run_id"] + ":gap-task-update:" + str(group["generation"]),
                    outputs={"artifact_refs": evidence, "approved_task_updates": group["pending_task_updates"],
                             "preserved_task_ids": sorted(preserved)},
                    error_code="development_contract_changed", detail="Reviewed gap resolution changed task contracts; fresh planning required")
                app["active_group"] = None
                return self._repair_failure(snapshot, header, app, "coder", failure, failure.command_id)
            for name in invalidated:
                task_id = f"coder.g{group['generation']}.{name}"
                group["results"].pop(task_id, None)
                app["effective"].pop(task_id, None)
                if name in group["scheduled"]:
                    group["scheduled"].remove(name)
                if task_id in group["members"]:
                    group["members"].remove(task_id)
                goal_id = f"goal.g{group['generation']}.{name}"
                group["results"].pop(goal_id, None)
                app["effective"].pop(goal_id, None)
                if name in group.get("goal_scheduled", []):
                    group["goal_scheduled"].remove(name)
                if goal_id in group["members"]:
                    group["members"].remove(goal_id)
            group["scheduling_updates"] = {task["id"]: task.get("blocked_by_gaps", []) for task in merged}
            group.pop("pending_task_updates", None)
            group.pop("replan_cancelled", None)
            group["preserved_results"] = sorted(preserved)
        tasks = {task["id"]: task for task in group["tasks"]}
        generation = group["generation"]
        # Migration knowledge cannot hold baseline characterization hostage.
        group_gaps = ([] if group.get("goal_scope") == "contract" or downstream_toolcall(header)
                      else self._knowledge_gaps(app))
        if group.get("execution_payload", {}).get("development_kind") == "preparation":
            relevant = {gap for task in group["tasks"] for gap in task.get("blocked_by_gaps", [])}
            group_gaps = [gap for gap in group_gaps if gap["gap_id"] in relevant
                          or set(gap.get("affected_tasks", [])) & tasks.keys()]
        def identifier(name):
            return f"coder.g{generation}.{name}"
        completed = {name for name in tasks if identifier(name) in group["results"]}
        if len(completed) == len(tasks):
            app.get("memory_waits", {}).pop("development", None)
            if group_gaps:
                if not app.get("gap_pending"):
                    planned = self._maybe_gap_plan(snapshot, header, app)
                    return operations + planned + ([] if planned else self._park_administrator(snapshot, header, app))
                return operations
            # Keep the original plan order; the integration handler validates/reorders its DAG.
            results = [group["results"][identifier(task["id"])] for task in group["tasks"]]
            app["active_group"] = None
            scope = group.get("goal_scope", "migration")
            stage = "development_integrate" if scope == "migration" else scope + "_repair_integrate"
            if group.get("execution_payload", {}).get("development_kind") == "preparation":
                stage = "development_prepare_integrate"
            scheduled = self._schedule(snapshot, header, app, stage,
                dependencies=group["members"], artifact_overrides=group.get("artifact_refs"),
                payload={**group.get("execution_payload", {}), "goal_scope": scope,
                    "development_results": results, "development_generation": generation,
                    "development_base": group["base"]})
            if app.get("early_active"):
                app["active_stage"] = None
                app.setdefault("early_pending", []).extend(
                    op["task_id"] for op in scheduled if op["kind"] == "dispatch")
            return scheduled
        capacity = self._memory_capacity(
            snapshot, header, app, "development", requested_stage="goal_prepare")
        unresolved = {gap["gap_id"] for gap in group_gaps}
        blockers = {task["id"]: set(group.get("scheduling_updates", {}).get(
                    task["id"], task.get("blocked_by_gaps", []))) for task in group["tasks"]}
        # New gaps that identify concrete tasks must not block unrelated work.
        for gap in group_gaps:
            for name in gap.get("affected_tasks", []):
                if name in tasks:
                    blockers[name].add(gap["gap_id"])
        mapped = {gap for values in blockers.values() for gap in values}
        # Reassessment can discover another gap after the task plan was made.
        # Do not assume that an unassigned gap affects no one. Existing coders
        # may settle, but new work needs that knowledge before it can start.
        unmapped = unresolved - mapped
        group["unmapped_gap_ids"] = sorted(unmapped)
        group["task_states"] = {
            name: ("accepted" if name in completed else "coding" if name in group["scheduled"]
                   else "waiting_knowledge" if unmapped or unresolved.intersection(blockers[name])
                   else "waiting_dependencies" if not set(tasks[name]["dependencies"]) <= completed
                   else "goal_ready" if f"goal.g{generation}.{name}" in group["results"]
                   else "preparing_goal" if name in group.get("goal_scheduled", []) else "waiting_goal")
            for name in tasks}
        if "development" in app.get("memory_waits", {}):
            for name, state in group["task_states"].items():
                if state in {"waiting_goal", "goal_ready"}:
                    group["task_states"][name] = "waiting_memory"
        # Goal preparation is the independent fifth round. It may run before
        # implementation dependencies settle, within the same concurrency cap.
        goal_dispatches = 0
        for task in group["tasks"]:
            if capacity <= 0 or app["stop_reason"]:
                break
            name = task["id"]
            if name in group.setdefault("goal_scheduled", []):
                continue
            goal_id = f"goal.g{generation}.{name}"
            dispatched = self._schedule(snapshot, header, app, "goal_prepare", task_id=goal_id,
                activate=False, dependencies=[group.get("entry_stage", "implementation")],
                artifact_overrides=group.get("artifact_refs"),
                payload={"development_task": task, "planning_context": group.get("planning_context", {}),
                    "goal_scope": group.get("goal_scope", "migration"), "goal_generation": generation})
            operations += dispatched
            if any(op["kind"] == "dispatch" for op in dispatched):
                group["goal_scheduled"].append(name)
                group["members"].append(goal_id)
                group["task_states"][name] = "preparing_goal"
                capacity -= 1
                goal_dispatches += 1
        goal_policy = self.memory_policy.for_stage("goal_prepare")
        capacity = self._memory_capacity(
            snapshot, header, app, "development", requested_stage="coder",
            additional_active=goal_dispatches,
            additional_reservation_bytes=(0 if goal_policy is None else
                                          goal_dispatches * goal_policy.heavy_slot_bytes))
        for task in group["tasks"]:
            name = task["id"]
            if capacity <= 0 or app["stop_reason"]:
                break
            if name in group["scheduled"] or not set(task["dependencies"]).issubset(completed):
                continue
            goal_id = f"goal.g{generation}.{name}"
            goal_result = group["results"].get(goal_id)
            if goal_result is None:
                continue
            goal_ref = goal_result.get("outputs", {}).get("artifact_refs", {}).get("coder_goal")
            if not isinstance(goal_ref, dict):
                group["failure"] = {**goal_result, "status": "blocked",
                    "error_code": "goal_artifact_missing", "detail": "goal preparation returned no authenticated goal"}
                return operations
            if unmapped or unresolved.intersection(blockers[name]):
                continue
            ancestors = set()
            def visit(dependency):
                if dependency in ancestors:
                    return
                ancestors.add(dependency)
                for parent in tasks[dependency]["dependencies"]:
                    visit(parent)
            for dependency in task["dependencies"]:
                visit(dependency)
            ancestors = [item["id"] for item in group["tasks"] if item["id"] in ancestors]
            patches = [group["results"][identifier(dep)]["outputs"]["artifact_refs"]["coder_patch"]
                       for dep in ancestors]
            task_id = identifier(name)
            operations += self._schedule(snapshot, header, app, "coder", task_id=task_id, activate=False,
                dependencies=[goal_id, *[identifier(dep) for dep in task["dependencies"]]],
                artifact_overrides={**group.get("artifact_refs", {}), "coder_goal": goal_ref},
                payload={**group.get("execution_payload", {}),
                    "goal_scope": group.get("goal_scope", "migration"), "goal_generation": generation,
                    "planning_context": group.get("planning_context", {}),
                    "development_task": task, "development_base": group["base"],
                    "development_generation": generation, "dependency_patches": patches},
                extra_options={"workspace": f"workspaces/development/g{generation}/{name}",
                    "model": task["model"], "reasoning_effort": task["reasoning_effort"]})
            group["scheduled"].append(name)
            group["members"].append(task_id)
            group["task_states"][name] = "coding"
            capacity -= 1
        if unresolved and not app.get("gap_pending"):
            planned = self._maybe_gap_plan(snapshot, header, app)
            operations += planned
            if not active and not operations:
                operations += self._park_administrator(snapshot, header, app)
        return operations

    @staticmethod
    def _audited_observation(sdk, header, *, through=None):
        """Audit one public SDK event at a time, then return its ACK position.

        A page of 100 events can contain gigabytes of repeated state. Keep
        the original snapshot/watermark, stream immutable events through that
        watermark, and retain only the last durably recorded sequence.
        """
        observed = sdk.observe(header["run_id"], subscription=SUBSCRIPTION, limit=1)
        target = observed["event_high_watermark"]
        if through is not None:
            target = min(target, through)
        page = observed.pop("events")
        advance_to = observed["cursor"]

        def events():
            nonlocal page, advance_to
            for _ in range(100):
                if not page or page[0]["sequence"] > target:
                    return
                event = page[0]
                sequence = event["sequence"]
                yield event
                # The strict recorder must have persisted this event before
                # requesting the next one. A write error prevents any ACK.
                advance_to = sequence
                del event
                page.clear()
                if advance_to >= target:
                    return
                page = sdk.read_events(header["run_id"], after=advance_to, limit=1)

        record_sdk_events(header["run_dir"], events())
        observed["audit_advance_to"] = advance_to
        return observed

    def tick(self, sdk, header, *, stop_reason=None, stop_state="cancelled"):
        root = Path(header["run_dir"])
        check_storage_budget(root, phase="tick")
        observed = self._audited_observation(sdk, header)
        stored_snapshot = observed["snapshot"]
        snapshot = hydrate_run_snapshot(root, stored_snapshot)
        from .interrupted_supervision import settle as settle_interrupted_supervisor
        if settle_interrupted_supervisor(root, header, sdk, snapshot):
            observed = self._audited_observation(sdk, header)
            stored_snapshot = observed['snapshot']
            snapshot = hydrate_run_snapshot(root, stored_snapshot)
        from .watchdog_settlement import settle as settle_watchdog_effects
        if settle_watchdog_effects(root, header, sdk, snapshot):
            observed = self._audited_observation(sdk, header)
            stored_snapshot = observed['snapshot']
            snapshot = hydrate_run_snapshot(root, stored_snapshot)
        if self._settle_progress_cancelled_effects(root, header, sdk, snapshot=snapshot):
            observed = self._audited_observation(sdk, header)
            stored_snapshot = observed['snapshot']
            snapshot = hydrate_run_snapshot(root, stored_snapshot)
        if snapshot["state"] in TERMINAL:
            self._project_gap_state(header, snapshot)
            self._drain_terminal_events(sdk, header, observed)
            return snapshot
        operations, app = self._decision(snapshot, header, sdk=sdk,
            stop_reason=stop_reason, stop_state=stop_state)
        def expired_with_pending_dispatch(operations, app):
            deadline = self._effective_deadline(header, app)
            return (deadline is not None and self.clock() >= deadline
                    and (not app.get("stop_reason")
                         or any(operation["kind"] in {"add_task", "new_attempt", "schedule", "dispatch"}
                                for operation in operations)))

        if expired_with_pending_dispatch(operations, app):
            # The deadline can pass while a planner result is being consumed.
            # Re-evaluate the same SDK snapshot under the original Run deadline
            # before any newly planned assignment is committed to the SDK.
            operations, app = self._decision(snapshot, header, sdk=sdk,
                stop_reason=app.get("stop_reason"),
                stop_state=app.get("stop_state") or "failed")
        advance_to = observed["audit_advance_to"]
        stored_app = pack_application_state(root, app)
        name = f"policy:{snapshot['revision']}:{observed['cursor']}:{digest([operations, stored_app])}"
        try:
            if operations or stored_app != stored_snapshot["application_state"]:
                verify_operations(root, operations)
                check_storage_budget(root, phase="apply")
                command_by_task = {}
                for operation in operations:
                    if operation.get("kind") not in {"add_task", "new_attempt", "schedule"}:
                        continue
                    command = operation.get("command")
                    if isinstance(command, dict):
                        command_by_task[operation.get("task_id")] = command
                        record_command_progress(root, command, "planned")
                if expired_with_pending_dispatch(operations, app):
                    # Validation and progress recording can outlive the deadline.
                    # Only the SDK transaction creates an executable attempt.
                    operations, app = self._decision(snapshot, header, sdk=sdk,
                        stop_reason=app.get("stop_reason"),
                        stop_state=app.get("stop_state") or "failed")
                    stored_app = pack_application_state(root, app)
                    name = f"policy:{snapshot['revision']}:{observed['cursor']}:{digest([operations, stored_app])}"
                    verify_operations(root, operations)
                    command_by_task = {}
                stored_state = sdk.apply_operations(header["run_id"], command_id=name,
                    expected_revision=snapshot["revision"], expected_generation=snapshot["generation"],
                    operations=operations, application_state=stored_app,
                    subscription=SUBSCRIPTION, expected_cursor=observed["cursor"], advance_to=advance_to)
                state = hydrate_run_snapshot(root, stored_state)
                for operation in operations:
                    if operation.get("kind") != "dispatch":
                        continue
                    task_id = operation.get("task_id")
                    command = command_by_task.get(task_id)
                    if command is None:
                        task = snapshot.get("tasks", {}).get(task_id)
                        if task and task.get("attempts"):
                            command = task["attempts"][-1].get("command")
                    if isinstance(command, dict):
                        record_command_progress(root, command, "dispatched")
                self._audit_startup_recovery_changes(root, state.get("application_state") or {})
                self._project_gap_state(header, state)
                if state["state"] in TERMINAL:
                    self._drain_terminal_events(sdk, header)
                return state
            if advance_to != observed["cursor"]:
                sdk.acknowledge_events(header["run_id"], command_id=name,
                    expected_revision=snapshot["revision"], expected_generation=snapshot["generation"],
                    subscription=SUBSCRIPTION,
                    expected_cursor=observed["cursor"], advance_to=advance_to)
        except RevisionConflict:
            return hydrate_run_snapshot(root, sdk.get_run(header["run_id"]))
        self._project_gap_state(header, snapshot)
        return snapshot

    @staticmethod
    def _drain_terminal_events(sdk, header, observed=None):
        """Persist each page before ACK, through a fixed observed watermark."""
        check_storage_budget(header["run_dir"], phase="terminal-audit")
        observed = observed or MigrationOperations._audited_observation(sdk, header)
        target = observed["event_high_watermark"]
        conflicts = 0
        while observed["cursor"] < target:
            snapshot = observed["snapshot"]
            if snapshot["state"] not in TERMINAL:
                raise RuntimeError("Run reopened while draining terminal audit events")
            advance_to = observed["audit_advance_to"]
            if advance_to <= observed["cursor"]:
                raise RuntimeError("SDK event page did not advance to the observed watermark")
            try:
                sdk.acknowledge_events(header["run_id"],
                    command_id=f"terminal-audit:{snapshot['generation']}:{snapshot['revision']}:{observed['cursor']}:{advance_to}",
                    expected_revision=snapshot["revision"], expected_generation=snapshot["generation"],
                    subscription=SUBSCRIPTION, expected_cursor=observed["cursor"], advance_to=advance_to)
            except RevisionConflict:
                conflicts += 1
                if conflicts >= 4:
                    raise
            observed = MigrationOperations._audited_observation(sdk, header, through=target)

    @staticmethod
    def _project_gap_state(header, state):
        from .project_gaps import write_gap_files
        state = hydrate_run_snapshot(Path(header["run_dir"]), state)
        from .desktop_state import publish_snapshot_safely
        publish_snapshot_safely(header, state)
        app = state.get("application_state") or {}
        write_gap_files(Path(header["run_dir"]), app, header["run_id"])
        project_rework_responses(Path(header["run_dir"]), app)
        atomic_json(Path(header["run_dir"]) / "artifacts" / "research-budget.json", {
            "schema_version": 1, "run_id": header["run_id"], "workflow_version": WORKFLOW_VERSION,
            "execution_version": header["registry_revision"], "knowledge_revisions": app.get("knowledge_revisions", {}),
            "gap_revision": app.get("gap_revision", 0), "research_budget": app.get("research_budget", {}),
            "research_attempts": app.get("research_attempts", {}),
            "execution_deadline_epoch": header["deadline_epoch"],
            "administrator_wait_seconds": app.get("administrator_wait_seconds", 0),
            "administrator_wait": app.get("administrator_wait")})

    def execute(self, run: MigrationRun, *, poll_interval=0.1) -> MigrationRun:
        self._header(Path(run.run_dir).resolve(), run.run_id)
        check_storage_budget(run.run_dir, phase="execute")
        from .runner import DriverLease
        with DriverLease(run.run_dir, run.run_id) as driver:
            try:
                return self._execute_owned(run, poll_interval=poll_interval, driver=driver)
            finally:
                if self._dynamic_memory_demand is not None:
                    self._dynamic_memory_demand.close()

    @staticmethod
    def _may_leave_waiting_run(state):
        if not any(wait["state"] == "open" for wait in state["waits"].values()):
            return False
        app = state.get("application_state") or {}
        if app.get('watchdog', {}).get('active') and not app.get('stop_reason'):
            # A diagnostic assignment must be dispatched even while an older
            # business attempt awaits Effect reconciliation.
            return False
        parkable = EXECUTION_TERMINAL | {"recovery_required"}
        if not app.get("stop_reason"):
            # Planned tasks may depend on the uncertain effect. Already
            # dispatched peers still need the live host to finish and sync.
            parkable = parkable | {"planned"}
        for task in state["tasks"].values():
            if task["attempts"][-1]["state"] not in parkable:
                return False
        return True

    def _execute_owned(self, run: MigrationRun, *, poll_interval, driver) -> MigrationRun:
        with self.session(run.run_dir, run.run_id) as (root, header, runtime, sdk):
            self._audit_action(root, run.run_id, "execute")
            # A real OpenCode host must be able to initialize before the SDK
            # dispatches work.  Fixture/in-process handler registries do not
            # need this provider probe; production always uses the default
            # registry and process isolation.
            preflight_state = hydrate_run_snapshot(root, sdk.get_run(run.run_id))
            preflight_app = preflight_state.get('application_state') or {}
            preflight_deadline = self._effective_deadline(header, preflight_app)
            from .token_budget import read_token_budget
            if (self.handlers is None and self.isolation_mode == "process"
                    and preflight_state['state'] not in TERMINAL
                    and (preflight_deadline is None or self.clock() < preflight_deadline)
                    and not read_token_budget(root)['exhausted']):
                from .handlers import preflight_opencode_host
                model, effort = agent_model_policy(header["definition"]["workflow_version"], model_policy=header.get("model_policy"))
                preflight_opencode_host(root, model=model, reasoning_effort=effort)
            runtime.reap()
            sdk.sync()
            from .interrupted_execution import reconcile_interrupted_executions
            reconcile_interrupted_executions(root, header, runtime, sdk)
            from .watchdog_events import collect_runtime_notifications
            collect_runtime_notifications(sdk, header, preflight_app)
            state = self.tick(sdk, header)
            if self.handlers is None and self.isolation_mode == 'process' and state['state'] not in TERMINAL:
                from .run_monitor import start_monitor
                start_monitor(root, run.run_id)
            if state["state"] in TERMINAL or self._may_leave_waiting_run(state):
                self._export_audit(root)
                if state["state"] in TERMINAL:
                    from .storage_lifecycle import automatic_retention
                    automatic_retention(root, sdk.get_run(run.run_id))
                return MigrationRun(run.run_id, root, state)
            from .watchdog_events import accept_notification, install_runtime_watches
            cancellation_drain_deadline = None
            with OrchestratorHost(sdk,
                    callback=lambda notice: accept_notification(root, notice),
                    worker_count=max(8, header["request"].get("max_parallel_coders", 3) + 5)) as host:
                while True:
                    driver.check_health()
                    state = self.tick(sdk, header)
                    install_runtime_watches(runtime, sdk, header, state)
                    if state["state"] in TERMINAL or self._may_leave_waiting_run(state):
                        break
                    if (any(wait["state"] == "open" for wait in state["waits"].values())
                            and (state.get("application_state") or {}).get("stop_reason")):
                        if cancellation_drain_deadline is None:
                            cancellation_drain_deadline = time.monotonic() + CANCEL_SETTLE_TIMEOUT_SECONDS
                        elif time.monotonic() >= cancellation_drain_deadline:
                            raise TimeoutError("SDK did not settle live assignments before recovery wait")
                    else:
                        cancellation_drain_deadline = None
                    host.wake(run.run_id)
                    health = host.health()
                    if health.state in {"failed", "stopped"}:
                        raise RuntimeError(f"SDK host stopped unexpectedly: {health.last_error}")
                    delay = poll_interval
                    if cancellation_drain_deadline is not None:
                        delay = min(delay, max(0.0, cancellation_drain_deadline - time.monotonic()))
                    time.sleep(delay)
            if state["state"] in TERMINAL:
                self._drain_terminal_events(sdk, header)
            else:
                self._audited_observation(sdk, header)
            self._export_audit(root)
            if state["state"] in TERMINAL:
                from .storage_lifecycle import automatic_retention
                automatic_retention(root, sdk.get_run(run.run_id))
            return MigrationRun(run.run_id, root,
                                hydrate_run_snapshot(root, sdk.get_run(run.run_id)))

    def load_retry_parent(self, run_dir, run_id):
        """Read retry authority through a validated, capacity-limited session."""
        self._header(Path(run_dir).resolve(), run_id)
        check_storage_budget(run_dir, phase="recover-retry")
        with self.session(run_dir, run_id, allow_terminal_deployment=True) as (root, _, _, sdk):
            return MigrationRun(run_id, root, hydrate_run_snapshot(root, sdk.get_run(run_id)))

    def resume(self, run_dir, run_id):
        """Resume through validated writer admission without an observation copy."""
        return self.execute(MigrationRun(run_id, Path(run_dir).resolve(), {}))

    def watchdog_recover(self, run_dir, run_id):
        from .watchdog_recovery import recover
        return recover(self, run_dir, run_id)

    def status(self, run_dir, run_id, *, detail=False, task_id=None):
        """Read bounded SDK status, with explicit full or single-task detail."""
        root = Path(run_dir).resolve()
        header = self._header(root, run_id)
        if header.get("sdk_identity", {}).get("source_version") != SDK_VERSION:
            raise ValueError("Run does not declare the supported SDK identity")
        if detail and task_id is not None:
            raise ValueError("--detail and --task-id are mutually exclusive")

        # Orchestrator construction may initialize its store. Keep that API
        # call on a disposable copy; the summary path copies only the database
        # it reads. Kernel availability is inspected separately through the
        # SDK's read-only public inspector against the original stores.
        if detail:
            with snapshot_databases(
                    (root / "kernel.sqlite3", root / "orchestrator.sqlite3"),
                    prefix="modport-status-detail-") as snapshot_root:
                require_compatible_storage(snapshot_root)
                sdk = Orchestrator(snapshot_root / "orchestrator.sqlite3", None, clock=self.clock)
                try:
                    state = hydrate_run_snapshot(root, sdk.get_run(run_id))
                finally:
                    sdk.close()
            if state["definition"] != header["definition"] or state["input"] != header:
                raise ValueError("authoritative SDK Run disagrees with the frozen input")
            for ref in header["initial_refs"].values():
                verified_path(root, ref)
            return MigrationRun(run_id, root, state)

        from .run_monitor import read_run_availability
        from .snapshot_storage import SnapshotLimitError

        # Check the live stores before creating even a temporary Orchestrator.
        require_compatible_storage(root)
        for ref in header["initial_refs"].values():
            verified_path(root, ref)
        availability = read_run_availability(root, run_id, sample_limit=20,
                                             effect_scan_limit=1000)
        summary = {}
        requested_task = None
        stage_tasks = {}
        task_scan_complete = False
        planned_tasks = []
        summary_error = None
        try:
            with snapshot_databases((root / "orchestrator.sqlite3",),
                                    prefix="modport-status-summary-") as snapshot_root:
                sdk = Orchestrator(snapshot_root / "orchestrator.sqlite3", None, clock=self.clock)
                try:
                    summary = sdk.get_run_summary(run_id)
                    requested_task = sdk.get_task(run_id, task_id) if task_id is not None else None

                    # Read only the known status-bearing task IDs. The SDK's
                    # general task list is ordered by ID, not recency, so a
                    # capped generic prefix cannot establish which result is
                    # latest. Generation-suffixed verification stages are
                    # paged by their known ID prefix with a fixed row cap.
                    known_stages = ("development_prepare_integrate", "development_integrate",
                                    "target_repair_integrate",
                                    "contract_repair_integrate", "contract_verify", "target_build",
                                    "test_execute", "acceptance_build", "client_smoke")
                    if task_id is None:
                        task_count = summary.get("task_count")
                        task_scan_complete = (type(task_count) is int and 0 <= task_count <= 128)
                        for stage in known_stages:
                            try:
                                task = sdk.get_task(run_id, stage)
                            except Exception as error:
                                if "unknown task" not in str(error).lower():
                                    raise
                                continue
                            attempt = task.get("latest_attempt") if isinstance(task, dict) else None
                            command = attempt.get("command") if isinstance(attempt, dict) else None
                            payload = command.get("payload") if isinstance(command, dict) else None
                            observed_stage = payload.get("stage_id") if isinstance(payload, dict) else None
                            if observed_stage == stage:
                                stage_tasks[stage] = task
                            else:
                                task_scan_complete = False

                        generated_task_caps = {
                            "test_execute": 64,
                            "acceptance_build": 16,
                            "client_smoke": 16,
                        }
                        page_size = 8
                        for stage, max_generated_tasks in generated_task_caps.items():
                            cursor = stage
                            selected_ids = []
                            while len(selected_ids) < max_generated_tasks:
                                page_limit = min(page_size, max_generated_tasks - len(selected_ids))
                                page = sdk.list_tasks(run_id, after_task_id=cursor,
                                                      limit=page_limit)
                                if not page:
                                    break
                                prefix = stage + ("." if stage == "test_execute" else ".g")
                                boundary = False
                                for row in page:
                                    selected_id = row.get("task_id") if isinstance(row, dict) else None
                                    if (not isinstance(selected_id, str)
                                            or not selected_id.startswith(prefix)):
                                        boundary = True
                                        break
                                    selected_ids.append(selected_id)
                                    cursor = selected_id
                                if boundary or len(page) < page_limit:
                                    break
                            if len(selected_ids) >= max_generated_tasks:
                                tail = sdk.list_tasks(run_id, after_task_id=cursor, limit=1)
                                next_id = tail[0].get("task_id") if tail and isinstance(tail[0], dict) else None
                                prefix = stage + ("." if stage == "test_execute" else ".g")
                                if isinstance(next_id, str) and next_id.startswith(prefix):
                                    task_scan_complete = False
                            for selected_id in selected_ids:
                                task = sdk.get_task(run_id, selected_id)
                                attempt = task.get("latest_attempt") if isinstance(task, dict) else None
                                command = attempt.get("command") if isinstance(attempt, dict) else None
                                payload = command.get("payload") if isinstance(command, dict) else None
                                if isinstance(payload, dict) and payload.get("stage_id") == stage:
                                    stage_tasks[selected_id] = task
                                else:
                                    task_scan_complete = False
                        # A small Run can be checked exhaustively through the
                        # public bounded task page. This also catches fresh
                        # reviewer-rework verifier IDs ending in `.verify`.
                        # Larger Runs keep useful sampled detail, but cannot
                        # claim that the selected result is globally latest.
                        if type(task_count) is int and 0 < task_count <= 128:
                            all_tasks = sdk.list_tasks(run_id, limit=task_count)
                            task_scan_complete = len(all_tasks) == task_count
                            if task_scan_complete:
                                for task in all_tasks:
                                    attempt = task.get("latest_attempt") if isinstance(task, dict) else None
                                    if (isinstance(attempt, dict)
                                            and attempt.get("state") in {"planned", "pending_dispatch", "queued"}
                                            and isinstance(task.get("task_id"), str)):
                                        planned_tasks.append(task["task_id"][:256])
                                    command = attempt.get("command") if isinstance(attempt, dict) else None
                                    payload = command.get("payload") if isinstance(command, dict) else None
                                    stage = payload.get("stage_id") if isinstance(payload, dict) else None
                                    selected_id = task.get("task_id") if isinstance(task, dict) else None
                                    if stage in known_stages and isinstance(selected_id, str):
                                        stage_tasks[selected_id] = task
                finally:
                    sdk.close()
        except SnapshotLimitError:
            # Keep useful read-only execution facts available when even the
            # bounded Orchestrator backup is too large or too slow.
            summary_error = "orchestrator_summary_snapshot_limit"
            task_scan_complete = False
        if summary and summary.get("run_id") != run_id:
            raise ValueError("authoritative SDK Run identity disagrees with the frozen input")

        def task_view(task):
            if not isinstance(task, dict):
                return {}
            attempt = task.get("latest_attempt")
            attempt = attempt if isinstance(attempt, dict) else {}
            command = attempt.get("command")
            command = command if isinstance(command, dict) else {}
            payload = command.get("payload")
            payload = payload if isinstance(payload, dict) else {}
            value = (attempt.get("result") or {}).get("value")
            value = value if isinstance(value, dict) else {}
            outputs = value.get("outputs")
            outputs = outputs if isinstance(outputs, dict) else {}
            artifact_refs = outputs.get("artifact_refs")
            artifact_refs = artifact_refs if isinstance(artifact_refs, dict) else {}
            def short(value, limit=256):
                return value[:limit] if isinstance(value, str) else None
            snapshot = attempt.get("kernel_snapshot")
            snapshot = snapshot if isinstance(snapshot, dict) else {}
            updated_at = snapshot.get("updated_at")
            if (isinstance(updated_at, bool) or not isinstance(updated_at, (int, float))
                    or not math.isfinite(float(updated_at))):
                updated_at = None
            return {
                "task_id": short(task.get("task_id"), 256),
                "stage_id": short(payload.get("stage_id"), 128),
                "execution_state": short(attempt.get("state"), 64) or "unknown",
                "business_status": short(value.get("status"), 64) or "unknown",
                "error_code": short(value.get("error_code"), 128),
                "detail": short(value.get("detail"), 500),
                "updated_at": updated_at,
                "head": short(outputs.get("head"), 128),
                "paths": [short(path, 256) for path in outputs.get("paths", [])[:20]
                          if isinstance(path, str)] if isinstance(outputs.get("paths"), list) else [],
                "artifact_refs": {
                    str(name)[:128]: {
                        "path": short(ref.get("path"), 512),
                        "sha256": short(ref.get("sha256"), 128),
                    }
                    for name, ref in list(artifact_refs.items())[:16]
                    if isinstance(name, str) and isinstance(ref, dict)
                },
            }

        sampled_tasks = [{
            "task_id": row.task_id[:256], "execution_id": row.execution_id[:128],
            "state": row.state, "application_attempt": row.application_attempt,
        } for row in availability.summaries[:8]]
        run_state = summary.get("state", availability.run_state)
        open_waits = availability.open_waits
        observed_at = self.clock()
        availability_stale = (not availability.complete
                              or availability.snapshot_consistency != "consistent")
        status_stale = (availability_stale or not summary
                        or availability.run_revision != summary.get("revision"))
        wait_samples = []
        try:
            from .execution_progress import read_execution_progress
            for execution in availability.summaries[:8]:
                if execution.state not in {"running", "leased"}:
                    continue
                marker = read_execution_progress(root, execution.execution_id)
                if (not isinstance(marker, dict) or marker.get("run_id") != run_id
                        or marker.get("task_id") != execution.task_id
                        or marker.get("execution_id") != execution.execution_id
                        or marker.get("application_attempt") != execution.application_attempt + 1):
                    continue
                wait = marker.get("wait")
                if not isinstance(wait, dict):
                    continue
                sampled_at = wait.get("sampled_at")
                if (isinstance(sampled_at, bool) or not isinstance(sampled_at, (int, float))
                        or not math.isfinite(float(sampled_at))):
                    sampled_at = marker.get("last_progress_at")
                if (isinstance(sampled_at, bool) or not isinstance(sampled_at, (int, float))
                        or not math.isfinite(float(sampled_at))):
                    continue
                fields = ("reason", "source", "stage", "waited_seconds", "required_bytes",
                          "reserved_bytes", "available_bytes", "ceiling_bytes", "dynamic_memory")
                def wait_value(key, value):
                    if isinstance(value, str):
                        return value[:256]
                    if isinstance(value, bool):
                        return value
                    if isinstance(value, (int, float)):
                        return value if math.isfinite(float(value)) else None
                    if key == "dynamic_memory" and isinstance(value, dict):
                        return {str(name)[:64]: (item[:128] if isinstance(item, str) else item)
                                for name, item in list(value.items())[:8]
                                if isinstance(name, str) and isinstance(item, (str, int, float, bool))}
                    return None
                sample = {key: wait_value(key, wait[key]) for key in fields
                          if key in wait and wait_value(key, wait[key]) is not None}
                sample.update({"task_id": execution.task_id[:256],
                               "execution_id": execution.execution_id[:128],
                               "observed_at": sampled_at,
                               "stale": max(0.0, observed_at - float(sampled_at)) > 90,
                               "source": str(wait.get("source") or "execution-progress marker")[:160]})
                raw_samples = marker.get("wait_samples")
                sample["samples"] = [
                    {key: wait_value(key, row[key]) for key in fields
                     if isinstance(row, dict) and key in row
                     and wait_value(key, row[key]) is not None}
                    for row in (raw_samples[-8:] if isinstance(raw_samples, list) else [])]
                wait_samples.append(sample)
        except (OSError, ValueError, TypeError):
            wait_samples = []
        wait_samples = wait_samples[:8]
        snapshot = {
            "run_id": run_id,
            "state": run_state,
            "waits": ({"sdk-open-waits": {"state": "open"}} if open_waits else {}),
            "application_state": {"acceptance_status": "unknown"},
            "status_summary": {
                "source": "dispatcher-sdk public summary and work-availability APIs",
                "observed_at": observed_at,
                "stale": status_stale,
                "revision": summary.get("revision", availability.run_revision),
                "generation": summary.get("generation"),
                "task_count": summary.get("task_count"),
                "summary_available": bool(summary),
                "summary_error": summary_error,
                "previous_run_id": summary.get("previous_run_id"),
                "next_run_id": summary.get("next_run_id"),
                "recovery": {
                    "id": summary.get("recovery_id"),
                    "status": summary.get("recovery_status"),
                    "target_generation": summary.get("recovery_target_generation"),
                },
                "availability": {
                    "source": "dispatcher-sdk inspect_work_availability",
                    "observed_at": availability.observed_at,
                    "stale": availability_stale,
                    "snapshot_consistency": availability.snapshot_consistency,
                    "complete": availability.complete,
                    "reason_codes": list(availability.reason_codes)[:20],
                    "counts": {
                        "active_leases": availability.active_leases,
                        "queued_ready": availability.queued_ready,
                        "expired_leases": availability.expired_leases,
                        "future_retries": availability.future_retries,
                        "pending_commands": availability.pending_commands,
                        "pending_result_sync": availability.pending_result_sync,
                        "pending_result_delivery": availability.pending_result_delivery,
                        "open_waits": availability.open_waits,
                        "recovery_required": availability.recovery_required,
                        "unknown_effects": availability.unknown_effects,
                        "missing_executions": availability.missing_executions,
                    },
                    "samples_truncated": availability.summaries_truncated,
                    "samples": [{
                        "task_id": row.task_id[:256], "execution_id": row.execution_id[:128],
                        "state": row.state, "application_attempt": row.application_attempt,
                    } for row in availability.summaries[:20]],
                },
                "active_tasks": sampled_tasks,
                "requested_task": task_view(requested_task) if requested_task else None,
                "overall_status": ("waiting" if run_state not in TERMINAL and open_waits
                                   else run_state),
                "last_code_change": {"status": "unknown", "source": "SDK task outputs",
                                     "observed_at": None, "stale": True,
                                     "selection_scope": ["development_prepare_integrate", "development_integrate",
                                         "target_repair_integrate", "contract_repair_integrate"],
                                     "selection_complete": task_scan_complete,
                                     "reason": ("no bounded, verified integrated code-change record selected"
                                                if task_scan_complete else
                                                "bounded task selection incomplete; latest code change is unknown")},
                "last_successful_authenticated_verification": {
                    "status": "unknown", "source": "SDK task artifact references",
                    "observed_at": None, "stale": True,
                    "selection_scope": ["contract_verify", "target_build", "test_execute",
                        "acceptance_build", "client_smoke"],
                    "selection_complete": task_scan_complete,
                    "reason": ("no successful authenticated verification was found in the bounded stage sample"
                               if task_scan_complete else
                               "bounded task selection incomplete; latest successful verification is unknown")},
                "current_wait": {
                    "status": ("unknown" if availability_stale else
                               "open" if any(not item["stale"] for item in wait_samples)
                               or open_waits else
                               "pending" if planned_tasks else
                               "none" if task_scan_complete or run_state in TERMINAL else
                               "unknown"),
                    "source": ("host execution-progress wait samples"
                               if any(not item["stale"] for item in wait_samples)
                               else "dispatcher-sdk inspect_work_availability"),
                    "observed_at": (max((item["observed_at"] for item in wait_samples), default=None)
                                    or availability.observed_at),
                    "stale": (availability_stale or all(item["stale"] for item in wait_samples)
                              if wait_samples else availability_stale or
                              (not task_scan_complete and run_state not in TERMINAL
                               and not open_waits)),
                    "detail": ("SDK has tasks pending dispatch, queue admission, or dependencies; the bounded summary cannot assign a capacity reason."
                               if planned_tasks and not wait_samples and not open_waits else
                               None if wait_samples or not open_waits else
                               "SDK reports %d open wait(s); the count-only API has no wait reason." % open_waits),
                    "samples": wait_samples,
                    "planned_task_ids": planned_tasks[:8],
                    "planned_selection_complete": task_scan_complete,
                },
            },
        }

        # Code-change and verification claims require an SDK-authenticated
        # successful task result plus a bounded SHA check of its artifact.
        hash_budget = 16 * 1024 * 1024
        artifact_limit = 8 * 1024 * 1024
        artifact_bytes = 0
        evidence_scan_complete = True
        def read_authenticated_json(ref):
            nonlocal artifact_bytes, evidence_scan_complete
            if not isinstance(ref, dict) or not isinstance(ref.get("sha256"), str):
                evidence_scan_complete = False
                return None
            try:
                path = verified_path(root, ref)
                size = path.stat().st_size
                if size > artifact_limit or artifact_bytes + size > hash_budget:
                    evidence_scan_complete = False
                    return None
                content = path.read_bytes()
                artifact_bytes += size
                from hashlib import sha256
                if sha256(content).hexdigest() != ref["sha256"]:
                    evidence_scan_complete = False
                    return None
                value = json.loads(content)
                return value if isinstance(value, dict) else None
            except (OSError, ValueError, TypeError, json.JSONDecodeError):
                evidence_scan_complete = False
                return None

        if task_id is None:
            verification_rows = []
            change_rows = []
            for _, task in stage_tasks.items():
                view = task_view(task)
                stage = view.get("stage_id")
                attempt = task.get("latest_attempt", {}) if isinstance(task, dict) else {}
                if not isinstance(attempt, dict) or attempt.get("state") != "succeeded":
                    continue
                value = ((attempt.get("result") or {}).get("value") or {})
                if not isinstance(value, dict) or value.get("status") not in {"completed", "succeeded"}:
                    continue
                outputs = value.get("outputs") if isinstance(value.get("outputs"), dict) else {}
                refs = outputs.get("artifact_refs") if isinstance(outputs.get("artifact_refs"), dict) else {}
                updated_at = view.get("updated_at")
                if updated_at is None:
                    evidence_scan_complete = False
                if stage == "development_prepare_integrate" and outputs.get("head"):
                    prepare_ref = refs.get("development_prepare")
                    prepare = read_authenticated_json(prepare_ref)
                    before = prepare.get("before_commit") if isinstance(prepare, dict) else None
                    after = prepare.get("after_commit") if isinstance(prepare, dict) else None
                    changed_paths = prepare.get("changed_paths") if isinstance(prepare, dict) else None
                    if (isinstance(before, str) and re.fullmatch(r"[0-9a-f]{40,64}", before)
                            and isinstance(after, str) and re.fullmatch(r"[0-9a-f]{40,64}", after)
                            and after == outputs.get("head")
                            and isinstance(changed_paths, list)
                            and all(isinstance(path, str) and path for path in changed_paths)):
                        change_rows.append({
                            "status": "observed" if changed_paths else "no_change",
                            "source": "successful SDK development_prepare_integrate task",
                            "task_id": view.get("task_id"), "head": after,
                            "base": before,
                            "changed_paths": [path[:256] for path in changed_paths[:20]],
                            "selection_scope": ["development_prepare_integrate",
                                "development_integrate", "target_repair_integrate",
                                "contract_repair_integrate"],
                            "selection_complete": task_scan_complete,
                            "observed_at": updated_at,
                            "stale": not isinstance(updated_at, (int, float))
                                     or observed_at - updated_at > 3600,
                            "artifact": {"path": str(prepare_ref.get("path", ""))[:512],
                                         "sha256": str(prepare_ref.get("sha256", ""))[:128]},
                            "artifact_identity_verified": True,
                        })
                if stage == "development_integrate" and outputs.get("head"):
                    integration_ref = refs.get("development_integration")
                    integration = read_authenticated_json(integration_ref)
                    base = outputs.get("development_base")
                    integrated_head = outputs.get("head")
                    integrated_tasks = integration.get("tasks") if isinstance(integration, dict) else None
                    valid_commit_ids = (isinstance(base, str) and isinstance(integrated_head, str)
                                        and re.fullmatch(r"[0-9a-f]{40,64}", base) is not None
                                        and re.fullmatch(r"[0-9a-f]{40,64}", integrated_head) is not None)
                    valid_task_ids = (isinstance(integrated_tasks, list) and bool(integrated_tasks)
                                      and all(isinstance(item, str) and item for item in integrated_tasks))
                    if (integration is not None and valid_commit_ids
                            and valid_task_ids and integration.get("head") == integrated_head
                            and integration.get("base") == base
                            and isinstance(integrated_tasks, list)):
                        changed = base != integrated_head
                        change_status = ("observed" if changed else
                                         "no_change" if base == integrated_head else "incomplete")
                        change_rows.append({
                            "status": change_status,
                            "source": "successful SDK development_integrate task",
                            "task_id": view.get("task_id"), "head": integrated_head,
                            "base": base,
                            "integrated_task_ids": [item[:128] for item in
                                integrated_tasks[:20] if isinstance(item, str)],
                            "selection_scope": ["development_prepare_integrate", "development_integrate",
                                "target_repair_integrate", "contract_repair_integrate"],
                            "selection_complete": task_scan_complete,
                            "observed_at": updated_at,
                            "stale": not isinstance(updated_at, (int, float))
                                    or observed_at - updated_at > 3600,
                            "artifact": {"path": str(integration_ref.get("path", ""))[:512],
                                         "sha256": str(integration_ref.get("sha256", ""))[:128]},
                            "artifact_identity_verified": True,
                        })
                if stage in {"target_repair_integrate", "contract_repair_integrate"}:
                    repair_ref = refs.get("repair_integration")
                    repair = read_authenticated_json(repair_ref)
                    expected_scope = ("target" if stage == "target_repair_integrate"
                                      else "contract")
                    source_head = repair.get("source_head") if isinstance(repair, dict) else None
                    integrated_head = outputs.get("head")
                    changed_paths = repair.get("changed_paths") if isinstance(repair, dict) else None
                    if (isinstance(repair, dict) and repair.get("scope") == expected_scope
                            and repair.get("head") == integrated_head
                            and isinstance(source_head, str)
                            and re.fullmatch(r"[0-9a-f]{40,64}", source_head)
                            and isinstance(integrated_head, str)
                            and re.fullmatch(r"[0-9a-f]{40,64}", integrated_head)
                            and isinstance(changed_paths, list)
                            and changed_paths == outputs.get("changed_paths")
                            and all(isinstance(path, str) and path for path in changed_paths)):
                        change_rows.append({
                            "status": "observed" if changed_paths else "no_change",
                            "source": "successful SDK %s task" % stage,
                            "task_id": view.get("task_id"), "head": integrated_head,
                            "base": source_head, "goal_scope": expected_scope,
                            "changed_paths": [path[:256] for path in changed_paths[:20]],
                            "selection_scope": ["development_prepare_integrate", "development_integrate",
                                "target_repair_integrate", "contract_repair_integrate"],
                            "selection_complete": task_scan_complete,
                            "observed_at": updated_at,
                            "stale": not isinstance(updated_at, (int, float))
                                     or observed_at - updated_at > 3600,
                            "artifact": {"path": str(repair_ref.get("path", ""))[:512],
                                         "sha256": str(repair_ref.get("sha256", ""))[:128]},
                            "artifact_identity_verified": True,
                        })
                if stage in {"contract_verify", "target_build", "test_execute",
                             "acceptance_build", "client_smoke"}:
                    if len(refs) > 16:
                        evidence_scan_complete = False
                    for alias, ref in list(refs.items())[:16]:
                        if alias not in {"baseline_contract_tests_candidate",
                                         "target_contract_tests_candidate"}:
                            continue
                        report = read_authenticated_json(ref)
                        if not isinstance(report, dict):
                            continue
                        records = report.get("evidence_records")
                        runtime_ids = ([test_id for test_id, record in records.items()
                                        if isinstance(test_id, str) and isinstance(record, dict)
                                        and record.get("evidence_kind") == "runtime"
                                        and type(record.get("runtime_witness_count")) is int
                                        and record["runtime_witness_count"] > 0
                                        and isinstance(record.get("sha256"), str)
                                        and re.fullmatch(r"[0-9a-f]{64}", record["sha256"])]
                                       if isinstance(records, dict) else [])
                        case_results = report.get("case_results")
                        requires_cases = header["definition"].get("workflow_version", 0) >= 29
                        v29_cases_valid = (not requires_cases or
                            (isinstance(case_results, dict) and bool(case_results)
                             and report.get("candidate_unchanged") is True
                             and all(isinstance(row, dict) and row.get("status") == "passed"
                                     and isinstance(row.get("xml_artifact_ref"), dict)
                                     for row in case_results.values())))
                        successful = (type(report.get("exit_code")) is int
                                      and report["exit_code"] == 0
                                      and isinstance(report.get("execution_nonce"), str)
                                      and re.fullmatch(r"[0-9a-f]{64}", report["execution_nonce"])
                                      and isinstance(report.get("source_commit"), str)
                                      and re.fullmatch(r"[0-9a-f]{40,64}", report["source_commit"])
                                      and report.get("record_errors") == []
                                      and report.get("no_source_tasks") == []
                                      and not value.get("error_code")
                                      and v29_cases_valid)
                        if successful and runtime_ids:
                            verification_rows.append({
                                "status": "observed", "source": "successful SDK %s task + SHA-verified artifact" % stage,
                                "task_id": view.get("task_id"), "stage_id": stage,
                                "selection_scope": ["contract_verify", "target_build",
                                    "test_execute", "acceptance_build", "client_smoke"],
                                "selection_complete": task_scan_complete,
                                "observed_at": updated_at,
                                "stale": not isinstance(updated_at, (int, float))
                                        or observed_at - updated_at > 3600,
                                "artifact": {"path": str(ref.get("path", ""))[:512],
                                             "sha256": str(ref.get("sha256", ""))[:128]},
                                "authenticated_test_count": len(runtime_ids),
                                "authenticated_test_sample": [item[:256] for item in runtime_ids[:20]],
                            })
            selection_complete = task_scan_complete and evidence_scan_complete
            snapshot["status_summary"]["last_code_change"]["selection_complete"] = selection_complete
            snapshot["status_summary"]["last_successful_authenticated_verification"]["selection_complete"] = selection_complete
            if change_rows:
                change_rows.sort(key=lambda row: row.get("observed_at") or 0)
                selected_change = change_rows[-1]
                selected_change["selection_complete"] = selection_complete
                if not selection_complete:
                    selected_change["status"] = "sampled_" + selected_change["status"]
                snapshot["status_summary"]["last_code_change"] = selected_change
            if verification_rows:
                verification_rows.sort(key=lambda row: row.get("observed_at") or 0)
                selected_verification = verification_rows[-1]
                selected_verification["selection_complete"] = selection_complete
                if not selection_complete:
                    selected_verification["status"] = "sampled"
                snapshot["status_summary"]["last_successful_authenticated_verification"] = selected_verification

        return MigrationRun(run_id, root, snapshot)

    def run(self, request, *, run_dir, run_id=None, model_policy=None):
        return self.execute(self.submit(request, run_dir=run_dir, run_id=run_id,
                                       model_policy=model_policy))

    @staticmethod
    def _progress_cancelled_assignment_evidence(root, header, sdk, recovery, operation):
        """Prove the committed supervisor decision and exact SDK cleanup custody."""
        if not progress_supervised(header):
            return None
        execution, effect = recovery.execution, recovery.effect
        if (recovery.run_id != header['run_id'] or recovery.task_id != operation.task_id
                or operation.run_id != header.get("logical_run_id", recovery.run_id)
                or operation.command_id != execution.command.execution_id
                or execution.command.handler_id != f'modport.{operation.stage_id}'
                or execution.recovery_target_state != 'cancelled'
                or effect.execution_id != operation.command_id
                or effect.effect_id != f'modport:{operation.command_id}'
                or effect.name != 'modport.stage'):
            return None
        snapshot = hydrate_run_snapshot(root, sdk.get_run(recovery.run_id))
        from .progress_supervisor import recorded_progress_termination
        authority = recorded_progress_termination(snapshot, operation, execution.recovery_reason)
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
                or entry.execution_state != 'recovery_required' or entry.task_state != 'recovery_required'
                or entry.execution_result_known
                or any(getattr(entry, name).status != 'confirmed' for name in (
                    'request_committed', 'command_delivered', 'execution_authority_revoked',
                    'local_process_tree_reaped', 'cleanup'))
                or not any(row.get('effect_id') == effect.effect_id and row.get('revision') == effect.revision
                           and row.get('attempt') == effect.attempt and row.get('fence') == effect.fence
                           and row.get('state') == 'indeterminate' for row in entry.effects)):
            return None
        return {'schema': 'modport.progress-assignment-cancellation.v1',
            'run_id': operation.run_id, 'task_id': operation.task_id,
            'stage_id': operation.stage_id, 'execution_id': operation.command_id,
            'effect_id': effect.effect_id, 'effect_revision': effect.revision,
            'kernel_attempt': effect.attempt, 'fence': effect.fence,
            'application_attempt': recovery.attempt, 'recovery_reason': execution.recovery_reason,
            'supervisor_authorization': authority, 'receipt_ids': list(entry.receipt_ids),
            'proof': {name: getattr(entry, name).status for name in (
                'request_committed', 'command_delivered', 'execution_authority_revoked',
                'local_process_tree_reaped', 'cleanup')}, 'issues': list(entry.issues),
            'effects_truncated': entry.effects_truncated, 'receipts_truncated': entry.receipts_truncated,
            'external_outcome': 'unknown', 'artifacts_complete': False, 'acceptance_status': 'unverified'}

    @staticmethod
    def _settle_progress_cancelled_effects(root, header, sdk, *, snapshot=None):
        """Settle authorized cancellations before execution parks or declares stop."""
        if not progress_supervised(header):
            return False
        if snapshot is None:
            sdk.sync()
            snapshot = hydrate_run_snapshot(root, sdk.get_run(header['run_id']))
        terminated = (snapshot.get('application_state') or {}).get(
            'progress_supervision', {}).get('terminated_executions', {})
        if not any((task.get('attempts') or [{}])[-1].get('state') == 'recovery_required'
                   and (task.get('attempts') or [{}])[-1].get('command', {}).get('execution_id') in terminated
                   for task in snapshot.get('tasks', {}).values()):
            return False
        sdk.sync()
        changed = False
        while True:
            settled = False
            for recovery in sdk.inspect_recoveries(header['run_id']):
                command = recovery.execution.command.to_dict()
                operation = OperationInput.from_dict(unpack_input(root, command['payload']))
                evidence = MigrationOperations._progress_cancelled_assignment_evidence(
                    root, header, sdk, recovery, operation)
                if evidence is None:
                    continue
                try:
                    with workspace_lock(operation_lock(root, operation), blocking=False):
                        from .kernel_runtime import cancel_incomplete_progress_assignment
                        response = cancel_incomplete_progress_assignment(root, command, recovery.effect, evidence)
                        sdk.resolve_effect(recovery.effect.effect_id, decision='applied', response=response,
                            expected_revision=recovery.effect.revision,
                            recovery_id=(f'progress-settlement:{recovery.effect.effect_id}:'
                                         f'{recovery.effect.revision}:'
                                         f'{evidence["supervisor_authorization"]["review_id"]}'))
                except BlockingIOError:
                    continue  # A peer still owns this workspace; leave recovery uncertain.
                sdk.sync()
                settled = True
                changed = True
                break
            if not settled:
                return changed

    @staticmethod
    def _settle_cancelled_coder_effects(root, header, sdk):
        """Close only reaped, user-cancelled coder effects with known identity.

        This records an incomplete failed assignment; it does not assert that
        workspace or external effects were rolled back. Other recovery cases
        remain parked for ordinary explicit recovery.
        """
        if header["definition"]["workflow_version"] < 25:
            return
        run_id = header["run_id"]
        while True:
            snapshot = hydrate_run_snapshot(root, sdk.get_run(run_id))
            app = snapshot.get("application_state") or {}
            if (app.get("user_cancelled") is not True
                    or app.get("stop_reason") != "user_cancelled"):
                return
            candidate = None
            for recovery in sdk.inspect_recoveries(run_id):
                execution = recovery.execution
                effect = recovery.effect
                if (execution.recovery_target_state != "cancelled"
                        or execution.recovery_reason != "user_cancelled"
                        or effect.execution_id != execution.command.execution_id
                        or execution.command.handler_id != "modport.coder"
                        or recovery.run_id != run_id):
                    continue
                command = execution.command.to_dict()
                operation = OperationInput.from_dict(unpack_input(root, command["payload"]))
                if (operation.stage_id != "coder"
                        or operation.run_id != run_id
                        or operation.task_id != recovery.task_id
                        or operation.command_id != execution.command.execution_id
                        or effect.effect_id != f"modport:{operation.command_id}"
                        or effect.name != "modport.stage"):
                    continue
                from .kernel_runtime import operation_workspace
                try:
                    workspace = operation_workspace(root, operation)
                except ValueError:
                    continue
                if workspace is None or not workspace.is_dir() or workspace.is_symlink():
                    continue
                report = sdk.inspect_cancellation(run_id, execution_id=operation.command_id)
                entry = next((row for row in report.executions
                              if row.execution_id == operation.command_id
                              and row.task_id == recovery.task_id
                              and row.application_attempt == recovery.attempt), None)
                if (entry is None or entry.issues or entry.effects_truncated
                        or entry.receipts_truncated
                        or entry.execution_state != "recovery_required"
                        or entry.request_committed.status != "confirmed"
                        or entry.command_delivered.status != "confirmed"
                        or entry.execution_authority_revoked.status != "confirmed"
                        or entry.local_process_tree_reaped.status != "confirmed"
                        or entry.cleanup.status != "confirmed"
                        or not any(row.get("effect_id") == effect.effect_id
                                   and row.get("state") == "indeterminate"
                                   and row.get("revision") == effect.revision
                                   for row in entry.effects)):
                    continue
                candidate = (operation, command, effect)
                break
            if candidate is None:
                return
            operation, command, effect = candidate
            with workspace_lock(operation_lock(root, operation), blocking=False):
                from .kernel_runtime import cancel_incomplete_coder
                response = cancel_incomplete_coder(root, command, effect)
                sdk.resolve_effect(effect.effect_id, decision="applied", response=response,
                    expected_revision=effect.revision,
                    recovery_id=f"receipt:{effect.effect_id}:{digest(response)}")
            sdk.sync()

    @staticmethod
    def _cancelled_agent_stage_evidence(root, header, sdk, recovery,
                                        operation):
        """Prove cleanup and stage dependencies before settling selected agents."""
        if header.get("definition", {}).get("workflow_version", 0) < 26:
            return None
        snapshot = hydrate_run_snapshot(root, sdk.get_run(recovery.run_id))
        app = snapshot.get("application_state") or {}
        if (app.get("user_cancelled") is not True
                or app.get("stop_reason") != "user_cancelled"):
            return None
        expected_handlers = {
            "agent_rework": "modport.agent_rework",
            "contract_draft": "modport.contract_draft",
            "contract_verify": "modport.contract_verify",
            "contract_review": "modport.contract_review",
            "supervisor": "modport.supervisor",
            "artifact_test_design": "modport.artifact_test_design",
            "artifact_test_execute": "modport.artifact_test_execute",
            "behavior_extract": "modport.behavior_extract",
            "behavior_review": "modport.behavior_review",
            "code_cleanup": "modport.code_cleanup",
            "final_cleanup": "modport.final_cleanup",
        }
        handler_id = expected_handlers.get(operation.stage_id)
        execution = recovery.execution
        effect = recovery.effect
        if (handler_id is None
                or recovery.run_id != header.get("run_id")
                or recovery.task_id != operation.task_id
                or operation.run_id != header.get("logical_run_id", recovery.run_id)
                or operation.command_id != execution.command.execution_id
                or execution.command.handler_id != handler_id
                or execution.recovery_target_state != "cancelled"
                or execution.recovery_reason != "user_cancelled"
                or effect.execution_id != operation.command_id
                or effect.effect_id != f"modport:{operation.command_id}"
                or effect.name != "modport.stage"):
            return None
        if operation.stage_id not in {"code_cleanup", "final_cleanup"}:
            expected_request = {"input_sha256": digest(operation.to_dict()),
                                "run_dir": str(root), "stage": operation.stage_id}
            if effect.request != expected_request:
                return None
        report = sdk.inspect_cancellation(recovery.run_id,
                                          execution_id=operation.command_id)
        if report.truncated or len(report.executions) != 1:
            return None
        entry = report.executions[0]
        if (entry.issues or entry.effects_truncated or entry.receipts_truncated
                or entry.task_id != recovery.task_id
                or entry.application_attempt != recovery.attempt
                or entry.execution_id != operation.command_id
                or entry.execution_revision != execution.revision
                or entry.kernel_attempt != effect.attempt
                or entry.fence != effect.fence
                or entry.execution_state != "recovery_required"
                or entry.task_state != "recovery_required"
                or entry.execution_result_known
                or entry.request_committed.status != "confirmed"
                or entry.command_delivered.status != "confirmed"
                or entry.execution_authority_revoked.status != "confirmed"
                or entry.local_process_tree_reaped.status != "confirmed"
                or entry.cleanup.status != "confirmed"
                or not any(row.get("effect_id") == effect.effect_id
                           and row.get("revision") == effect.revision
                           and row.get("attempt") == effect.attempt
                           and row.get("fence") == effect.fence
                           and row.get("state") == "indeterminate"
                           for row in entry.effects)):
            return None

        review_requests = []
        if (operation.stage_id == "agent_rework"
                or (operation.stage_id == "contract_verify"
                    and operation.task_id.startswith("agent-rework."))):
            rework = operation.payload.get("reviewer_rework")
            reviewer_id = operation.payload.get("reviewer_execution_id")
            request_id = rework.get("request_id") if isinstance(rework, Mapping) else None
            ledger = app.get("review_rework", {}).get("requests", {})
            record = (ledger.get(f"{reviewer_id}/{request_id}")
                      if isinstance(ledger, Mapping) else None)
            expected_task_ids = ({f"agent-rework.{request_id}"}
                                 if isinstance(request_id, str) else set())
            if operation.stage_id == "contract_verify" and isinstance(request_id, str):
                expected_task_ids.add(f"agent-rework.{request_id}.verify")
            if (not isinstance(request_id, str) or not request_id
                    or not isinstance(reviewer_id, str) or not reviewer_id
                    or operation.task_id not in expected_task_ids
                    or not isinstance(record, Mapping)
                    or record.get("state") != "running"
                    or record.get("request_id") != request_id
                    or record.get("reviewer_execution_id") != reviewer_id
                    or record.get("task_id") != operation.task_id):
                return None
        if operation.stage_id == "contract_review":
            ledger = app.get("review_rework", {}).get("requests", {})
            tasks = snapshot.get("tasks", {})
            outstanding = [record for record in ledger.values()
                           if record.get("state") == "running"
                           and record.get("reviewer_execution_id") == operation.command_id]
            recoveries = sdk.inspect_recoveries(recovery.run_id)
            recovery_task_ids = {item.task_id for item in recoveries}
            from .rework_tools import session_directory
            for record in outstanding:
                request_id = record.get("request_id")
                task_id = record.get("task_id")
                task = tasks.get(task_id)
                if (not isinstance(request_id, str) or not isinstance(task_id, str)
                        or task is None or not task.get("attempts")
                        or task_id in recovery_task_ids):
                    return None
                attempt = task["attempts"][-1]
                if (attempt.get("state") not in EXECUTION_TERMINAL
                        or attempt.get("command", {}).get("execution_id") is None):
                    return None
                session = session_directory(root, operation.command_id)
                request_path = session / "requests" / f"{request_id}.json"
                response_path = session / "responses" / f"{request_id}.json"
                if (request_path.is_symlink() or not request_path.is_file()
                        or request_path.resolve() != request_path.absolute()
                        or response_path.is_symlink()):
                    return None
                request = read_json(request_path)
                if (not isinstance(request, Mapping)
                        or request.get("run_id") != operation.run_id
                        or request.get("reviewer_execution_id") != operation.command_id
                        or request.get("request_id") != request_id
                        or request.get("task_id") not in {None, task_id}):
                    return None
                if response_path.exists():
                    return None
                raw_result = attempt.get("result")
                result = raw_result.get("value", {}) if isinstance(raw_result, Mapping) else {}
                if not isinstance(result, Mapping):
                    result = {}
                review_requests.append({
                    "reviewer_execution_id": operation.command_id,
                    "request_id": request_id, "task_id": task_id,
                    "child_execution_id": attempt["command"]["execution_id"],
                    "child_execution_state": attempt["state"],
                    "child_business_status": result.get("status"),
                    "child_error_code": result.get("error_code"),
                    "task_terminal": True, "accepted": False,
                })

        return {
            "schema": "modport.interrupted-agent-cancellation.v1",
            "run_id": operation.run_id, "task_id": operation.task_id,
            "stage_id": operation.stage_id, "execution_id": operation.command_id,
            "effect_id": effect.effect_id, "effect_revision": effect.revision,
            "kernel_attempt": effect.attempt, "fence": effect.fence,
            "application_attempt": recovery.attempt,
            "recovery_reason": execution.recovery_reason,
            "receipt_ids": list(entry.receipt_ids),
            "proof": {name: getattr(entry, name).status for name in (
                "request_committed", "command_delivered",
                "execution_authority_revoked", "local_process_tree_reaped",
                "cleanup")},
            "issues": list(entry.issues),
            "effects_truncated": entry.effects_truncated,
            "receipts_truncated": entry.receipts_truncated,
            "review_rework_requests": review_requests,
            "external_outcome": "unknown",
            "artifacts_complete": False, "acceptance_status": "unverified",
        }

    @staticmethod
    def _rework_verifier_cancellation_evidence(root, header, sdk, recovery,
                                                operation):
        """Return exact cancellation proof for a closed reviewer's verifier."""
        execution = recovery.execution
        effect = recovery.effect
        if (header["definition"]["workflow_version"] < 25
                or operation.stage_id not in {"contract_verify", "target_build"}
                or not operation.task_id.endswith(".verify")
                or operation.run_id != header.get("logical_run_id", recovery.run_id)
                or operation.task_id != recovery.task_id
                or operation.command_id != execution.command.execution_id
                or execution.command.handler_id != f"modport.{operation.stage_id}"
                or effect.execution_id != operation.command_id
                or effect.effect_id != f"modport:{operation.command_id}"
                or effect.name != "modport.stage"
                or execution.recovery_target_state != "cancelled"
                or execution.recovery_reason != "reviewer tool call closed"):
            return None
        reviewer = operation.payload.get("reviewer_rework")
        if not isinstance(reviewer, dict):
            return None
        request_id = reviewer.get("request_id")
        reviewer_execution_id = reviewer.get("reviewer_execution_id")
        if (not isinstance(request_id, str) or not request_id
                or operation.task_id != f"agent-rework.{request_id}.verify"):
            return None
        snapshot = hydrate_run_snapshot(root, sdk.get_run(recovery.run_id))
        requests = ((snapshot.get("application_state") or {})
                    .get("review_rework", {}).get("requests", {}))
        if not isinstance(requests, dict):
            return None
        record = requests.get(f"{reviewer_execution_id}/{request_id}")
        if (not isinstance(record, dict) or record.get("state") != "running"
                or record.get("task_id") != operation.task_id
                or record.get("followup_stage") != operation.stage_id
                or record.get("request_id") != request_id
                or record.get("reviewer_execution_id") != reviewer_execution_id):
            return None
        callers = [attempt["command"]
                   for task in snapshot["tasks"].values()
                   for attempt in task.get("attempts", [])
                   if attempt["command"]["execution_id"] == reviewer_execution_id]
        if len(callers) != 1:
            return None
        caller_command = callers[0]
        try:
            caller = OperationInput.from_dict(unpack_input(root, caller_command["payload"]))
        except (KeyError, TypeError, ValueError):
            return None
        if (caller.run_id != operation.run_id
                or caller.command_id != reviewer_execution_id
                or caller.stage_id != record.get("reviewer_stage")
                or caller_command["handler_id"] != f"modport.{caller.stage_id}"):
            return None
        from .rework_tools import session_directory
        try:
            session = session_directory(root, reviewer_execution_id)
            descriptor = session / "session.json"
            if (not descriptor.is_file() or descriptor.is_symlink()
                    or descriptor.resolve() != descriptor.absolute()):
                return None
            session_data = read_json(descriptor)
            if (not isinstance(session_data, dict)
                    or session_data.get("run_id") != operation.run_id
                    or session_data.get("reviewer_execution_id") != reviewer_execution_id):
                return None
            cancelled = session / "requests" / f"{request_id}.cancel.json"
            closed = session / "closed.json"
            marker = None
            if (cancelled.is_file() and not cancelled.is_symlink()
                    and cancelled.resolve() == cancelled.absolute()):
                data = read_json(cancelled)
                if (isinstance(data, dict) and data.get("request_id") == request_id
                        and data.get("run_id") == operation.run_id
                        and data.get("reviewer_execution_id") == reviewer_execution_id):
                    marker = cancelled.relative_to(root).as_posix()
            if (marker is None and closed.is_file() and not closed.is_symlink()
                    and closed.resolve() == closed.absolute()):
                data = read_json(closed)
                if (isinstance(data, dict) and isinstance(data.get("reason"), str)
                        and data["reason"].strip()):
                    marker = closed.relative_to(root).as_posix()
        except (OSError, ValueError, TypeError):
            return None
        if marker is None:
            return None
        report = sdk.inspect_cancellation(recovery.run_id,
                                          execution_id=operation.command_id)
        if report.truncated or len(report.executions) != 1:
            return None
        entry = report.executions[0]
        if (entry.issues or entry.effects_truncated or entry.receipts_truncated
                or entry.task_id != recovery.task_id
                or entry.application_attempt != recovery.attempt
                or entry.execution_id != operation.command_id
                or entry.execution_revision != execution.revision
                or entry.kernel_attempt != effect.attempt
                or entry.fence != effect.fence
                or entry.execution_state != "recovery_required"
                or entry.task_state != "recovery_required"
                or entry.execution_result_known
                or entry.request_committed.status != "confirmed"
                or entry.command_delivered.status != "confirmed"
                or entry.execution_authority_revoked.status != "confirmed"
                or entry.local_process_tree_reaped.status != "confirmed"
                or entry.cleanup.status != "confirmed"
                or not any(row.get("effect_id") == effect.effect_id
                           and row.get("revision") == effect.revision
                           and row.get("attempt") == effect.attempt
                           and row.get("fence") == effect.fence
                           and row.get("state") == "indeterminate"
                           for row in entry.effects)):
            return None
        return {
            "schema": "modport.rework-verification-cancellation.v1",
            "run_id": operation.run_id, "task_id": operation.task_id,
            "stage_id": operation.stage_id, "execution_id": operation.command_id,
            "effect_id": effect.effect_id, "effect_revision": effect.revision,
            "kernel_attempt": effect.attempt, "fence": effect.fence,
            "application_attempt": recovery.attempt,
            "recovery_reason": execution.recovery_reason,
            "reviewer_execution_id": reviewer_execution_id,
            "reviewer_stage": caller.stage_id,
            "reviewer_task_id": caller.task_id,
            "request_id": request_id, "close_marker": marker,
            "cancellation_receipt_ids": list(entry.receipt_ids),
            "proof": {name: getattr(entry, name).status for name in (
                "request_committed", "command_delivered",
                "execution_authority_revoked", "local_process_tree_reaped",
                "cleanup")},
            "external_outcome": "unknown",
            "artifacts_complete": False, "acceptance_status": "unverified",
        }

    def cancel(self, run_dir, run_id):
        with self.session(run_dir, run_id) as (root, header, _, sdk):
            self._audit_action(root, run_id, "cancel")
            deadline = time.monotonic() + CANCEL_SETTLE_TIMEOUT_SECONDS
            while True:
                state = self.tick(sdk, header, stop_reason="user_cancelled")
                app = state.get("application_state") or {}
                if (state["state"] in TERMINAL
                        or app.get("user_cancelled") is True
                        or app.get("stop_reason") == "user_cancelled"):
                    break
                if time.monotonic() >= deadline:
                    raise TimeoutError("could not persist the user cancellation decision")
                # tick() deliberately returns the latest snapshot on a revision
                # conflict. Re-sync and retry until that snapshot proves the
                # user cancellation decision was committed.
                sdk.sync()
                time.sleep(min(CANCEL_SETTLE_POLL_SECONDS,
                               max(0.0, deadline - time.monotonic())))

            active = []
            for task_id, task in state.get("tasks", {}).items():
                attempts = task.get("attempts", [])
                if not attempts:
                    continue
                attempt = attempts[-1]
                if attempt.get("state") not in EXECUTION_TERMINAL:
                    active.append((task_id, attempt["command"]["execution_id"]))

            # A separately invoked CLI has no reference to the process
            # supervisors owned by the active driver's Runtime. Leave cancel
            # outbox delivery to that owner instead of consuming it here.
            pending = active
            while pending:
                sdk.sync()
                pending = []
                for task_id, execution_id in active:
                    report = sdk.inspect_cancellation(run_id, execution_id=execution_id)
                    entry = next((item for item in report.executions
                                  if item.execution_id == execution_id), None)
                    # The owner may finish an execution just before it consumes
                    # the cancel outbox item. No process remains to reap, and
                    # no cancellation journal receipt will be written for this
                    # already-terminal non-cancelled execution.
                    if (entry is not None
                            and entry.execution_state in EXECUTION_TERMINAL
                            and entry.execution_state != "cancelled"):
                        continue
                    if (entry is None
                            or entry.request_committed.status != "confirmed"
                            or entry.command_delivered.status != "confirmed"
                            or entry.local_process_tree_reaped.status not in {"confirmed", "not_applicable"}
                            or entry.cleanup.status not in {"confirmed", "not_applicable"}):
                        pending.append((task_id, execution_id))
                if not pending:
                    break
                if time.monotonic() >= deadline:
                    waiting_for = ", ".join(execution_id for _, execution_id in pending)
                    raise TimeoutError(
                        "the active SDK Runtime did not confirm cancellation cleanup for: "
                        + waiting_for
                    )
                time.sleep(min(CANCEL_SETTLE_POLL_SECONDS,
                               max(0.0, deadline - time.monotonic())))

            # Synchronize the owner's settled Kernel facts before the policy
            # records recovery waits or publishes a terminal Run state.
            sdk.sync()
            self._settle_cancelled_coder_effects(root, header, sdk)
            state = self.tick(sdk, header)
            return MigrationRun(run_id, root,
                                hydrate_run_snapshot(root, sdk.get_run(run_id)))

    def recover(self, run_dir, run_id, *, cancel_interrupted_research=False, resume_native_goals=False):
        self._header(Path(run_dir).resolve(), run_id)
        check_storage_budget(run_dir, phase="recover")
        with self.session(run_dir, run_id,
                          allow_cancelled_recovery=not resume_native_goals) as (root, header, runtime, sdk):
            self._audit_action(root, run_id, "recover")
            observed = hydrate_run_snapshot(root, sdk.get_run(run_id))
            app = observed.get("application_state") or {}
            if resume_native_goals and (app.get("user_cancelled") is True
                                        or app.get("stop_reason") == "user_cancelled"):
                raise ValueError("native goal recovery is forbidden after user cancellation")
            runtime.reap()
            sdk.sync()
            from .interrupted_execution import reconcile_interrupted_executions
            reconcile_interrupted_executions(root, header, runtime, sdk)
            self._settle_cancelled_coder_effects(root, header, sdk)
            self._settle_progress_cancelled_effects(root, header, sdk)
            # The SDK reads Kernel authority even when the orchestration view
            # is stale. Re-query after each resolution for the next Effect.
            while True:
                recoveries = sdk.inspect_recoveries(run_id)
                if not recoveries:
                    break
                def recovery_priority(item):
                    try:
                        operation = OperationInput.from_dict(unpack_input(
                            root, item.execution.command.payload))
                    except (KeyError, TypeError, ValueError):
                        return 10
                    if (operation.task_id.startswith("agent-rework.")
                            and operation.stage_id != "contract_review"):
                        return -1
                    priorities = {"contract_draft": 0, "supervisor": 1,
                                  "contract_review": 10}
                    return priorities.get(operation.stage_id, 5)
                recoveries.sort(key=recovery_priority)
                recovery = recoveries[0]
                command = recovery.execution.command.to_dict()
                effect = recovery.effect
                operation = OperationInput.from_dict(unpack_input(root, command["payload"]))
                native_resume = resume_native_goals and operation.stage_id == "coder"
                agent_cancel = self._cancelled_agent_stage_evidence(
                    root, header, sdk, recovery, operation)
                rework_cancel = self._rework_verifier_cancellation_evidence(
                    root, header, sdk, recovery, operation)
                permit = nullcontext()
                if native_resume:
                    snapshot = hydrate_run_snapshot(root, sdk.get_run(run_id))
                    admission = {}
                    interrupted = [row.execution.command.execution_id for row in recoveries]
                    if not self._memory_capacity(snapshot, header, admission, "recovery",
                                                 requested_stage=operation.stage_id,
                                                 exclude_execution_ids=interrupted):
                        import warnings
                        warnings.warn("native goal recovery deferred: insufficient memory "
                                      "or coder capacity; retry recover when capacity is available",
                                      RuntimeWarning)
                        break
                    if self.handlers is None:
                        from .memory_leases import memory_permit

                        def still_interrupted():
                            return any(row.effect.effect_id == effect.effect_id
                                       and row.effect.revision == effect.revision
                                       for row in sdk.inspect_recoveries(run_id))

                        permit = memory_permit(operation, policy=self.memory_policy,
                            probe=self.memory_probe, is_active=still_interrupted)
                # Take resources before workspace locks, as live handlers do.
                # A waiting recovery must not hold a workspace needed by a
                # worker that already owns a memory reservation.
                with permit, workspace_lock(operation_lock(root, operation), blocking=False):
                    if cancel_interrupted_research and operation.stage_id in BUDGETED_RESEARCH_STAGES:
                        from .kernel_runtime import cancel_incomplete_research
                        response = cancel_incomplete_research(root, command, effect)
                    elif native_resume:
                        from .kernel_runtime import resume_interrupted_goal
                        response = resume_interrupted_goal(root, command, effect)
                    elif rework_cancel is not None:
                        from .kernel_runtime import cancel_incomplete_rework_verification
                        response = cancel_incomplete_rework_verification(
                            root, command, effect, rework_cancel)
                    elif agent_cancel is not None:
                        from .kernel_runtime import cancel_incomplete_agent_stage
                        response = cancel_incomplete_agent_stage(
                            root, command, effect, agent_cancel)
                    elif (operation.stage_id == 'final_cleanup'
                            and self._final_cleanup_enabled(header)
                            and not app.get('user_cancelled') and app.get('stop_reason') != 'user_cancelled'):
                        from .kernel_runtime import recover_final_cleanup
                        response = recover_final_cleanup(root, command, effect, runtime.kernel)
                    else:
                        response = reconcile_receipt(root, command, effect)
                    sdk.resolve_effect(effect.effect_id, decision="applied", response=response,
                        expected_revision=effect.revision, recovery_id=f"receipt:{effect.effect_id}:{digest(response)}")
                    sdk.sync()
            self.tick(sdk, header)
            return MigrationRun(run_id, root,
                                hydrate_run_snapshot(root, sdk.get_run(run_id)))

    @staticmethod
    def _drain_result_delivery(sdk, *, owner="modport-reopen"):
        """Settle SDK 0.6 Kernel and application result delivery before reopen."""
        # The public pump first imports Kernel outbox facts.  Claims are then
        # acknowledged through the public fenced result API; no SDK database
        # tables are touched directly.
        for _ in range(128):
            sdk.pump_results(limit=1000, lease_seconds=60.0)
            claims = sdk.claim_results(owner=owner, lease_seconds=60.0, limit=1000)
            if not claims:
                break
            for claim in claims:
                sdk.acknowledge_result(claim["result"]["result_id"],
                                       lease_id=claim["lease_id"], fence=claim["fence"])

    @staticmethod
    def _reopen_application(root, state, *, stage, deadline_epoch, max_agent_assignments):
        """Prepare one bounded same-Run repair attempt without editing history."""
        if stage != "contract_diagnose":
            raise ValueError("same-Run reopen currently supports contract_diagnose only")
        app = json_copy(state.get("application_state") or {})
        require_settled_review_rework(app)
        if (app.get("effective", {}).get("contract_freeze", {}).get("status") == "completed"
                or app.get("locked_artifacts", {}).get("contract_lock_sha256")
                or app.get("locked_artifacts", {}).get("contract_sha256")):
            raise ValueError("contract diagnosis recovery cannot replace a frozen contract")
        context = app.get("repair_context")
        if not isinstance(context, dict) or context.get("repair_scope") != "contract":
            raise ValueError("contract diagnosis recovery requires an existing contract repair context")
        if not isinstance(context.get("current_failure"), dict):
            raise ValueError("contract diagnosis recovery requires the current failure context")
        for ref in context.get("artifact_refs", {}).values():
            verified_path(root, ref)
        if app.get("support_pending") or app.get("administrator_wait"):
            raise ValueError("settle support and administrator work before reopening")
        effective = app.setdefault("effective", {})
        current = effective.get(stage, {})
        format_feedback = app.get('format_context')
        if not isinstance(format_feedback, dict) or format_feedback.get('stage') != stage:
            format_feedback = None
        attempts = state.get('tasks', {}).get(stage, {}).get('attempts', [])

        def current_cycle(attempt):
            operation = attempt.get('command', {}).get('payload', {})
            payload = operation.get('payload', {})
            previous_context = payload.get('repair_context')
            return (isinstance(previous_context, dict)
                    and all(previous_context.get(key) == context.get(key)
                            for key in ('repair_scope', 'repair_generation', 'failure_execution_id'))
                    and ('repair_generation' not in payload
                         or payload['repair_generation'] == context.get('repair_generation')))

        # A projected correction normally already belongs to this cycle. Drop
        # it only when its recorded assignment proves that it belongs elsewhere.
        if format_feedback is not None:
            previous = next((attempt for attempt in attempts
                if attempt.get('command', {}).get('execution_id') == format_feedback.get('execution_id')), None)
            if previous is not None and not current_cycle(previous):
                format_feedback = None
        # Recovery changes execution identity, not the model's latest known
        # formatting mistake. Older repair cycles cannot supply a correction
        # for this diagnosis; keep the original build failure separately.
        for attempt in reversed(attempts):
            if not current_cycle(attempt):
                continue
            prior = (attempt.get('result') or {}).get('value')
            if (isinstance(prior, dict) and prior.get('stage_id') == stage
                    and prior.get('status') == 'completed'):
                # A successful diagnosis already resolved any older format
                # rejection in this cycle, even if later re-entry was interrupted.
                format_feedback = None
                break
            if (isinstance(prior, dict) and prior.get('stage_id') == stage
                    and prior.get('status') == 'failed'
                    and prior.get('error_code') in {'planning_output_invalid', 'repair_output_invalid'}):
                format_feedback = {'stage': stage, 'execution_id': prior['command_id'],
                                   'result': json_copy(prior)}
                break
        early_failure = app.setdefault("early_failures", {}).get(stage)
        # A generation can be deliberately settled after a deployment change
        # (for example, a queued attempt that never ran).  Its prior recovery
        # context is still the authoritative failure input even though the
        # previous generation removed the stage from ``effective``.  Permit a
        # fresh, explicitly recorded recovery in that narrow case.
        retrying_recovery = (
            app.get("early_active") is True
            and isinstance(context.get("current_failure"), dict)
            and context.get("failure_execution_id")
        )
        if current.get("status") not in {"blocked", "failed"} and early_failure is None and not retrying_recovery:
            raise ValueError("contract_diagnose is not the failed stage selected for recovery")
        invalidate_contract_tail(app, include_diagnosis=True)
        app.update({
            "active_stage": None,
            "active_group": None,
            "early_active": True,
            "early_pending": [stage],
            "gap_pending": [],
            "stop_reason": None,
            "stop_state": None,
            "terminal_reason": None,
            "cancel_sent": [],
            "format_context": json_copy(format_feedback),
            "rework_context": json_copy(context["current_failure"]),
            "recovery_deadline_epoch": deadline_epoch,
            "recovery_budget_override": {"max_agent_assignments": max_agent_assignments},
        })
        app.setdefault("early_rework", {})["contract"] = json_copy(context["current_failure"])
        app.pop("repair_evidence_error", None)
        return app

    @staticmethod
    def _prompt_reuse_descriptor(root, state, *, stage):
        """Describe a complete prior prompt cache for an interrupted retry."""
        if stage != "contract_diagnose":
            return None
        task = state.get("tasks", {}).get(stage)
        attempts = task.get("attempts", ()) if isinstance(task, dict) else ()
        if not attempts:
            return None
        # A later retry can be interrupted before it writes compression
        # metadata.  Walk backwards to the newest complete cache instead of
        # letting that partial attempt hide an earlier verified one.
        for attempt in reversed(attempts):
            command = attempt.get("command", {})
            execution_id = command.get("execution_id")
            if not isinstance(execution_id, str) or not execution_id.strip():
                continue
            base = root / "artifacts" / "executions" / execution_id
            paths = {
                "source": base / "prompt-compression" / "source.txt",
                "metadata": base / "prompt-compression.json",
                "compressed": base / "prompt-compression" / "compressed.txt",
            }
            descriptor = {"execution_id": execution_id}
            valid = True
            for key, path in paths.items():
                try:
                    relative = path.relative_to(root).as_posix()
                except ValueError:
                    valid = False
                    break
                if (path.is_symlink() or not path.is_file()
                        or path.resolve() != path.absolute()
                        or not path.resolve().is_relative_to(root.resolve())):
                    valid = False
                    break
                descriptor[key] = relative
                descriptor[key + "_sha256"] = file_digest(path)
            if valid:
                return descriptor
        return None

    def reopen(self, run_dir, run_id, *, stage="contract_diagnose", reason=None,
               additional_seconds=86400, max_agent_assignments=80):
        """Reopen one settled failed stage through SDK 0.6's public API.

        This is intentionally narrower than a generic state editor: the only
        supported route retries contract diagnosis while retaining the frozen
        Run input, cumulative counters and sealed repair evidence.
        """
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("same-Run reopen requires a reason")
        if type(additional_seconds) is not int or additional_seconds <= 0:
            raise ValueError("additional_seconds must be a positive integer")
        if type(max_agent_assignments) is not int or max_agent_assignments <= 0:
            raise ValueError("max_agent_assignments must be a positive integer")
        if stage != "contract_diagnose":
            raise ValueError("same-Run reopen currently supports contract_diagnose only")
        root = Path(run_dir).resolve()
        self._header(root, run_id)
        check_storage_budget(root, phase="recover-reopen")
        # The public SDK has no readonly generation lookup. Validate the bounded
        # prepared packet set before opening any writer; identity/revision checks
        # below select and revalidate the authoritative packet inside the session.
        packets = root / "artifacts" / "recoveries"
        if packets.exists():
            count = 0
            for candidate in packets.glob(f"reopen:{stage}:g*/prepared.json"):
                count += 1
                if count > 4096:
                    raise ValueError("recovery packet inspection exceeds its bound")
                packet = read_prepared_json(root, candidate.relative_to(root).as_posix())
                if packet.get("run_id") == run_id:
                    _verify_recovery_payload(root, packet)
        command_id = None
        with workspace_lock(root / ".locks" / "continuation", blocking=False), self.session(
                root, run_id, allow_terminal_deployment=True) as (root, header, runtime, sdk):
            header = self._header(root, run_id)
            if read_json(root / "run.json")["run_id"] != run_id:
                raise ValueError("only the current segment may be reopened")
            from .workflow_upgrade import validate_upgrade_definition
            if validate_upgrade_definition(header) != header["definition"]:
                raise ValueError("same-Run reopen cannot change workflow definition; use continue --upgrade-workflow")
            if (root / "artifacts" / "continuations" / run_id / "successor.json").exists():
                raise ValueError("Run already has a prepared successor; replay its continuation")
            self._audit_action(root, run_id, "reopen")
            runtime.reap()
            sdk.sync()
            state = hydrate_run_snapshot(root, sdk.get_run(run_id))
            next_generation = int(state.get("generation", 0)) + 1
            command_id = f"reopen:{stage}:g{next_generation}"
            summary = sdk.get_run_summary(run_id)
            latest_recovery = None
            if summary.get("recovery_id"):
                latest_recovery = sdk.get_recovery(summary["recovery_id"])
            # A caller may safely replay after a crash between SDK commit and
            # host return.  The immutable command identity is the proof.
            if (state["state"] == "running" and latest_recovery
                    and latest_recovery.get("command_id") == f"reopen:{stage}:g{state.get('generation', 0)}"
                    and latest_recovery.get("status") == "activated"):
                active_id = latest_recovery["command_id"]
                active_packet = read_prepared_json(root, f"artifacts/recoveries/{active_id}/prepared.json")
                if (active_packet.get("run_id") != run_id or active_packet.get("command_id") != active_id
                        or active_packet.get("stage") != stage or active_packet.get("reason") != reason
                        or active_packet.get("additional_seconds") != additional_seconds
                        or active_packet.get("max_agent_assignments") != max_agent_assignments
                        or active_packet.get("target_generation") != state.get("generation")
                        or active_packet.get("target_deployment") != latest_recovery.get("target_deployment")):
                    raise ValueError("recovery replay differs from the persisted prepared request")
                _verify_recovery_payload(root, active_packet)
                manifest = latest_recovery.get("manifest") or {}
                if (active_packet.get("decision") != latest_recovery.get("decision")
                        or active_packet.get("application_state")
                        != latest_recovery.get("application_state")
                        or active_packet.get("operations") != manifest.get("operations")):
                    raise ValueError("recovery packet differs from the immutable SDK decision")
                sdk.flush()
                sdk.sync()
                state = hydrate_run_snapshot(root, sdk.get_run(run_id))
                self._project_gap_state(self._execution_header(header, state, runtime, sdk), state)
                return MigrationRun(run_id, root, state)
            if state["state"] not in {"failed", "cancelled"}:
                raise ValueError("same-Run reopen requires a settled failed or cancelled Run")
            if state["state"] == "cancelled":
                raise ValueError("same-Run reopen of a cancelled Run requires separate cancellation authorization")
            preflight = sdk.inspect_reopen(run_id, expected_revision=state["revision"],
                                           expected_generation=state.get("generation", 0))
            if not preflight["complete"]:
                if all(item["code"] in {"pending_result_delivery", "kernel_result_not_settled"}
                       for item in preflight["blockers"]):
                    self._drain_result_delivery(sdk)
                    preflight = sdk.inspect_reopen(
                        run_id, expected_revision=state["revision"],
                        expected_generation=state.get("generation", 0))
            if not preflight["complete"]:
                codes = ", ".join(item["code"] for item in preflight["blockers"])
                raise ValueError(f"same-Run reopen blocked by SDK preflight: {codes}")
            from .handlers import preflight_opencode_host
            if self.handlers is None and self.isolation_mode == "process":
                # This must happen before _schedule increments the assignment
                # counter or SDK receives the reopen transaction.
                model, effort = agent_model_policy(header["definition"]["workflow_version"], model_policy=header.get("model_policy"))
                preflight_opencode_host(root, model=model, reasoning_effort=effort)
            packet_path = root / "artifacts" / "recoveries" / command_id / "prepared.json"
            if packet_path.is_symlink() or packet_path.resolve() != packet_path.absolute():
                raise ValueError("unsafe recovery packet path")
            prior = None
            if packet_path.exists():
                prior = read_prepared_json(root, packet_path.relative_to(root).as_posix())
                if (prior.get("run_id") != run_id or prior.get("command_id") != command_id
                        or prior.get("stage") != stage or prior.get("reason") != reason
                        or prior.get("source_revision") != state["revision"]
                        or prior.get("source_generation") != state.get("generation", 0)
                        or prior.get("additional_seconds") != additional_seconds
                        or prior.get("max_agent_assignments") != max_agent_assignments):
                    raise ValueError("recovery replay differs from the persisted prepared request")
                _verify_recovery_payload(root, prior)
                deadline_epoch = prior["deadline_epoch"]
                target_deployment = prior["target_deployment"]
                decision = prior["decision"]
                stored_app = prior["application_state"]
                operations = prior["operations"]
                if target_deployment.get("registry_revision") != runtime.registry_revision:
                    raise ValueError("prepared recovery deployment changed")
            else:
                now = self.clock()
                workflow_version = header.get("definition", {}).get("workflow_version", 0)
                historical_wait = (state["application_state"].get("administrator_wait_seconds", 0)
                                   if workflow_version < 19 else 0)
                deadline_epoch = now + additional_seconds - historical_wait
                app = self._reopen_application(root, state, stage=stage,
                                                deadline_epoch=deadline_epoch,
                                                max_agent_assignments=max_agent_assignments)
                prompt_reuse = self._prompt_reuse_descriptor(root, state, stage=stage)
                target_identity = inspect_runtime(root)["module"]
                handler_revisions = {
                    (f"{key[0]}:{key[1]}" if isinstance(key, tuple) and len(key) == 2 else str(key)): value
                    for key, value in runtime.handler_revisions.items()
                }
                target_deployment = {
                    "registry_revision": runtime.registry_revision,
                    "handler_revisions": handler_revisions,
                    "sdk_identity": target_identity,
                }
                execution_header = json_copy(header)
                execution_header["registry_revision"] = runtime.registry_revision
                execution_header["sdk_identity"] = target_identity
                execution_header["deadline_epoch"] = deadline_epoch
                request = json_copy(execution_header["request"])
                request.setdefault("budget", {})["max_agent_assignments"] = max_agent_assignments
                execution_header["request"] = request
                operations = self._schedule(
                    state, execution_header, app, stage, activate=False, dependencies=[],
                    causation_id=app["repair_context"].get("failure_execution_id"),
                    payload={"prompt_reuse": prompt_reuse} if prompt_reuse is not None else None,
                )
                if not any(op["kind"] == "dispatch" for op in operations):
                    raise ValueError("same-Run reopen could not schedule a new diagnosis assignment")
                decision = {
                    "start_stage": stage,
                    "reused_artifacts": sorted(app.get("effective", {})),
                    "invalidated_artifacts": sorted(contract_repair_tail(include_diagnosis=True)),
                    "invalidated_effective_keys": sorted(set(state["application_state"].get("effective", {}))
                                                          - set(app.get("effective", {}))),
                    "budget_change": {"max_agent_assignments": max_agent_assignments},
                    "budget_reason": "user-authorized temporary recovery ceiling",
                    "deadline_change": {"seconds": additional_seconds, "deadline_epoch": deadline_epoch},
                    "deadline_reason": "user-authorized generation recovery window",
                }
                if prompt_reuse is not None:
                    decision["prompt_reuse"] = prompt_reuse
                stored_app = pack_application_state(root, app)
                decision["prepared_payload_sha256"] = _recovery_payload_digest(
                    stored_app, operations, decision
                )
                packet = {
                    "schema_version": 1,
                    "run_id": run_id,
                    "command_id": command_id,
                    "stage": stage,
                    "reason": reason,
                    "source_revision": state["revision"],
                    "source_generation": state.get("generation", 0),
                    "target_generation": next_generation,
                    "additional_seconds": additional_seconds,
                    "max_agent_assignments": max_agent_assignments,
                    "deadline_epoch": deadline_epoch,
                    "target_deployment": target_deployment,
                    "decision": decision,
                    "application_state": stored_app,
                    "operations": operations,
                }
                atomic_json(packet_path, packet)
                _verify_recovery_payload(root, packet)
            if prior is not None:
                execution_header = json_copy(header)
                execution_header["registry_revision"] = target_deployment["registry_revision"]
                execution_header["sdk_identity"] = target_deployment.get("sdk_identity", header["sdk_identity"])
                execution_header["deadline_epoch"] = deadline_epoch
                request = json_copy(execution_header["request"])
                request.setdefault("budget", {})["max_agent_assignments"] = max_agent_assignments
                execution_header["request"] = request
            sdk.reopen_run(
                run_id,
                command_id=command_id,
                expected_revision=state["revision"],
                expected_generation=state.get("generation", 0),
                actor="modport-host",
                authorization_source="user-approved",
                reason=reason,
                target_deployment=target_deployment,
                decision=decision,
                application_state=stored_app,
                operations=operations,
                owner_id="modport-reopen",
            )
            sdk.flush()
            sdk.sync()
            reopened = hydrate_run_snapshot(root, sdk.get_run(run_id))
            self._project_gap_state(execution_header, reopened)
            return MigrationRun(run_id, root, reopened)

    def build_failure_packet(self, parent: MigrationRun):
        if parent is None:
            raise ValueError("parent is required")
        with self.session(parent.run_dir, parent.run_id, allow_terminal_deployment=True) as (root, header, runtime, sdk):
            state = hydrate_run_snapshot(root, sdk.get_run(parent.run_id))
            if state["state"] not in {"failed", "cancelled"}:
                raise ValueError("retry requires a settled failed or cancelled v2 Run")
            app = state["application_state"] or self._new_application()
            refs = self._refs(header, app)
            stage_results = {}
            for task_id, task in state["tasks"].items():
                for attempt in task["attempts"]:
                    if attempt["state"] not in EXECUTION_TERMINAL:
                        continue
                    command = attempt["command"]
                    execution_id = command["execution_id"]
                    operation = command.get("payload") or {}
                    value = (attempt.get("result") or {}).get("value")
                    row = {"execution_id": execution_id, "task_id": task_id,
                           "stage_id": operation.get("stage_id", task_id),
                           "execution_state": attempt["state"],
                           "result": self._without_repair_context(value) if isinstance(value, dict) else None}
                    if row["result"] is None:
                        row["result_unavailable"] = "settled execution has no business result"
                        row["failure_input"] = self._without_repair_context(operation)
                    stage_results[execution_id] = row
            repair_data = {
                "repair_context": json_copy(app.get("repair_context")),
                "repair_history": json_copy(app.get("repair_history", [])),
                "repair_cycle_results": self._repair_cycle_results(state, app, app["repair_context"])
                if app.get("repair_context") else {},
                "stage_history": json_copy(app.get("history", [])),
                "stage_results": stage_results,
            }
            # Inline documents may contain refs outside their artifact_refs
            # fields. Give every such ref a copy alias, preserving exact ref
            # identity for recursive child remapping (including metadata).
            def include_nested_refs(value):
                if isinstance(value, list):
                    for item in value:
                        include_nested_refs(item)
                elif isinstance(value, dict):
                    if is_repair_artifact_ref(value):
                        refs["parent_context:" + digest(value)] = json_copy(value)
                    else:
                        for item in value.values():
                            include_nested_refs(item)
            include_nested_refs(repair_data)
            for ref in refs.values():
                verified_path(root, ref)
            request = json_copy(header["request"])
            if "source_evidence" in refs:
                source = read_json(verified_path(root, refs["source_evidence"]))
                request["source_revision"] = source["source_commit"]
            if not re.fullmatch(r"[0-9a-fA-F]{40}", str(request.get("source_revision", ""))):
                raise ValueError("retry requires a resolved immutable source commit")
            findings = list(header["prior_findings"])
            for task in state["tasks"].values():
                for attempt in task["attempts"]:
                    result = attempt.get("result") or {}
                    value = result.get("value")
                    if isinstance(value, dict):
                        findings.extend(value.get("outputs", {}).get("prior_findings", []))
                        if value.get("status") != "completed":
                            findings.append({"stage": value["stage_id"], "detail": value["detail"], "error_code": value["error_code"]})
            return {"format_version": 2, "parent_run_id": parent.run_id, "parent_revision": state["revision"],
                    "request": request, "findings": findings, "parent_refs": refs,
                    # These paths are parent-relative provenance. submit supplies
                    # copied_evidence with the same aliases and child-local paths.
                    "source_refs": json_copy(refs),
                    **repair_data}

    def retry(self, parent, *, run_dir, run_id=None, budget_overrides=None, budget_reason=None,
              inherit_harness=False, dependency_cache=None):
        packet = self.build_failure_packet(parent)
        from .retry_policy import apply_budget_overrides
        request = apply_budget_overrides(packet["request"], budget_overrides, budget_reason)
        if dependency_cache is not None:
            from dataclasses import replace
            request = replace(request, dependency_cache=str(Path(dependency_cache).absolute()))
        return self.execute(self.submit(request, run_dir=run_dir, run_id=run_id, parent=parent,
            budget_overrides=budget_overrides, budget_reason=budget_reason, inherit_harness=inherit_harness,
            dependency_cache=dependency_cache))
