"""SDK handler adapter. SQLite, queues, leases and retries belong to the SDK."""
from dataclasses import dataclass, replace
from contextlib import nullcontext
from pathlib import Path
from typing import Any, Callable, Mapping
from dispatcher_sdk.execution_kernel import Kernel, registry_revision
from .contracts import OperationInput, OperationResult
from .workspace import project_path, project_relative
from .telemetry import operation_context, record_event
from .evidence import (atomic_json, digest, file_digest, read_json, seal_ref,
                       workspace_lock)

from .sdk_compat import SDK_VERSION, require_compatible_storage
from .memory_leases import memory_permit
from .memory_admission import MemoryPolicy
from .application_state_storage import ApplicationStateStorageError, _encoded_size
from .payload_storage import pack_result, unpack_input, unpack_result
from .storage_budget import StoragePolicy, check_storage_budget
from .execution_budget import SDK_LEASE_SECONDS, execution_budget, remaining_timeout
from .execution_progress import (mark_current_execution_progress, record_command_progress,
                                track_execution_progress)




def deployment_revision() -> str:
    """Bind opaque handlers to the full deployed ModPort and SDK source."""
    import dispatcher_sdk.execution_kernel as sdk_kernel
    parts = {}
    for label, root in (("modport", Path(__file__).parent),
                        ("dispatcher_sdk", Path(sdk_kernel.__file__).parent.parent)):
        for path in sorted(path for path in root.rglob("*") if path.is_file() and
                           (path.suffix in {".py", ".md", ".json", ".sh", ".js", ".yaml", ".yml", ".toml"}
                            or path.name == "LICENSE") and "__pycache__" not in path.parts):
            parts[f"{label}/{path.relative_to(root).as_posix()}"] = file_digest(path)
    return digest(parts)


def operation_workspace(root: Path, operation: OperationInput) -> Path | None:
    relative = operation.options.get("workspace")
    if relative is None:
        return None
    path = root / relative
    if (not isinstance(relative, str) or Path(relative).is_absolute()
            or ".." in Path(relative).parts or not relative.startswith("workspaces/")
            or path.resolve() != path.absolute()):
        raise ValueError("unsafe isolated workspace")
    return path


_BASELINE_HARNESS_STAGES = frozenset({
    "baseline_build", "contract_draft", "contract_verify", "contract_review",
    "contract_freeze", "contract_restore", "contract_revise",
    "contract_diagnose", "contract_repair_plan", "contract_repair_tasks",
    "contract_repair_review", "contract_repair_integrate",
    "behavior_extract", "behavior_review", "behavior_freeze",
})

# Handlers without an explicit isolated workspace still have narrow, known
# write scopes.  Keep those scopes separate so early preparation can overlap:
# project initialization, skill lookup/publication/scanning, and the baseline
# build all write different paths.  The names are lock namespaces, not source
# paths; the actual files remain protected by the handler's path checks.
_STAGE_LOCK_SCOPES = {
    "background": ".modport/background",
    "preparation": ".modport/preparation",
    "project_init": ".modport/project-init",
    "mod_analysis": ".modport/mod-analysis",
    "gap_research": ".modport/gap-research",
    "research_cleanup": ".modport/research-cleanup",
    "research_review": ".modport/gap-research",
    "admin_review": ".modport/admin-review",
    "gap_plan": ".modport/gap-plan",
    "gap_plan_review": ".modport/gap-plan",
    "knowledge_publish": "artifacts/knowledge-publish",
    "environment": "artifacts/environment",
    "skill_lookup": "artifacts/skill-lookup",
    "skill_publish": "artifacts/skill-publish",
    "skill_resolve": "artifacts/skill-publish",
    "codemod": "worktree",
    "build_prepare": "worktree",
    "early_compile": "worktree",
    "mod_scan": "artifacts/mod-scan",
    "migration_inventory": ".modport/migration-inventory",
    "migration_plan": ".modport/migration-plan",
    "migration_tasks": ".modport/migration-tasks",
    "parallel_review": ".modport/parallel-review",
    "implementation": "worktree",
    "development_integrate": "worktree",
    "code_cleanup": "worktree",
    "final_cleanup": "worktree",
    "development_prepare": "worktree",
    "development_prepare_integrate": "worktree",
    "target_revise": "worktree",
    "target_repair_integrate": "worktree",
    "target_build": "worktree",
    "target_contract_freeze": "worktree",
    "code_review": "worktree",
    "acceptance_preflight": "worktree",
    "acceptance_build": "worktree",
    "client_smoke": "worktree",
    "delivery": "worktree",
}


def operation_lock(root: Path, operation: OperationInput) -> Path:
    """Return the lock for the operation's actual write scope.

    Source setup keeps the Run-wide lock.  Stages with an explicit workspace
    use a lock named after that workspace, rather than their task id: different
    workspaces can run concurrently and two attempts targeting one workspace
    cannot race.  Stages without a workspace use their declared narrow output
    scope.  A task id is not a resource boundary because retries and sibling
    tasks may intentionally use the same output directory.
    """
    from .rework_tools import is_interactive_review
    if is_interactive_review(operation):
        # The caller waits while its requested author writes. Holding the
        # author's workspace lock here would deadlock that synchronous call.
        return root / '.locks' / 'reviews' / operation.command_id
    if operation.stage_id == 'agent_rework':
        from .rework_coder import rework_workspace
        workspace = rework_workspace(operation)
        return root / '.locks' / 'scopes' / ('baseline-harness' if workspace == 'baseline' else workspace)
    if operation.stage_id == "goal_prepare":
        # Each read-only planner writes execution-specific logs/artifacts only.
        from hashlib import sha256
        identity = sha256(operation.task_id.encode()).hexdigest()
        return root / ".locks" / "scopes" / "goal-prepare" / identity
    workspace = operation_workspace(root, operation)
    if workspace is not None:
        relative = project_relative(root, workspace.resolve())
        return root / ".locks" / "scopes" / relative
    if operation.stage_id == "source":
        return root
    if (operation.options.get('workflow_version', 0) >= 19
            and operation.stage_id in {'mod_scan', 'migration_inventory'}):
        return root / '.locks' / 'scopes' / 'worktree'
    if operation.stage_id in _BASELINE_HARNESS_STAGES:
        scope = "baseline-harness"
    else:
        scope = _STAGE_LOCK_SCOPES.get(operation.stage_id)
    if scope is None:
        # Keep unknown non-source stages isolated by stage. This preserves
        # compatibility for extensions while avoiding an accidental global
        # lock that would serialize unrelated work.
        scope = "stage/" + operation.stage_id
    return root / ".locks" / "scopes" / scope


def repository_facts(root: Path, operation: OperationInput | None = None) -> dict:
    """Describe the write scope without hashing the mutable Run tree.

    This metadata is useful when inspecting a receipt, but it deliberately
    does not attempt to prove that a workspace stayed byte-for-byte unchanged.
    Agents are the writers in an isolated VM and changing their assigned files
    is expected.  SDK identity, effect requests and path checks remain the
    authoritative execution and recovery boundaries.
    """
    workspace = operation_workspace(root, operation) if operation is not None else None
    if workspace is None:
        return {"scope": "run", "workspace": None}
    return {"scope": "workspace", "workspace": project_relative(root, workspace.resolve()).as_posix()}


