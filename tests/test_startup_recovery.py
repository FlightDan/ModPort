import os
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from modport.contracts import OperationInput
from modport.memory_admission import MemorySnapshot
from modport.operations import MigrationOperations
from modport.payload_storage import pack_input
from modport.startup_recovery import classify_startup_timeout


class ProductionWrapperStageHandler:
    """Small business stage used to exercise the production SDK wrapper."""

    __execution_kernel_revision__ = "production-wrapper-stage-v1"

    def __call__(self, operation):
        from modport.contracts import OperationResult
        return OperationResult(
            "completed", operation.run_id, operation.task_id, operation.stage_id,
            operation.command_id, outputs={"wrapper_probe": "completed"},
        )


class StartupTimeoutHandler:
    """SDK-only barrier: publish worker identity, then block before any effect."""

    __execution_kernel_revision__ = "startup-timeout-handler-v1"

    def __call__(self, payload, context):
        from modport.execution_progress import record_command_progress
        record_command_progress(Path(payload["run_dir"]), {
            "payload": payload,
            "registry_revision": context.command.registry_revision,
        }, "worker_entered", kernel_attempt=context.lease.attempt,
           fence=context.lease.fence)
        time.sleep(10)


@unittest.skipUnless(os.name == "posix" and Path("/proc").is_dir(),
                     "requires process isolation and Linux process birth evidence")
