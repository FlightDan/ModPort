"""Actual SDK cancellation, proved incomplete receipt, and subsequent repair."""
from dataclasses import asdict, dataclass, replace
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from dispatcher_sdk.orchestrator import Orchestrator, OrchestratorHost
from modport.application_state_storage import hydrate_run_snapshot
from modport.contracts import OperationInput, OperationResult, json_copy
from modport.evidence import atomic_json, workspace_lock
from modport.kernel_runtime import open_runtime, operation_lock
from modport.models import Budget, MigrationRequest
from modport.operations import MigrationOperations
from modport.watchdog_routing import begin, decide
from modport.watchdog_settlement import _authorization, _evidence, settle
from modport.workflow import compile_migration_workflow, WORKFLOW_VERSION


@dataclass
class InterruptedAuthor:
    __execution_kernel_revision__ = "watchdog-interrupted-author-v1"

    def __call__(self, operation):
        root = Path(operation.run_dir)
        (root / "author-entered").write_text(operation.command_id)
        if operation.payload.get("watchdog_recovery"):
            (root / "consumed-repair.json").write_text(json.dumps(operation.payload["watchdog_recovery"]))
            return OperationResult("completed", operation.run_id, operation.task_id,
                                   operation.stage_id, operation.command_id)
        while True:
            time.sleep(.02)


@dataclass
class RecoverySupervisor:
    __execution_kernel_revision__ = "watchdog-recovery-supervisor-v1"

    def __call__(self, operation):
        request = operation.payload["watchdog_incident"]
        decision = {"incident_id": request["incident_id"], "action": "repair_resume",
            "reason": "Observed the blocked fixture dependency", "instruction": "Resume with the supplied corrected dependency",
            "wait_for": [], "stop_category": None}
        return OperationResult("completed", operation.run_id, operation.task_id,
            operation.stage_id, operation.command_id, outputs={"watchdog_decision": decision})