def _contained_file(root: Path, ref: Mapping[str, Any]) -> Path:
    """Validate a reference's location while leaving byte identity to callers.

    Artifact references still have a path and are kept contained in the Run.
    Their optional SHA-256 metadata is for artifact indexing/evidence; it is no
    longer a runtime gate for an agent's mutable working files.
    """
    relative = Path(str(ref.get("path", "")))
    from .workspace import project_path
    path = project_path(root, relative)
    if (not relative.is_absolute() and relative.parts and relative.parts[0] != 'worktree'
            and ".." not in relative.parts and not path.exists()):
        from .artifact_retention import restore_archived_artifact
        restore_archived_artifact(root, relative.as_posix())
    if (relative.is_absolute() or not relative.parts or ".." in relative.parts
            or path.is_symlink() or not path.is_file()
            or path.resolve() != path.absolute()):
        raise ValueError(f"artifact path is not a contained regular file: {relative}")
    return path


def validate_consumed_copies(root: Path, operation: OperationInput):
    """Check canonical input locations without comparing mutable file bytes.

    The host owns the Run and its isolated VM; an agent changing an assigned
    output is ordinary work.  Keep only containment and presence checks here.
    Individual handlers may still apply semantic validation to the data they
    consume (for example, JSON schema or a required source commit).
    """
    from .business_policy import business_gates_disabled
    if business_gates_disabled(operation):
        # v17 consumes the available execution-specific observations. Requiring
        # older canonical report files would recreate a report-presence gate.
        # Recheck the actual references at the execution boundary instead.
        from .evidence import verified_path
        for ref in operation.artifact_refs.values():
            verified_path(root, ref)
        return
    canonical = {
        "source_evidence": "artifacts/source.json",
        "locked_manifest": "artifacts/locked-manifest.json",
        "functional_contract_lock": "artifacts/functional-contract.lock.json",
        "acceptance_rubric": "artifacts/acceptance-rubric.json",
    }
    if operation.stage_id in {"contract_review", "contract_freeze"}:
        canonical["baseline_contract_tests_candidate"] = "artifacts/baseline-contract-tests.json"
    if operation.stage_id == "contract_freeze":
        canonical["contract_review"] = "baseline/.modport/contract-review.json"
    for key, path in canonical.items():
        if key in operation.artifact_refs:
            _contained_file(root, {"path": path})
    if operation.stage_id in {"contract_verify", "contract_review", "contract_freeze"}:
        producers = operation.upstream_results
        producer = producers.get("contract_revise", producers.get("contract_restore", producers.get("contract_draft", {})))
        refs = producer.get("outputs", {}).get("artifact_refs", {})
        candidate = next((ref for key, ref in refs.items()
                          if key.endswith(":.modport/functional-contract.json")), None)
        path = "baseline/.modport/functional-contract.json"
        if candidate is not None:
            _contained_file(root, {"path": path})
        # A candidate may already exist while an upstream result is being
        # repaired.  Its current contents are consumed by the next handler;
        # missing/invalid content is reported by that handler's schema checks.


def _response_bytes(response: dict) -> int:
    """Bound and measure a logical result before any durable result write."""
    try:
        return _encoded_size(response)
    except ApplicationStateStorageError as exc:
        raise ValueError(str(exc).replace("application state", "operation result")) from exc


def validate_stage_response(root: Path, operation: OperationInput, response: dict, expected_request: dict):
    directory = root / "artifacts" / "executions" / operation.command_id
    if directory.resolve() != directory.absolute():
        raise ValueError("stage receipt directory is not contained")
    receipt_path = directory / "receipt.json"
    if (not receipt_path.is_file() or receipt_path.is_symlink()
            or receipt_path.resolve() != receipt_path.absolute()):
        raise ValueError("no complete stage receipt; external changes need manual reconciliation")
    receipt = read_json(receipt_path)
    logical_response = unpack_result(root, response)
    receipt_response = unpack_result(root, receipt.get("response"))
    if (receipt.get("execution_id") != operation.command_id
            or receipt.get("effect_request") != expected_request
            or receipt_response != logical_response):
        raise ValueError("stage receipt does not match the frozen operation/response")
    result = OperationResult.from_dict(logical_response)
    result.validate_for(operation)
    for ref in result.outputs.get("artifact_refs", {}).values():
        _contained_file(root, ref)
    return result.to_dict()