class RealSDKStartupRecoveryTests(unittest.TestCase):
    def test_process_worker_uses_memory_lease_workspace_lock_and_stage_markers(self):
        from dispatcher_sdk.execution_kernel import Kernel, RetryPolicy
        from dispatcher_sdk.orchestrator import Operations, Orchestrator
        from modport.execution_progress import read_execution_progress
        from modport.kernel_runtime import SDKHandler
        from modport.memory_admission import MemoryPolicy

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            handler = SDKHandler(
                ProductionWrapperStageHandler(), "production-wrapper-deployment-v1",
                enforce_memory=True, memory_policy=MemoryPolicy(8 * 1024**2, 0),
            )
            runtime = Kernel.open_sqlite(
                root / "kernel.sqlite3", {"modport.contract_draft": handler},
                isolation_mode="process",
            )
            sdk = Orchestrator(root / "orchestrator.sqlite3", runtime.kernel, runtime=runtime)
            try:
                run_id = "production-wrapper"
                execution_id = run_id + ":contract-draft:1"
                sdk.create_run(run_id, command_id="create", input={"run_id": run_id}, definition={})
                operation = OperationInput(
                    run_id, "contract-draft", "contract_draft", execution_id,
                    str(root), options={"workflow_version": 22},
                )
                command = runtime.command(
                    "modport.contract_draft", execution_id=execution_id,
                    idempotency_key=execution_id, correlation_id=run_id,
                    timeout_seconds=15, payload=operation.to_dict(),
                    retry_policy=RetryPolicy(max_attempts=1, retry_timeouts=False),
                )
                sdk.apply_operations(
                    run_id, command_id="dispatch", expected_revision=0,
                    operations=[Operations.add_task("contract-draft", command),
                                Operations.dispatch("contract-draft")],
                    application_state={},
                )
                sdk.flush()

                terminal = runtime.run_once()
                self.assertEqual("succeeded", terminal.state)
                observed = sdk.inspect_execution(execution_id)
                self.assertEqual("succeeded", observed.result.status)
                from modport.payload_storage import unpack_result
                business_result = unpack_result(root, observed.result.value)
                self.assertEqual("completed", business_result["status"])
                self.assertEqual("completed", business_result["outputs"]["wrapper_probe"])
                progress = read_execution_progress(root, execution_id)
                self.assertEqual("finished", progress["phase"])
                phases = [item["phase"] for item in progress["transitions"]]
                self.assertIn("waiting_memory", phases)
                self.assertIn("waiting_workspace", phases)
                self.assertIn("handler_entered", phases)
                self.assertIn("settling", phases)
            finally:
                sdk.close()
                runtime.close()

    def test_sdk_process_timeout_can_be_proved_pre_handler_and_effect_free(self):
        from dispatcher_sdk.execution_kernel import Kernel, RetryPolicy
        from dispatcher_sdk.orchestrator import Operations, Orchestrator
        from modport.execution_progress import read_execution_progress

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runtime = Kernel.open_sqlite(
                root / "kernel.sqlite3", {"modport.coder": StartupTimeoutHandler()},
                isolation_mode="process",
            )
            sdk = Orchestrator(root / "orchestrator.sqlite3", runtime.kernel, runtime=runtime)
            try:
                run_id = "process-startup"
                execution_id = run_id + ":coder-1:1"
                sdk.create_run(run_id, command_id="create", input={"run_id": run_id}, definition={})
                operation = OperationInput(
                    run_id, "coder-1", "coder", execution_id, str(root),
                    options={"workflow_version": 22},
                )
                command = runtime.command(
                    "modport.coder", execution_id=execution_id,
                    idempotency_key=execution_id, correlation_id=run_id,
                    timeout_seconds=1.2, payload=operation.to_dict(),
                    retry_policy=RetryPolicy(max_attempts=1, retry_timeouts=False),
                )
                sdk.apply_operations(run_id, command_id="dispatch", expected_revision=0,
                    operations=[Operations.add_task("coder-1", command),
                                Operations.dispatch("coder-1")],
                    application_state={})
                sdk.flush()

                terminal = runtime.run_once()
                self.assertEqual("timed_out", terminal.state)
                observed = sdk.inspect_execution(execution_id)
                self.assertEqual("timed_out", observed.result.status)
                self.assertIsNone(observed.lease)
                self.assertEqual([], observed.result.effect_ids)
                progress = read_execution_progress(root, execution_id)
                self.assertEqual("worker_entered", progress["phase"])

                decision = classify_startup_timeout(
                    sdk, run_id=run_id, execution_id=execution_id,
                    task_id="coder-1", application_attempt=1,
                    command=observed.command.to_dict(), progress=progress,
                    process_isolated=True, already_recovered=False,
                    current_registry_revision=observed.command.registry_revision,
                    deadline_epoch=None, now=time.time(),
                )
                self.assertEqual("retry", decision["action"], decision)
                self.assertEqual(
                    "sdk_timeout_terminal_and_pre_handler_worker_birth_gone",
                    decision["worker_stopped_proof"],
                )

                # Carry the actual public-SDK timeout evidence through the
                # v22 host policy. This stays a policy-only check: dispatch
                # remains an SDK operation owned by the normal driver.
                host = MigrationOperations(isolation_mode="process")
                host_header = {
                    "run_dir": str(root), "deadline_epoch": time.time() + 120,
                    "registry_revision": observed.command.registry_revision,
                    "definition": {"workflow_version": 22,
                        "stages": [{"stage_id": "coder", "agent": True}]},
                }
                snapshot = {
                    "run_id": run_id, "state": "running", "waits": {},
                    "tasks": {"coder-1": {"attempts": [{
                        "state": "timed_out", "command": observed.command.to_dict(),
                    }]}},
                }
                app = host._new_application()
                before_assignments = app["agent_assignments"]
                changes, parked = host._startup_recovery_decision(
                    snapshot, host_header, app, sdk)
                self.assertTrue(parked)
                self.assertEqual(["new_attempt", "dispatch"],
                                 [change["kind"] for change in changes])
                recovered = changes[0]["command"]
                from modport.payload_storage import unpack_input
                recovered_input = unpack_input(root, recovered["payload"])
                self.assertEqual(2, recovered_input["attempt"])
                self.assertEqual(execution_id, recovered["causation_id"])
                self.assertEqual(before_assignments, app["agent_assignments"])
            finally:
                sdk.close()
                runtime.close()


class FakeSDK:
    def __init__(self, *, state="timed_out", status="timed_out", attempt=1,
                 fence=2, effects=(), lease=None, recoveries=(), error=None,
                 result_error_code="handler_timeout"):
        self.execution = SimpleNamespace(
            state=state, result=SimpleNamespace(status=status, attempt=attempt,
                                                fence=fence, effect_ids=effects,
                                                error=SimpleNamespace(code=result_error_code)),
            lease=lease,
            command=SimpleNamespace(execution_id="run-1:coder-1:1",
                                    registry_revision="revision-1",
                                    handler_id="modport.coder", correlation_id="run-1"),
        )
        self.recoveries = recoveries
        self.error = error

    def inspect_execution(self, execution_id):
        if self.error:
            raise self.error
        return self.execution

    def inspect_recoveries(self, run_id):
        return self.recoveries


class StartupRecoveryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.execution_id = "run-1:coder-1:1"
        self.command = {"execution_id": self.execution_id,
                        "registry_revision": "revision-1",
                        "handler_id": "modport.coder", "correlation_id": "run-1"}
        self.progress = {
            "run_id": "run-1", "task_id": "coder-1", "stage_id": "coder",
            "execution_id": self.execution_id, "application_attempt": 1,
            "registry_revision": "revision-1", "phase": "waiting_memory",
            "kernel_attempt": 1, "fence": 2, "last_progress_at": 100,
            "worker_pid": 1234, "worker_birth": "boot:1",
        }

    def classify(self, sdk=None, **changes):
        worker_state = changes.pop("worker_state", False)
        with patch("modport.startup_recovery.process_identity_state",
                   return_value=worker_state):
            return classify_startup_timeout(
                sdk or FakeSDK(), run_id="run-1", execution_id=self.execution_id,
                task_id="coder-1", application_attempt=1, command=self.command,
                progress=changes.pop("progress", self.progress),
                process_isolated=changes.pop("process_isolated", True),
                already_recovered=changes.pop("already_recovered", False),
                current_registry_revision="revision-1",
                deadline_epoch=changes.pop("deadline_epoch", 1000),
                now=changes.pop("now", 200),
                allow_settled_diagnosis=changes.pop("allow_settled_diagnosis", False),
                receipt_verified=changes.pop("receipt_verified", False),
                settled_effect=changes.pop("settled_effect", None),
                expected_effect_request=changes.pop("expected_effect_request", None),
            )

    def test_only_positive_pre_handler_proof_authorizes_one_retry(self):
        decision = self.classify()
        self.assertEqual("retry", decision["action"])
        self.assertEqual("sdk_timeout_terminal_and_pre_handler_worker_birth_gone",
                         decision["worker_stopped_proof"])
        self.assertEqual("handler_marker_not_entered_and_sdk_effect_ids_empty",
                         decision["effects_proof"])

    def test_missing_or_conflicting_authority_never_authorizes_retry(self):
        cases = [
            (self.classify(progress=None), "execution_progress_missing"),
            (self.classify(progress={**self.progress, "task_id": "other"}),
             "execution_progress_identity_mismatch"),
            (self.classify(progress={**self.progress, "phase": "handler_entered"}),
             "handler_or_model_may_have_started"),
            (self.classify(progress={**self.progress, "fence": None}),
             "execution_fence_not_proven"),
            (self.classify(progress={**self.progress, "fence": 9}),
             "execution_fence_mismatch"),
            (self.classify(worker_state=True), "old_worker_process_exit_not_proven"),
            (self.classify(worker_state=None), "old_worker_process_exit_not_proven"),
            (self.classify(process_isolated=False), "worker_tree_stop_not_proven"),
            (self.classify(already_recovered=True), "automatic_recovery_already_used"),
            (self.classify(deadline_epoch=200), "original_run_deadline_exhausted"),
        ]
        cases.extend([
            (self.classify(FakeSDK(lease=object())), "old_worker_lease_still_present"),
            (self.classify(FakeSDK(effects=("effect-1",))), "effect_state_not_proven_empty"),
            (self.classify(FakeSDK(status="failed")), "sdk_timeout_terminal_state_not_proven"),
            (self.classify(FakeSDK(error=OSError("unavailable"))),
             "sdk_execution_authority_unavailable"),
        ])
        recovery = SimpleNamespace(execution=SimpleNamespace(
            execution_id=self.execution_id))
        cases.append((self.classify(FakeSDK(recoveries=(recovery,))),
                      "sdk_effect_recovery_present"))
        for decision, reason in cases:
            with self.subTest(reason=reason):
                self.assertEqual("recovery_required", decision["action"])
                self.assertEqual(reason, decision["reason"])

    def test_settled_coder_timeout_only_enters_diagnosis_with_full_proof(self):
        finished = {**self.progress, "phase": "finished"}
        effect_id = "modport:" + self.execution_id
        request = {"input_sha256": "frozen", "run_dir": str(self.root), "stage": "coder"}
        effect = SimpleNamespace(effect_id=effect_id, execution_id=self.execution_id,
                                 name="modport.stage", state="committed", attempt=1,
                                 fence=2, request=request)
        kwargs = {"progress": finished, "allow_settled_diagnosis": True,
                  "receipt_verified": True, "settled_effect": effect,
                  "expected_effect_request": request}
        decision = self.classify(FakeSDK(effects=(effect_id,)), **kwargs)
        self.assertEqual("diagnose", decision["action"])
        self.assertEqual("settled_coder_handler_timeout", decision["reason"])
        self.assertEqual("diagnose", self.classify(
            FakeSDK(effects=(effect_id,)), already_recovered=True, **kwargs)["action"])

        recovery = SimpleNamespace(execution=SimpleNamespace(
            execution_id=self.execution_id))
        cases = [
            self.classify(FakeSDK(effects=(effect_id,)), progress=finished,
                          allow_settled_diagnosis=True, settled_effect=effect,
                          expected_effect_request=request),
            self.classify(FakeSDK(effects=(effect_id,)), progress=finished,
                          receipt_verified=True, settled_effect=effect,
                          expected_effect_request=request),
            self.classify(FakeSDK(effects=(effect_id,)), progress={**finished, "phase": "handler_entered"},
                          allow_settled_diagnosis=True, receipt_verified=True,
                          settled_effect=effect, expected_effect_request=request),
            self.classify(FakeSDK(effects=()), **kwargs),
            self.classify(FakeSDK(effects=(effect_id, "other")), **kwargs),
            self.classify(FakeSDK(effects=(effect_id,), recoveries=(recovery,)), **kwargs),
            self.classify(FakeSDK(effects=(effect_id,)), worker_state=True, **kwargs),
            self.classify(FakeSDK(effects=(effect_id,), status="failed"), **kwargs),
            self.classify(FakeSDK(effects=(effect_id,), result_error_code="other"), **kwargs),
            self.classify(FakeSDK(effects=(effect_id,)), **{**kwargs, "settled_effect":
                SimpleNamespace(**{**vars(effect), "state": "indeterminate"})}),
        ]
        for candidate in cases:
            with self.subTest(reason=candidate["reason"]):
                self.assertEqual("recovery_required", candidate["action"])

    def _operations_fixture(self, *, include_progress=True, timeout=500):
        operation = OperationInput(
            "run-1", "coder-1", "coder", self.execution_id, str(self.root),
            attempt=1, options={"retry_policy": {"max_attempts": 3}},
        )
        command = {
            "execution_id": self.execution_id,
            "idempotency_key": self.execution_id,
            "handler_id": "modport.coder", "correlation_id": "run-1",
            "registry_revision": "revision-1", "timeout_seconds": timeout,
            "retry_policy": {"max_attempts": 3},
            "payload": pack_input(self.root, operation.to_dict()),
        }
        snapshot = {
            "run_id": "run-1", "state": "running",
            "tasks": {"coder-1": {"attempts": [{
                "state": "timed_out", "command": command,
            }]}},
            "waits": {},
        }
        header = {
            "run_dir": str(self.root), "deadline_epoch": 10_000,
            "registry_revision": "revision-1",
            "definition": {"workflow_version": 22,
                "stages": [{"stage_id": "coder", "agent": True}]},
        }
        app = MigrationOperations._new_application()
        app["agent_assignments"] = 7
        operations = MigrationOperations(
            isolation_mode="process", clock=lambda: 9_800,
            memory_probe=lambda: MemorySnapshot(8 * 1024**3, 8 * 1024**3, "fixture"),
        )
        if include_progress:
            from modport.execution_progress import write_execution_progress
            write_execution_progress(self.root, {
                "run_id": "run-1", "task_id": "coder-1", "stage_id": "coder",
                "command_id": self.execution_id, "attempt": 1,
            }, "worker_entered", kernel_attempt=1, fence=2,
               registry_revision="revision-1", now=9_700)
        return operations, snapshot, header, app, command

    def test_proven_retry_keeps_assignment_and_original_deadline(self):
        operations, snapshot, header, app, original = self._operations_fixture()
        snapshot["application_state"] = app
        with patch("modport.startup_recovery.process_identity_state", return_value=False):
            changes, app = operations._decision(snapshot, header, sdk=FakeSDK())
        retry, dispatch = changes
        self.assertEqual("new_attempt", retry["kind"])
        self.assertEqual("dispatch", dispatch["kind"])
        self.assertNotIn("startup_recovery_required", app)
        self.assertEqual(7, app["agent_assignments"])
        recovered = retry["command"]
        self.assertEqual(200, recovered["timeout_seconds"])
        self.assertEqual(original["registry_revision"], recovered["registry_revision"])
        self.assertEqual(original["retry_policy"], recovered["retry_policy"])
        payload = recovered["payload"]
        self.assertEqual(2, payload["attempt"])
        self.assertEqual("run-1:coder-1:2", payload["command_id"])
        self.assertEqual(original["execution_id"], recovered["causation_id"])
        self.assertIn("coder-1", app["startup_recoveries"])

        snapshot["tasks"]["coder-1"]["attempts"].append({
            "state": "timed_out", "command": recovered,
        })
        changes, parked = operations._startup_recovery_decision(
            snapshot, header, app, FakeSDK())
        self.assertTrue(parked)
        self.assertEqual(["wait"], [change["kind"] for change in changes])
        self.assertEqual("automatic_recovery_already_used",
                         app["startup_recovery_required"]["coder-1"]["reason"])

    def test_uncertain_execution_parks_once_and_user_stop_releases_one_wait(self):
        operations, snapshot, header, app, _ = self._operations_fixture(include_progress=False)
        snapshot["application_state"] = app
        changes, app = operations._decision(snapshot, header, sdk=FakeSDK())
        self.assertTrue(any(change["kind"] == "wait" for change in changes))
        wait = next(change for change in changes if change["kind"] == "wait")
        self.assertNotIn("execution_id", wait["payload"])
        snapshot["waits"][wait["wait_id"]] = {"state": "open"}

        changes, parked = operations._startup_recovery_decision(
            snapshot, header, app, FakeSDK())
        self.assertEqual([], changes)
        self.assertTrue(parked)

        app["stop_reason"] = "user_cancelled"
        changes, parked = operations._startup_recovery_decision(
            snapshot, header, app, FakeSDK())
        self.assertEqual(["release_wait"], [change["kind"] for change in changes])
        self.assertFalse(parked)
        self.assertEqual({}, app["startup_recovery_required"])

    def test_malformed_recovery_history_and_wait_state_fail_closed(self):
        operations, snapshot, header, app, _ = self._operations_fixture()
        app["startup_recoveries"] = []
        changes, parked = operations._startup_recovery_decision(
            snapshot, header, app, FakeSDK())
        self.assertTrue(parked)
        self.assertEqual(["wait"], [change["kind"] for change in changes])
        self.assertEqual("startup_recovery_state_invalid",
                         app["startup_recovery_required"]["coder-1"]["reason"])

        operations, snapshot, header, app, _ = self._operations_fixture()
        app["startup_recovery_required"] = {"coder-1": "malformed"}
        changes, parked = operations._startup_recovery_decision(
            snapshot, header, app, FakeSDK())
        self.assertTrue(parked)
        self.assertEqual(["wait"], [change["kind"] for change in changes])
        self.assertEqual("startup_recovery_state_invalid",
                         app["startup_recovery_required"]["__invalid_state__"]["reason"])

    def test_frozen_v21_policy_is_not_given_v22_recovery_state(self):
        operations, snapshot, header, app, _ = self._operations_fixture()
        header["definition"]["workflow_version"] = 21
        before = dict(app)
        changes, parked = operations._startup_recovery_decision(
            snapshot, header, app, FakeSDK())
        self.assertEqual([], changes)
        self.assertFalse(parked)
        self.assertEqual(before, app)


if __name__ == "__main__":
    unittest.main()