class WatchdogSettlementTests(unittest.TestCase):
    def test_real_sdk_cancel_retains_uncertainty_until_proof_then_consumes_repair(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "baseline/.modport").mkdir(parents=True)
            handlers = {"modport.behavior_extract": InterruptedAuthor(), "modport.supervisor": RecoverySupervisor()}
            owner = MigrationOperations(handlers=handlers, isolation_mode="process")
            request = MigrationRequest("fixture", "https://example.invalid/fixture.git", "1.20.1", "1.21.1",
                workflow_mode="artifact_verification", budget=Budget(max_seconds=3000, max_agent_assignments=8))
            definition = compile_migration_workflow(request).to_dict()
            self.assertEqual(definition["workflow_version"], WORKFLOW_VERSION)
            with open_runtime(root, handlers=handlers, isolation_mode="process", now=owner.clock,
                              memory_policy=owner.memory_policy) as runtime:
                sdk = Orchestrator(root / "orchestrator.sqlite3", runtime.kernel, runtime=runtime)
                host = OrchestratorHost(sdk, worker_count=3)
                try:
                    now = time.time()
                    header = {"format_version": 2, "run_id": "watchdog-settlement", "run_dir": str(root),
                        "request": request.to_dict(), "definition": definition,
                        "registry_revision": runtime.registry_revision, "prior_findings": [], "initial_refs": {},
                        "rubric_sha256": "host-provided", "started_at": now, "deadline_epoch": now + 3000,
                        "watchdog_policy": {"enabled": True, "inactivity_seconds": 600}}
                    atomic_json(root / "run.json", header)
                    sdk.create_run(header["run_id"], command_id="create", input=header, definition=definition)
                    sequence = [0]
                    def read():
                        sdk.sync()
                        return hydrate_run_snapshot(root, sdk.get_run(header["run_id"]))
                    def apply(state, actions, app):
                        sequence[0] += 1
                        sdk.apply_operations(header["run_id"], command_id="decision-" + str(sequence[0]),
                            expected_revision=state["revision"], expected_generation=state["generation"],
                            operations=actions, application_state=app)
                        host.wake(header["run_id"])
                    def wait_until(predicate, seconds=15):
                        bound = time.monotonic() + seconds
                        while time.monotonic() < bound:
                            state = read()
                            if predicate(state):
                                return state
                            host.wake(header["run_id"])
                            time.sleep(.02)
                        self.fail("Bounded SDK fixture did not settle: " + repr(state["tasks"]))
                    state = read()
                    app = owner._new_application()
                    actions = owner._schedule(state, header, app, "behavior_extract", dependencies=[])
                    host.start()
                    apply(state, actions, app)
                    state = wait_until(lambda _: (root / "author-entered").exists())
                    execution = state["tasks"]["behavior_extract"]["attempts"][-1]["command"]["execution_id"]
                    app = state["application_state"]
                    actions = begin(owner, state, header, app, {"incident_id": "blocked-author", "kind": "no_useful_progress",
                        "reason": "Bounded fixture author waits on a dependency", "target_task_id": "behavior_extract",
                        "target_execution_id": execution})
                    supervisor = app["watchdog"]["episodes"]["blocked-author"]["supervisor_task_id"]
                    apply(state, actions, app)
                    state = wait_until(lambda s: s["tasks"][supervisor]["attempts"][-1]["state"] == "succeeded")
                    app = state["application_state"]
                    actions, pending = decide(owner, state, header, app, sdk)
                    self.assertTrue(pending)
                    self.assertEqual([action["task_id"] for action in actions if action["kind"] == "cancel"], ["behavior_extract"])
                    apply(state, actions, app)
                    state = wait_until(lambda s: s["tasks"]["behavior_extract"]["attempts"][-1]["state"] == "recovery_required")
                    report = sdk.inspect_cancellation(header["run_id"], execution_id=execution)
                    bound = time.monotonic() + 10
                    proof_fields = ("request_committed", "command_delivered", "execution_authority_revoked",
                                    "local_process_tree_reaped", "cleanup")
                    # Runtime cleanup can finish before the SDK host records
                    # cancellation delivery. Settlement requires both facts.
                    while any(getattr(report.executions[0], field).status != "confirmed"
                              for field in proof_fields) and time.monotonic() < bound:
                        time.sleep(.02)
                        report = sdk.inspect_cancellation(header["run_id"], execution_id=execution)
                    entry = report.executions[0]
                    self.assertEqual({field: getattr(entry, field).status for field in proof_fields},
                                     {field: "confirmed" for field in proof_fields})
                    effect = runtime.kernel.get_effect("modport:" + execution)
                    self.assertEqual(effect.state, "indeterminate")
                    incomplete = replace(report, executions=(replace(entry, cleanup=replace(entry.cleanup, status="unknown")),))
                    with patch.object(sdk, "inspect_cancellation", return_value=incomplete):
                        self.assertFalse(settle(root, header, sdk, state))
                    self.assertFalse((root / "artifacts/executions" / execution / "receipt.json").exists())
                    stale = json_copy(state)
                    stale["application_state"]["watchdog"]["episodes"]["blocked-author"]["decision"]["incident_id"] = "unrelated"
                    self.assertFalse(settle(root, header, sdk, stale))
                    settled = settle(root, header, sdk, read())
                    if not settled:
                        diagnostic_path = Path(__file__).resolve().parents[1] / (
                            "build/validation/watchdog-v40-20261007/settlement-failure.json")
                        def diagnose():
                            current = read()
                            operation = OperationInput.from_dict(
                                current["tasks"]["behavior_extract"]["attempts"][-1]["command"]["payload"])
                            try:
                                with workspace_lock(operation_lock(root, operation), blocking=False):
                                    lock_held = False
                            except BlockingIOError:
                                lock_held = True
                            cancellation = sdk.inspect_cancellation(header["run_id"], execution_id=execution).to_dict()
                            recoveries = list(sdk.inspect_recoveries(header["run_id"]))
                            return {"run_revision": current["revision"], "generation": current["generation"],
                                "episode": current["application_state"]["watchdog"]["episodes"]["blocked-author"],
                                "supervisor_task": current["tasks"][supervisor],
                                "authorization": [_authorization(current, operation, item.execution.recovery_reason)
                                                  for item in recoveries],
                                "evidence": [_evidence(root, header, sdk, current, item, operation)
                                             for item in recoveries],
                                "operation_lock_held": lock_held, "cancellation": cancellation,
                                "recoveries": [asdict(item) for item in recoveries]}
                        observations = [diagnose()]
                        atomic_json(diagnostic_path, {"observations": observations, "settled": False})
                        def proof_state(observation):
                            return (observation["operation_lock_held"], observation["cancellation"]["executions"],
                                    observation["recoveries"], observation["episode"])
                        previous = proof_state(observations[-1])
                        bound = time.monotonic() + 2
                        while not settled and time.monotonic() < bound:
                            time.sleep(.05)
                            observation = diagnose()
                            current = proof_state(observation)
                            if current == previous:
                                continue
                            observations.append(observation)
                            previous = current
                            # Retry only after a recorded proof or lock change.
                            settled = settle(root, header, sdk, read())
                            atomic_json(diagnostic_path, {"observations": observations, "settled": settled})
                    self.assertTrue(settled)
                    state = wait_until(lambda s: s["tasks"]["behavior_extract"]["attempts"][-1]["state"] == "cancelled")
                    receipt = json.loads((root / "artifacts/executions" / execution / "receipt.json").read_text())
                    self.assertEqual(receipt["effect_request"], effect.request)
                    self.assertEqual(receipt["response"]["status"], "failed")
                    self.assertEqual(receipt["response"]["outputs"]["external_outcome"], "unknown")
                    self.assertEqual(receipt["response"]["outputs"]["acceptance_status"], "unverified")
                    self.assertEqual(runtime.kernel.get_effect("modport:" + execution).state, "committed")
                    self.assertFalse(settle(root, header, sdk, state))
                    app = state["application_state"]
                    actions, _ = decide(owner, state, header, app, sdk)
                    self.assertTrue(any(action["kind"] == "new_attempt" for action in actions))
                    apply(state, actions, app)
                    state = wait_until(lambda s: len(s["tasks"]["behavior_extract"]["attempts"]) == 2
                        and s["tasks"]["behavior_extract"]["attempts"][-1]["state"] == "succeeded")
                    self.assertEqual(state["application_state"]["agent_assignments"], 3)
                    repaired = json.loads((root / "consumed-repair.json").read_text())
                    self.assertEqual(repaired["previous_execution_id"], execution)
                    self.assertEqual(repaired["instruction"], "Resume with the supplied corrected dependency")
                    for task in state["tasks"].values():
                        for attempt in task["attempts"]:
                            self.assertEqual(attempt["command"]["payload"]["options"]["deadline_epoch"], header["deadline_epoch"])
                finally:
                    host.stop(timeout=5)
                    sdk.close()


if __name__ == "__main__":
    unittest.main()