@dataclass
class SDKHandler:
    handler: Callable[[OperationInput], OperationResult]
    __execution_kernel_revision__: str
    enforce_memory: bool = True
    memory_policy: MemoryPolicy | None = None
    storage_policy: StoragePolicy | None = None
    now: Callable[[], float] | None = None

    def __call__(self, payload: Mapping[str, Any], context):
        # Start the local budget before hydration, memory admission or lock
        # acquisition; each consumes part of the SDK's outer execution window.
        with execution_budget(context, now=self.now):
            command = context.command.to_dict()
            lease = getattr(context, "lease", None)
            run_dir = payload.get("run_dir") if isinstance(payload, Mapping) else None
            if isinstance(run_dir, str) and Path(run_dir).is_absolute():
                record_command_progress(Path(run_dir), {"payload": payload,
                    "registry_revision": command.get("registry_revision")}, "worker_entered",
                    kernel_attempt=getattr(lease, "attempt", None),
                    fence=getattr(lease, "fence", None))
            return self._execute(payload, context)

    def _execute(self, payload: Mapping[str, Any], context):
        run_dir = payload.get("run_dir") if isinstance(payload, Mapping) else None
        if not isinstance(run_dir, str) or not Path(run_dir).is_absolute():
            raise ValueError("operation payload must retain an absolute run_dir")
        root = Path(run_dir)
        lease = getattr(context, "lease", None)
        record_command_progress(root, {"payload": payload,
            "registry_revision": getattr(context.command, "registry_revision", None)}, "input_loading",
            kernel_attempt=getattr(lease, "attempt", None),
            fence=getattr(lease, "fence", None))
        # Admission precedes blob hydration so an over-budget Run cannot incur a
        # large decode before the business handler is rejected.
        check_storage_budget(
            root, policy=self.storage_policy, phase="kernel-handler:before"
        )
        operation = OperationInput.from_dict(unpack_input(root, dict(payload)))
        if (context.command.execution_id != operation.command_id
                or context.command.correlation_id != operation.run_id
                or context.command.handler_id != f"modport.{operation.stage_id}"):
            raise ValueError("execution and ModPort operation identity disagree")
        stage_policy = (self.memory_policy or MemoryPolicy.from_env()).for_stage(operation.stage_id)
        with track_execution_progress(root, operation, context,
                                      getattr(context.command, "registry_revision", None)):
            def memory_wait(sample):
                mark_current_execution_progress("waiting_memory", wait=sample)

            # Host and worker use the same stage mapping. Bound the wait by the
            # live SDK/run work deadline and return an explicit failure rather
            # than consuming the full author timeout in a resource queue.
            # ``remaining_timeout`` is bound to the ModPort operation identity
            # while an execution budget is active. The SDK command intentionally
            # exposes ``execution_id`` instead of ModPort's ``command_id``.
            stage_wait = (remaining_timeout(operation, 120)
                          if hasattr(context.command, "timeout_seconds") else 120)
            admission = (memory_permit(operation, is_active=context.effects.is_active,
                policy=stage_policy, wait_seconds=5,
                max_wait_seconds=stage_wait,
                observer=memory_wait)
                if self.enforce_memory and stage_policy is not None else nullcontext())
            mark_current_execution_progress("restoring_inputs")
            # Cold immutable inputs are restored before taking a workspace lock;
            # source handlers use the Run lock that the restore operation also needs.
            for ref in operation.artifact_refs.values():
                _contained_file(root, ref)
            mark_current_execution_progress("waiting_memory")
            with admission:
                if not context.effects.is_active():
                    raise RuntimeError("execution authority expired while waiting for memory")
                mark_current_execution_progress("waiting_workspace")

                def workspace_wait(sample):
                    mark_current_execution_progress("waiting_workspace", wait=sample)

                workspace_timeout = (remaining_timeout(operation, 120)
                                     if hasattr(context.command, "timeout_seconds") else 120)
                with workspace_lock(operation_lock(root, operation),
                                    timeout_seconds=workspace_timeout,
                                    on_wait=workspace_wait):
                    if not context.effects.is_active():
                        raise RuntimeError("execution authority expired while waiting for the workspace")
                    for ref in operation.artifact_refs.values():
                        _contained_file(root, ref)
                    validate_consumed_copies(root, operation)
                    evidence_dir = root / "artifacts" / "executions" / operation.command_id
                    effect_request = {"input_sha256": digest(operation.to_dict()),
                                      "run_dir": str(root), "stage": operation.stage_id}

                    def perform_stage():
                        atomic_json(evidence_dir / "input.json", operation.to_dict())
                        before_facts = repository_facts(root, operation)
                        atomic_json(evidence_dir / "before.json", before_facts)
                        result = self.handler(operation)
                        mark_current_execution_progress("settling")
                        if not isinstance(result, OperationResult):
                            raise TypeError("business handler must return OperationResult")
                        result.validate_for(operation)
                        outputs = dict(result.outputs)
                        refs = dict(outputs.get("artifact_refs", {}))
                        for field in ("log", "last_message"):
                            relative = outputs.get(field)
                            if isinstance(relative, str) and (root / relative).is_file():
                                refs.setdefault(f"{operation.stage_id}:{field}", {
                                    "path": relative, "sha256": file_digest(root / relative), "media_type": "text/plain"})
                        if operation.stage_id == "delivery" and (root / "artifacts/delivery-report.json").is_file():
                            path = root / "artifacts/delivery-report.json"
                            refs["delivery_report"] = {"path": "artifacts/delivery-report.json", "sha256": file_digest(path),
                                                       "media_type": "application/json"}
                            for index, jar in enumerate(outputs.get("jars", [])):
                                refs[f"delivery_jar:{index}"] = jar
                        outputs["artifact_refs"] = {key: seal_ref(root, ref, execution_id=operation.command_id)
                                                    for key, ref in refs.items()}
                        result = replace(result, outputs=outputs)
                        response = result.to_dict()
                        pending_bytes = _response_bytes(response)
                        # Once the business handler has run, its logical receipt is the
                        # recovery authority. Publish it before a storage-budget pause
                        # can prevent the SDK Effect/result projections from growing.
                        receipt = {"execution_id": operation.command_id, "effect_request": effect_request,
                                   "response": response, "after": repository_facts(root, operation)}
                        atomic_json(evidence_dir / "receipt.json", receipt)
                        check_storage_budget(
                            root, policy=self.storage_policy,
                            phase=f"kernel-handler:{operation.stage_id}:effect-result",
                            pending_bytes=pending_bytes,
                        )
                        stored_response = pack_result(root, response)
                        return response, stored_response

                    def perform():
                        with operation_context(operation) as audit_identity:
                            response, stored_response = perform_stage()
                            record_event(root, audit_identity["invocation_id"] + ":result", "operation.result",
                                         response, status=response["status"])
                            return stored_response

                    # This marker is intentionally strict: no application
                    # handler/effect may start unless the durable boundary is
                    # observable, so its absence remains useful recovery proof.
                    mark_current_execution_progress("handler_entered", strict=True)
                    response = context.effects.execute_once(
                        f"modport:{operation.command_id}", "modport.stage", effect_request, perform)
                    logical_response = validate_stage_response(
                        root, operation, response, effect_request
                    )
                    check_storage_budget(
                        root, policy=self.storage_policy,
                        phase=f"kernel-handler:{operation.stage_id}:execution-result",
                        pending_bytes=_response_bytes(logical_response),
                    )
                    mark_current_execution_progress("finished")
                    return pack_result(root, logical_response)


def sdk_handlers(handlers=None, *, memory_policy=None,
                 storage_policy=None, now=None) -> dict[str, SDKHandler]:
    revision = deployment_revision()
    production = handlers is None
    if handlers is None:
        from .handlers import build_registry
        handlers = build_registry()
    else:
        revision = digest({"deployment": revision, "handlers": registry_revision(handlers)})
    # Custom registries are the repository's deterministic fixture interface;
    # their handlers do not launch the production Codex/build processes.
    return {name: SDKHandler(handler, f"{revision}:{name}", enforce_memory=production,
                            memory_policy=memory_policy, storage_policy=storage_policy,
                            now=now)
            for name, handler in handlers.items()}


def open_runtime(root: Path, *, handlers=None, isolation_mode="process", now=None,
                 memory_policy=None, storage_policy=None):
    root = Path(root).resolve()
    check_storage_budget(root, policy=storage_policy, phase="kernel-open")
    require_compatible_storage(root)
    kernel_path = root / "kernel.sqlite3"
    cancellation_source = "modport:" + digest({
        "schema": "modport-cancellation-source.v1",
        "kernel_path": str(kernel_path),
    })
    return Kernel.open_sqlite(root / "kernel.sqlite3", sdk_handlers(
        handlers, memory_policy=memory_policy, storage_policy=storage_policy, now=now),
                             isolation_mode=isolation_mode, now=now,
                             lease_seconds=SDK_LEASE_SECONDS,
                             cancellation_journal_path=str(
                                 root / ".modport" / "execution-cancellation.sqlite3"),
                             source_id=cancellation_source)


def reconcile_receipt(root: Path, command: dict, effect) -> dict:
    """Return a proved applied response, or leave uncertain work parked."""
    operation = OperationInput.from_dict(unpack_input(root, command["payload"]))
    if root.resolve() != Path(operation.run_dir).resolve():
        raise ValueError("recovery workspace does not match the command")
    receipt_path = root / "artifacts" / "executions" / operation.command_id / "receipt.json"
    if not receipt_path.is_file() or receipt_path.is_symlink():
        raise ValueError("no complete stage receipt; external changes need manual reconciliation")
    receipt = read_json(receipt_path)
    expected_request = {"input_sha256": digest(operation.to_dict()), "run_dir": str(root), "stage": operation.stage_id}
    if effect.request != expected_request:
        raise ValueError("recovery receipt does not match the frozen operation")
    response = validate_stage_response(
        root, operation, receipt["response"], expected_request
    )
    check_storage_budget(
        root, phase=f"kernel-recovery:{operation.stage_id}:effect-result",
        pending_bytes=_response_bytes(response),
    )
    return pack_result(root, response)


def cancel_incomplete_research(root: Path, command: dict, effect) -> dict:
    """Record interrupted research as failed work, never a successful report.

    The caller holds the stage lock and resolves the SDK effect explicitly.
    A complete receipt always takes precedence and is recovered normally.
    """
    from .research_policy import RESEARCH_STAGES
    operation = OperationInput.from_dict(unpack_input(root, command['payload']))
    if operation.stage_id not in RESEARCH_STAGES:
        raise ValueError('only research assignments support partial-output cancellation')
    directory = root / 'artifacts' / 'executions' / operation.command_id
    receipt = directory / 'receipt.json'
    if receipt.exists():
        return reconcile_receipt(root, command, effect)
    expected = {'input_sha256': digest(operation.to_dict()), 'run_dir': str(root), 'stage': operation.stage_id}
    if effect.request != expected:
        raise ValueError('research recovery operation identity mismatch')
    note = directory / 'interrupted-research.json'
    atomic_json(note, {'execution_id': operation.command_id, 'status': 'cancelled',
                       'artifacts_complete': False, 'reason': 'explicit_recovery_cancellation',
                       'workspace': operation.options.get('workspace', 'baseline/.modport/gap-research')})
    response = OperationResult('failed', operation.run_id, operation.task_id, operation.stage_id,
        operation.command_id, outputs={'research_cancelled': True, 'artifacts_complete': False,
            'artifact_refs': {'interrupted_research': {'path': note.relative_to(root).as_posix()}}},
        error_code='research_interrupted', detail='partial research retained; no findings accepted').to_dict()
    atomic_json(receipt, {'execution_id': operation.command_id, 'effect_request': expected,
                         'response': response, 'after': repository_facts(root, operation)})
    check_storage_budget(
        root, phase=f"kernel-recovery:{operation.stage_id}:effect-result",
        pending_bytes=_response_bytes(response),
    )
    return pack_result(root, response)


def cancel_incomplete_coder(root: Path, command: dict, effect) -> dict:
    """Settle a reaped user-cancelled coder without accepting partial work.

    The caller must have verified the durable SDK cancellation decision and
    confirmed local process cleanup. This receipt describes the interrupted
    stage outcome, not the completeness or rollback of workspace side effects.
    """
    operation = OperationInput.from_dict(unpack_input(root, command["payload"]))
    if operation.stage_id != "coder":
        raise ValueError("only interrupted coder assignments use this cancellation receipt")
    if Path(operation.run_dir).resolve() != root.resolve():
        raise ValueError("coder cancellation workspace does not match the command")
    if (command.get("handler_id") != "modport.coder"
            or command.get("execution_id") != operation.command_id
            or effect.effect_id != f"modport:{operation.command_id}"
            or effect.name != "modport.stage"):
        raise ValueError("interrupted coder handler/effect identity mismatch")
    directory = root / "artifacts" / "executions" / operation.command_id
    if directory.resolve() != directory.absolute():
        raise ValueError("interrupted coder receipt directory is not contained")
    receipt = directory / "receipt.json"
    if receipt.exists() or receipt.is_symlink():
        return reconcile_receipt(root, command, effect)
    expected = {"input_sha256": digest(operation.to_dict()), "run_dir": str(root),
                "stage": operation.stage_id}
    if effect.request != expected:
        raise ValueError("coder cancellation effect request differs from the frozen operation")
    workspace = operation_workspace(root, operation)
    if workspace is None or not workspace.is_dir() or workspace.is_symlink():
        raise ValueError("interrupted coder has no contained isolated workspace")
    relative_workspace = workspace.relative_to(root).as_posix()
    note = directory / "interrupted-coder.json"
    note_data = {
        "schema": "modport.interrupted-coder.v1",
        "execution_id": operation.command_id,
        "effect_id": effect.effect_id,
        "status": "cancelled",
        "reason": "user_cancelled",
        "workspace": relative_workspace,
        "workspace_changes": "unknown_unaccepted",
        "external_outcome": "unknown",
        "artifacts_complete": False,
        "acceptance_status": "unverified",
    }
    if note.exists() or note.is_symlink():
        if note.is_symlink() or read_json(note) != note_data:
            raise ValueError("interrupted coder note differs from the verified cancellation")
    else:
        atomic_json(note, note_data)
    response = OperationResult(
        "failed", operation.run_id, operation.task_id, operation.stage_id,
        operation.command_id,
        outputs={"coder_cancelled": True, "artifacts_complete": False,
                 "workspace_changes": "unknown_unaccepted", "external_outcome": "unknown",
                 "acceptance_status": "unverified",
                 "artifact_refs": {"interrupted_coder": {
                     "path": note.relative_to(root).as_posix(),
                 }}},
        error_code="coder_interrupted",
        detail="user cancelled coder; isolated partial work retained and unaccepted",
    ).to_dict()
    atomic_json(receipt, {"execution_id": operation.command_id,
                         "effect_request": expected, "response": response,
                         "after": repository_facts(root, operation)})
    check_storage_budget(root, phase=f"kernel-recovery:{operation.stage_id}:effect-result",
                         pending_bytes=_response_bytes(response))
    return reconcile_receipt(root, command, effect)


def cancel_incomplete_progress_assignment(root: Path, command: dict, effect,
                                           evidence: Mapping[str, Any]) -> dict:
    """Settle only a proved supervisor-cancelled assignment as incomplete.

    The caller binds its committed termination ledger to the completed SDK
    supervisor result and proves exact cancellation/revocation/cleanup custody.
    A complete stage receipt takes precedence; this never accepts partial work.
    """
    operation = OperationInput.from_dict(unpack_input(root, command['payload']))
    if (Path(operation.run_dir).resolve() != root.resolve()
            or command.get('handler_id') != f'modport.{operation.stage_id}'
            or command.get('execution_id') != operation.command_id
            or effect.execution_id != operation.command_id
            or effect.effect_id != f'modport:{operation.command_id}'
            or effect.name != 'modport.stage'):
        raise ValueError('progress cancellation handler/effect identity mismatch')
    directory = root / 'artifacts' / 'executions' / operation.command_id
    if directory.resolve() != directory.absolute():
        raise ValueError('progress cancellation receipt directory is not contained')
    receipt = directory / 'receipt.json'
    if receipt.exists() or receipt.is_symlink():
        return reconcile_receipt(root, command, effect)
    expected = dict(effect.request)  # Preserve host-owned Effect provenance unchanged.
    required = {'schema': 'modport.progress-assignment-cancellation.v1',
        'run_id': operation.run_id, 'task_id': operation.task_id,
        'stage_id': operation.stage_id, 'execution_id': operation.command_id,
        'effect_id': effect.effect_id, 'effect_revision': effect.revision,
        'kernel_attempt': effect.attempt, 'fence': effect.fence,
        'external_outcome': 'unknown', 'artifacts_complete': False,
        'acceptance_status': 'unverified'}
    if not isinstance(evidence, Mapping) or any(evidence.get(k) != v for k, v in required.items()):
        raise ValueError('progress cancellation evidence identity is incomplete')
    authority = evidence.get('supervisor_authorization')
    if (not isinstance(authority, Mapping)
            or authority.get('schema') != 'modport.progress-supervisor-termination.v1'
            or authority.get('run_id') != operation.run_id
            or not authority.get('supervisor_task_id')
            or not authority.get('supervisor_execution_id')):
        raise ValueError('progress cancellation supervisor authorization is missing')
    from .progress_supervisor import validate_progress_supervisor_decision
    decision = validate_progress_supervisor_decision(authority.get('decision'), authority.get('request'))
    reason = 'progress_supervisor: ' + decision['reason']
    if (decision['decision'] != 'terminate'
            or decision['target_task_id'] != operation.task_id
            or decision['target_execution_id'] != operation.command_id
            or decision['review_id'] != authority.get('review_id')
            or evidence.get('recovery_reason') != reason
            or authority.get('recovery_reason') != reason):
        raise ValueError('progress cancellation is not bound to this terminated execution')
    proof = evidence.get('proof')
    if (not isinstance(proof, Mapping)
            or any(proof.get(name) != 'confirmed' for name in (
                'request_committed', 'command_delivered', 'execution_authority_revoked',
                'local_process_tree_reaped', 'cleanup'))
            or evidence.get('issues') != [] or evidence.get('effects_truncated') is not False
            or evidence.get('receipts_truncated') is not False):
        raise ValueError('progress cancellation cleanup proof is incomplete')
    note = directory / 'progress-cancelled-assignment.json'
    note_data = {**required, 'recovery_reason': reason, 'proof': dict(proof),
        'supervisor_authorization': dict(authority),
        'cancellation_receipt_ids': list(evidence.get('receipt_ids', [])),
        'workspace': repository_facts(root, operation),
        'partial_outputs': 'retained_unaccepted; no completeness or rollback asserted'}
    if note.is_symlink():
        raise ValueError('progress cancellation note is a symlink')
    if note.exists():
        if read_json(note) != note_data:
            raise ValueError('progress cancellation note differs from the proved cancellation')
    else:
        atomic_json(note, note_data)
    response = OperationResult('failed', operation.run_id, operation.task_id,
        operation.stage_id, operation.command_id,
        outputs={'progress_supervisor_terminated': True, 'agent_cancelled': True,
            'progress_supervisor_review_id': decision['review_id'],
            'external_outcome': 'unknown', 'artifacts_complete': False,
            'acceptance_status': 'unverified', 'partial_outputs_unaccepted': True,
            'workspace_changes': 'unknown_unaccepted',
            'artifact_refs': {'progress_cancelled_assignment': {'path': note.relative_to(root).as_posix()}}},
        error_code='progress_supervisor_terminated',
        detail='supervisor cancelled this execution; partial work retained and unaccepted').to_dict()
    atomic_json(receipt, {'execution_id': operation.command_id, 'effect_request': expected,
                         'response': response, 'after': repository_facts(root, operation)})
    check_storage_budget(root, phase=f'kernel-recovery:{operation.stage_id}:effect-result',
                         pending_bytes=_response_bytes(response))
    return reconcile_receipt(root, command, effect)


def cancel_incomplete_agent_stage(root: Path, command: dict, effect,
                                  evidence: Mapping[str, Any]) -> dict:
    """Record selected interrupted agent stages as failed and unaccepted.

    The caller must prove explicit user cancellation, exact Effect identity,
    process-tree cleanup, and stage-specific dependencies. This receipt keeps
    partial work unaccepted; it never claims workspace rollback or a complete
    agent result.
    """
    operation = OperationInput.from_dict(unpack_input(root, command["payload"]))
    handlers = {
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
    handler_id = handlers.get(operation.stage_id)
    if handler_id is None:
        raise ValueError("this agent stage has no interrupted-agent cancellation receipt")
    if (Path(operation.run_dir).resolve() != root.resolve()
            or command.get("handler_id") != handler_id
            or command.get("execution_id") != operation.command_id
            or effect.execution_id != operation.command_id
            or effect.effect_id != f"modport:{operation.command_id}"
            or effect.name != "modport.stage"):
        raise ValueError("interrupted agent handler/effect identity mismatch")
    directory = root / "artifacts" / "executions" / operation.command_id
    if directory.resolve() != directory.absolute():
        raise ValueError("interrupted agent receipt directory is not contained")
    receipt = directory / "receipt.json"
    if receipt.exists() or receipt.is_symlink():
        return reconcile_receipt(root, command, effect)
    if operation.stage_id in {"code_cleanup", "final_cleanup"}:
        # The proved SDK Effect already owns the frozen request. Preserve its
        # provenance without introducing another content identity check.
        expected = dict(effect.request)
    else:
        expected = {"input_sha256": digest(operation.to_dict()), "run_dir": str(root),
                    "stage": operation.stage_id}
        if effect.request != expected:
            raise ValueError("interrupted agent effect request differs from the frozen operation")

    required = {
        "schema": "modport.interrupted-agent-cancellation.v1",
        "run_id": operation.run_id,
        "task_id": operation.task_id,
        "stage_id": operation.stage_id,
        "execution_id": operation.command_id,
        "effect_id": effect.effect_id,
        "effect_revision": effect.revision,
        "kernel_attempt": effect.attempt,
        "fence": effect.fence,
        "recovery_reason": "user_cancelled",
        "external_outcome": "unknown",
        "artifacts_complete": False,
        "acceptance_status": "unverified",
    }
    if not isinstance(evidence, Mapping) or any(evidence.get(k) != v for k, v in required.items()):
        raise ValueError("interrupted agent cancellation evidence identity is incomplete")
    proof = evidence.get("proof")
    proof_fields = ("request_committed", "command_delivered", "execution_authority_revoked",
                    "local_process_tree_reaped", "cleanup")
    if (not isinstance(proof, Mapping)
            or any(proof.get(name) != "confirmed" for name in proof_fields)
            or evidence.get("issues") != []
            or evidence.get("effects_truncated") is not False
            or evidence.get("receipts_truncated") is not False):
        raise ValueError("interrupted agent cancellation proof is incomplete")

    def path_observation(relative: str) -> dict:
        path = project_path(root, relative)
        try:
            root_path = project_path(root).resolve() if Path(relative).parts[0] == 'worktree' else root.resolve()
            cursor = root_path
            components = Path(relative).parts[1:] if Path(relative).parts[0] == 'worktree' else Path(relative).parts
            for component in components:
                cursor = cursor / component
                if cursor.is_symlink():
                    return {"path": relative, "state": "symlink_untrusted"}
            if (path.resolve(strict=False) != path.absolute()
                    or not path.resolve(strict=False).is_relative_to(root_path)):
                return {"path": relative, "state": "outside_workspace_untrusted"}
        except OSError:
            return {"path": relative, "state": "unreadable_untrusted"}
        if not path.exists():
            return {"path": relative, "state": "missing"}
        if path.is_file():
            if operation.stage_id in {"code_cleanup", "final_cleanup"}:
                return {"path": relative, "state": "untrusted_file",
                        "size": path.stat().st_size}
            return {"path": relative, "state": "untrusted_file",
                    "size": path.stat().st_size, "sha256": file_digest(path)}
        if path.is_dir():
            return {"path": relative, "state": "untrusted_directory"}
        return {"path": relative, "state": "unsupported_entry"}

    partial = {}
    prepared_supervisor = None
    if operation.stage_id == "contract_draft":
        partial = {
            "disposition": "retained_unaccepted; do not use as a handoff baseline",
            "workspace": "baseline/.modport",
            "observations": [path_observation(path) for path in (
                "baseline/.modport/functional-contract.json",
                "baseline/.modport/characterization.init.gradle",
                "baseline/.modport/harness",
            )],
        }
    elif operation.stage_id == "agent_rework":
        rework = operation.payload.get("reviewer_rework")
        reviewer_id = operation.payload.get("reviewer_execution_id")
        request_id = rework.get("request_id") if isinstance(rework, Mapping) else None
        if (not isinstance(request_id, str) or not request_id
                or not isinstance(reviewer_id, str) or not reviewer_id
                or operation.task_id != f"agent-rework.{request_id}"):
            raise ValueError("interrupted coder rework lacks its reviewer request identity")
        from .rework_coder import rework_workspace
        workspace = rework_workspace(operation)
        partial = {
            "disposition": "candidate changes retained but unaccepted; no rollback is asserted",
            "reviewer_execution_id": reviewer_id,
            "request_id": request_id,
            "workspace": path_observation(workspace),
            "candidate_workspace": path_observation("workspaces/development"),
            "execution_artifacts": path_observation(
                f"artifacts/executions/{operation.command_id}"),
        }
    elif operation.stage_id == "contract_verify":
        partial = {
            "disposition": "verification output is incomplete and untrusted",
            "workspace": path_observation("baseline"),
            "execution_artifacts": path_observation(
                f"artifacts/executions/{operation.command_id}"),
        }
    elif operation.stage_id in {"code_cleanup", "final_cleanup"}:
        partial = {
            "disposition": "cleanup changes retained and unaccepted; integration and rollback are unknown",
            "workspace": path_observation("worktree"),
            "cleanup_workspaces": path_observation("workspaces/code-cleanup"),
            "execution_artifacts": path_observation(f"artifacts/executions/{operation.command_id}"),
            "final_cleanup_evidence": path_observation("artifacts/final-cleanup"),
        }
    elif operation.stage_id in {"artifact_test_design", "artifact_test_execute", "behavior_extract", "behavior_review"}:
        partial = {"disposition": "partial output retained and unaccepted; external outcome unknown",
                   "execution_artifacts": path_observation(f"artifacts/executions/{operation.command_id}")}
    elif operation.stage_id == "contract_review":
        report = path_observation("baseline/.modport/contract-review.json")
        requests = evidence.get("review_rework_requests")
        if not isinstance(requests, list):
            raise ValueError("interrupted contract review lacks its child-task settlement evidence")
        for row in requests:
            if (not isinstance(row, Mapping) or row.get("reviewer_execution_id") != operation.command_id
                    or not isinstance(row.get("request_id"), str)
                    or not isinstance(row.get("task_id"), str)
                    or row.get("task_terminal") is not True
                    or row.get("accepted") is not False):
                raise ValueError("contract review has an unsettled or unbound rework child")
        partial = {"review_output": report, "review_rework_requests": list(requests),
                   "disposition": "review outcome unaccepted"}
    elif operation.stage_id == 'supervisor' and 'watchdog_incident' in operation.payload:
        partial = {
            'disposition': 'interrupted watchdog diagnosis retained without recovery authority',
            'request': path_observation(f'artifacts/executions/{operation.command_id}/watchdog-supervision/input.json'),
            'execution_artifacts': path_observation(f'artifacts/executions/{operation.command_id}'),
        }
    elif operation.stage_id == 'supervisor' and 'progress_supervision' in operation.payload:
        from .progress_supervisor import archive_interrupted_progress_supervisor
        partial = archive_interrupted_progress_supervisor(operation)
    else:
        from .supervised_goals import collect, prepare
        identity = digest({"run_id": operation.run_id, "task_id": operation.task_id,
                           "stage_id": operation.stage_id, "command_id": operation.command_id})
        manifest = root / "artifacts" / "supervised-goals" / "manifests" / f"{identity}.json"
        if manifest.is_symlink() or not manifest.is_file():
            raise ValueError("interrupted supervisor has no durable prepared-goal manifest")
        prepared_supervisor = prepare(operation)
        partial = {"manifest": path_observation(
            f"artifacts/supervised-goals/manifests/{identity}.json"),
            "disposition": "partial goal edits retained as non-applicable evidence"}

    note = directory / "interrupted-agent.json"
    note_data = {
        "schema": "modport.interrupted-agent-cancellation.v1",
        "run_id": operation.run_id, "task_id": operation.task_id,
        "stage_id": operation.stage_id, "execution_id": operation.command_id,
        "effect_id": effect.effect_id, "effect_revision": effect.revision,
        "kernel_attempt": effect.attempt, "fence": effect.fence,
        "reason": "user_cancelled", "proof": dict(proof),
        "cancellation_receipt_ids": list(evidence.get("receipt_ids", [])),
        "partial_outputs": partial, "external_outcome": "unknown",
        "artifacts_complete": False, "acceptance_status": "unverified",
    }
    if note.is_symlink():
        raise ValueError("interrupted agent note is a symlink")
    if note.exists():
        if read_json(note) != note_data:
            raise ValueError("interrupted agent note differs from the verified cancellation")
    else:
        atomic_json(note, note_data)

    outputs = {
        "agent_cancelled": True, "external_outcome": "unknown",
        "artifacts_complete": False, "acceptance_status": "unverified",
        "partial_outputs_unaccepted": True,
        "artifact_refs": {"interrupted_agent": {"path": note.relative_to(root).as_posix()}},
    }
    if operation.stage_id == 'supervisor' and 'watchdog_incident' in operation.payload:
        outputs.update(watchdog_decision=None,
                       watchdog_supervisor_diagnostics=['User cancelled watchdog diagnosis; no decision accepted.'])
    if operation.stage_id == 'supervisor' and 'progress_supervision' in operation.payload:
        outputs.update(progress_supervisor_decision=None,
                       progress_supervisor_diagnostics=partial['diagnostics'])
        outputs['artifact_refs'].update(
            progress_supervision_interrupted_request=partial['request_ref'],
            progress_supervision_interrupted_report=partial['raw_report_ref'])
    result = OperationResult(
        "failed", operation.run_id, operation.task_id, operation.stage_id,
        operation.command_id, outputs=outputs,
        error_code=f"{operation.stage_id}_interrupted",
        detail="user cancelled agent; partial output is retained as unaccepted evidence",
    )
    if prepared_supervisor is not None:
        result = collect(prepared_supervisor, result)
        outputs = dict(result.outputs)
        refs = dict(outputs.get("artifact_refs", {}))
        refs["interrupted_agent"] = {"path": note.relative_to(root).as_posix()}
        outputs["artifact_refs"] = refs
        result = OperationResult(result.status, result.run_id, result.task_id,
            result.stage_id, result.command_id, outputs=outputs,
            detail=result.detail, error_code=result.error_code)
    atomic_json(receipt, {"execution_id": operation.command_id,
                         "effect_request": expected, "response": result.to_dict(),
                         "after": repository_facts(root, operation)})
    check_storage_budget(root, phase=f"kernel-recovery:{operation.stage_id}:effect-result",
                         pending_bytes=_response_bytes(result.to_dict()))
    return reconcile_receipt(root, command, effect)


def cancel_incomplete_rework_verification(root: Path, command: dict, effect,
                                          evidence: dict) -> dict:
    """Record a reaped reviewer-requested verification as failed and unverified.

    The caller must establish the matching SDK cancellation and cleanup facts.
    Neither a missing receipt nor this response proves the verifier's external
    work was rolled back. A completed receipt always takes precedence.
    """
    operation = OperationInput.from_dict(unpack_input(root, command["payload"]))
    if (operation.stage_id not in {"contract_verify", "target_build"}
            or not operation.task_id.endswith(".verify")
            or Path(operation.run_dir).resolve() != root.resolve()
            or command.get("handler_id") != f"modport.{operation.stage_id}"
            or command.get("execution_id") != operation.command_id
            or effect.execution_id != operation.command_id
            or effect.effect_id != f"modport:{operation.command_id}"
            or effect.name != "modport.stage"):
        raise ValueError("interrupted rework verification identity mismatch")
    directory = root / "artifacts" / "executions" / operation.command_id
    if directory.resolve() != directory.absolute():
        raise ValueError("interrupted verification receipt directory is not contained")
    receipt = directory / "receipt.json"
    if receipt.exists() or receipt.is_symlink():
        return reconcile_receipt(root, command, effect)
    if evidence.get("schema") != "modport.rework-verification-cancellation.v1" or any(
            evidence.get(key) != value for key, value in {
                "run_id": operation.run_id, "task_id": operation.task_id,
                "stage_id": operation.stage_id, "execution_id": operation.command_id,
                "effect_id": effect.effect_id, "effect_revision": effect.revision,
                "recovery_reason": "reviewer tool call closed",
            }.items()):
        raise ValueError("rework verification cancellation evidence differs from the effect")
    reviewer = operation.payload.get("reviewer_rework")
    reviewer_id = reviewer.get("reviewer_execution_id") if isinstance(reviewer, dict) else None
    request_id = reviewer.get("request_id") if isinstance(reviewer, dict) else None
    if (not isinstance(request_id, str) or not request_id
            or not isinstance(reviewer_id, str) or not reviewer_id
            or operation.task_id != f"agent-rework.{request_id}.verify"
            or evidence.get("request_id") != request_id
            or evidence.get("reviewer_execution_id") != reviewer_id
            or evidence.get("kernel_attempt") != effect.attempt
            or evidence.get("fence") != effect.fence
            or type(evidence.get("application_attempt")) is not int
            or evidence["application_attempt"] < 0
            or not isinstance(evidence.get("reviewer_stage"), str)
            or not evidence["reviewer_stage"]
            or not isinstance(evidence.get("reviewer_task_id"), str)
            or not evidence["reviewer_task_id"]
            or evidence.get("proof") != {name: "confirmed" for name in (
                "request_committed", "command_delivered",
                "execution_authority_revoked", "local_process_tree_reaped",
                "cleanup")}
            or not isinstance(evidence.get("cancellation_receipt_ids"), list)
            or not evidence["cancellation_receipt_ids"]
            or not all(isinstance(item, str) and item
                       for item in evidence["cancellation_receipt_ids"])
            or evidence.get("external_outcome") != "unknown"
            or evidence.get("artifacts_complete") is not False
            or evidence.get("acceptance_status") != "unverified"):
        raise ValueError("incomplete rework verification cancellation proof")
    marker = evidence.get("close_marker")
    allowed_markers = {
        f"artifacts/rework-tools/{reviewer_id}/closed.json",
        f"artifacts/rework-tools/{reviewer_id}/requests/{request_id}.cancel.json",
    }
    if not isinstance(marker, str) or marker not in allowed_markers:
        raise ValueError("rework verification cancellation marker identity mismatch")
    marker_path = root / marker
    if (not marker_path.is_file() or marker_path.is_symlink()
            or marker_path.resolve() != marker_path.absolute()):
        raise ValueError("rework verification cancellation marker is not contained")
    expected = {"input_sha256": digest(operation.to_dict()), "run_dir": str(root),
                "stage": operation.stage_id}
    if effect.request != expected:
        raise ValueError("rework verification effect request differs from the frozen operation")
    note = directory / "interrupted-rework-verification.json"
    if note.exists() or note.is_symlink():
        previous = read_json(note) if not note.is_symlink() else None
        previous_ids = (previous.get("cancellation_receipt_ids")
                        if isinstance(previous, dict) else None)
        stable = lambda value: {key: item for key, item in value.items()
                                if key != "cancellation_receipt_ids"}
        if (not isinstance(previous_ids, list) or not previous_ids
                or not all(isinstance(item, str) and item for item in previous_ids)
                or stable(previous) != stable(evidence)
                or not set(previous_ids).issubset(evidence["cancellation_receipt_ids"])):
            raise ValueError("interrupted verification note differs from the cancellation proof")
    else:
        atomic_json(note, evidence)
    response = OperationResult(
        "failed", operation.run_id, operation.task_id, operation.stage_id,
        operation.command_id,
        outputs={"verification_cancelled": True, "artifacts_complete": False,
                 "acceptance_status": "unverified", "external_outcome": "unknown",
                 "artifact_refs": {"interrupted_rework_verification": {
                     "path": note.relative_to(root).as_posix()}}},
        error_code="rework_verification_interrupted",
        detail="reviewer call closed; verification incomplete and external outcome unknown",
    ).to_dict()
    atomic_json(receipt, {"execution_id": operation.command_id,
                         "effect_request": expected, "response": response,
                         "after": repository_facts(root, operation)})
    check_storage_budget(root, phase=f"kernel-recovery:{operation.stage_id}:effect-result",
                         pending_bytes=_response_bytes(response))
    return reconcile_receipt(root, command, effect)


def resume_interrupted_goal(root: Path, command: dict, effect) -> dict:
    """Resume one frozen coder effect in its existing native thread.

    The existing recover command holds the operation lock. This helper never
    dispatches an assignment or changes the SDK database; its caller resolves
    the original effect through the public SDK after the receipt is durable.
    """
    from hashlib import sha256
    import json
    import math
    import time
    from .development import CoderHandler, coder_runtime_goal
    from .business_policy import business_gates_disabled

    root = Path(root).resolve()
    operation = OperationInput.from_dict(unpack_input(root, command['payload']))
    if root != Path(operation.run_dir).resolve():
        raise ValueError('native goal recovery workspace does not match the command')
    directory = root / 'artifacts' / 'executions' / operation.command_id
    receipt = directory / 'receipt.json'
    if receipt.exists() or receipt.is_symlink():
        return reconcile_receipt(root, command, effect)
    expected = {'input_sha256': digest(operation.to_dict()), 'run_dir': str(root),
                'stage': operation.stage_id}
    if effect.request != expected:
        raise ValueError('native goal recovery operation identity mismatch')
    if operation.stage_id != 'coder' or 'coder_goal' not in operation.artifact_refs:
        raise ValueError('native goal recovery requires an interrupted native coder')
    original_input = _contained_file(root, {'path': str((directory / 'input.json').relative_to(root))})
    if read_json(original_input) != operation.to_dict():
        raise ValueError('native goal recovery input differs from the frozen operation')
    workspace = operation_workspace(root, operation)
    if workspace is None or not workspace.is_dir():
        raise ValueError('native goal recovery requires its existing isolated workspace')
    setup_path = _contained_file(root, {'path': str((directory / 'coder-setup.json').relative_to(root))})
    setup = read_json(setup_path)
    record = setup.get('record') if isinstance(setup, dict) else None
    if not isinstance(record, dict) or setup.get('sha256') != sha256(
            json.dumps(record, sort_keys=True, separators=(',', ':')).encode()).hexdigest():
        raise ValueError('native goal recovery setup digest mismatch')
    required = {key: getattr(operation, key) for key in ('run_id', 'task_id', 'stage_id', 'command_id')}
    required.update(workspace=operation.options['workspace'], goal_ref=operation.artifact_refs['coder_goal'])
    if any(record.get(key) != value for key, value in required.items()):
        raise ValueError('native goal recovery setup identity mismatch')
    goal_ref = operation.artifact_refs['coder_goal']
    goal_path = _contained_file(root, goal_ref)
    if file_digest(goal_path) != goal_ref.get('sha256'):
        raise ValueError('native goal recovery goal digest mismatch')
    goal = read_json(goal_path)
    if not isinstance(goal, dict) or not isinstance(goal.get('objective'), str):
        raise ValueError('native goal recovery goal is invalid')
    native_directory = root / 'artifacts' / 'native-goals' / sha256(operation.command_id.encode()).hexdigest()[:24]
    _contained_file(root, {'path': str((native_directory / 'claim').relative_to(root))})
    native_path = _contained_file(root, {'path': str((native_directory / 'state.json').relative_to(root))})
    native = read_json(native_path)
    # A v17 goal report is free-form coder context.  ``CoderHandler`` derives
    # the native-session objective from the normalized task/goal stored in its
    # authenticated setup record, rather than using that report verbatim.
    # Compare against the same stored value during recovery.  Older setup
    # records do not contain a normalized goal, so retain their original
    # report-objective comparison.
    setup_goal = record.get('goal')
    native_objective = (coder_runtime_goal(setup_goal, advisory=business_gates_disabled(operation))['objective']
                        if isinstance(setup_goal, dict)
                        and isinstance(setup_goal.get('objective'), str)
                        else goal.get('objective'))
    if (not isinstance(native, dict) or native.get('command_id') != operation.command_id
            or native.get('worktree') != str(workspace.resolve())
            or native.get('objective') != native_objective or not native.get('thread_id')
            or not isinstance(native.get('prompt_sha256'), str) or len(native['prompt_sha256']) != 64):
        raise ValueError('native goal recovery thread identity mismatch')
    deadline = native.get('deadline_epoch')
    if isinstance(deadline, bool) or not isinstance(deadline, (int, float)) or not math.isfinite(deadline):
        raise ValueError('native goal recovery requires its original assignment deadline')
    original_deadline = operation.options.get('deadline_epoch')
    if original_deadline is not None:
        if (isinstance(original_deadline, bool) or not isinstance(original_deadline, (int, float))
                or not math.isfinite(original_deadline)):
            raise ValueError('native goal recovery original operation deadline is invalid')
        deadline = min(deadline, original_deadline)
    if deadline <= time.time():
        raise ValueError('native goal original assignment deadline is exhausted')
    for ref in operation.artifact_refs.values():
        _contained_file(root, ref)
    validate_consumed_copies(root, operation)
    # This overlay is execution-only: input.json and the effect digest remain
    # those of the original frozen command, including its original attempt.
    delegated = replace(operation, options={**operation.options, 'native_goal_resume': True,
                                            'deadline_epoch': deadline})
    with operation_context(operation) as audit_identity:
        result = CoderHandler()(delegated)
        if not isinstance(result, OperationResult):
            raise TypeError('native goal recovery handler must return OperationResult')
        result.validate_for(operation)
        outputs = dict(result.outputs)
        refs = dict(outputs.get('artifact_refs', {}))
        for field in ('log', 'last_message'):
            relative = outputs.get(field)
            if isinstance(relative, str) and (root / relative).is_file():
                refs.setdefault(f'{operation.stage_id}:{field}', {
                    'path': relative, 'sha256': file_digest(root / relative), 'media_type': 'text/plain'})
        outputs['artifact_refs'] = {key: seal_ref(root, ref, execution_id=operation.command_id)
                                    for key, ref in refs.items()}
        response = replace(result, outputs=outputs).to_dict()
        atomic_json(receipt, {'execution_id': operation.command_id, 'effect_request': expected,
                             'response': response, 'after': repository_facts(root, operation)})
        record_event(root, audit_identity['invocation_id'] + ':result', 'operation.result',
                     response, status=response['status'])
    logical_response = validate_stage_response(root, operation, response, expected)
    check_storage_budget(
        root, phase=f"kernel-recovery:{operation.stage_id}:effect-result",
        pending_bytes=_response_bytes(logical_response),
    )
    return pack_result(root, logical_response)


def recover_final_cleanup(root: Path, command: dict, effect, kernel) -> dict:
    """Publish an interrupted final cleanup's checkpoint through its original Effect.

    No new model call, assignment, deadline or Effect is created. The caller
    holds the normal operation lock and resolves the Effect with the public SDK.
    """
    from dispatcher_sdk.execution_kernel import BudgetEnvelope, BudgetClockUnknownError
    from .cleanup import FinalCleanupHandler
    from .execution_budget import receipt_phase, reserve_settlement

    root = Path(root).resolve()
    operation = OperationInput.from_dict(unpack_input(root, command['payload']))
    if (operation.stage_id != 'final_cleanup' or operation.options.get('workflow_version', 0) < 37
            or Path(operation.run_dir).resolve() != root
            or command.get('execution_id') != operation.command_id
            or command.get('correlation_id') != operation.run_id
            or command.get('handler_id') != 'modport.final_cleanup'
            or effect.execution_id != operation.command_id
            or effect.effect_id != 'modport:' + operation.command_id
            or effect.name != 'modport.stage'):
        raise ValueError('final cleanup recovery operation/effect identity mismatch')
    directory = root / 'artifacts' / 'executions' / operation.command_id
    receipt = directory / 'receipt.json'
    if receipt.exists() or receipt.is_symlink():
        return reconcile_receipt(root, command, effect)
    frozen = _contained_file(root, {'path': str((directory / 'input.json').relative_to(root))})
    if read_json(frozen) != operation.to_dict():
        raise ValueError('final cleanup recovery input differs from the frozen operation')
    handler = FinalCleanupHandler()
    checkpoint = read_json(handler._checkpoint_path(root))
    if (checkpoint.get('phase') not in {'prepared', 'settled'}
            or checkpoint.get('execution_id') != operation.command_id):
        raise ValueError('final cleanup lacks a published checkpoint for this execution')

    class RecoveryBudget:
        command = kernel.get(operation.command_id).command

        @property
        def budget(self):
            limits = kernel.get_execution_limits(operation.command_id)
            if (not isinstance(limits, Mapping) or limits.get('clock_status') != 'trusted'
                    or limits.get('entry_attempt') != effect.attempt
                    or limits.get('entry_fence') != effect.fence):
                raise BudgetClockUnknownError('final cleanup original SDK budget is unavailable')
            return BudgetEnvelope.from_dict(limits['envelope']).view()

    with execution_budget(RecoveryBudget()), operation_context(operation) as audit_identity:
        reserve_settlement(operation, 0)
        result = handler(operation)
        result.validate_for(operation)
        # Handler replay uses only already-published artifact references. Receipt
        # sealing follows the same publication contract as a normal SDK stage.
        with receipt_phase(operation):
            outputs = dict(result.outputs)
            refs = outputs.get('artifact_refs', {})
            outputs['artifact_refs'] = {key: seal_ref(root, ref, execution_id=operation.command_id)
                for key, ref in refs.items()}
            response = replace(result, outputs=outputs).to_dict()
            atomic_json(receipt, {'execution_id': operation.command_id,
                'effect_request': dict(effect.request), 'response': response,
                'after': repository_facts(root, operation)})
            record_event(root, audit_identity['invocation_id'] + ':result', 'operation.result',
                response, status=response['status'])
    return reconcile_receipt(root, command, effect)
