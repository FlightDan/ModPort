"""Public SDK tests for the narrow interrupted-verification reopen route."""

from pathlib import Path
from hashlib import sha256
import json
import subprocess
import tempfile
import time
import unittest
from unittest.mock import patch

from dispatcher_sdk.orchestrator import Orchestrator

from fixtures_modport import Clock, registry
from modport import MigrationOperations, MigrationRequest
from modport.contracts import OperationResult, json_copy
from modport.evidence import atomic_json, read_json
from modport.interrupted_verification import (SCHEMA, capture_workspace_facts,
                                              reconcile_interrupted_verification)
from modport.payload_storage import unpack_result
from modport.operations import _verify_recovery_payload
from modport.verification_recovery import _validate_source, reopen_verification


class InterruptedVerification:
    __execution_kernel_revision__ = "modport-interrupted-verification-test-v1"

    def __init__(self):
        self.park = False

    def __call__(self, operation):
        if self.park:
            raise RuntimeError("fixture interrupted external workload")
        return OperationResult(
            "failed", operation.run_id, operation.task_id,
            operation.stage_id, operation.command_id,
            error_code="contract_verify_interrupted", detail="interrupted host effect",
        )


class VerificationRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "run"
        self.clock = Clock(1000.0)
        handlers = registry()
        self.verifier = InterruptedVerification()
        handlers["modport.contract_verify"] = self.verifier
        self.ops = MigrationOperations(
            handlers=handlers, isolation_mode="thread", clock=self.clock)
        self.ops.submit(MigrationRequest(
            "mod", "https://example.invalid/mod.git", "1.20.1", "26.1.2"),
            run_dir=self.root, run_id="interrupted")

    def seed(self, *, frozen=False, downstream=False, adjudicate=False):
        with self.ops.session(self.root, "interrupted") as (_, header, runtime, sdk):
            app = self.ops._new_application()
            app["early_active"] = True
            draft = OperationResult(
                "completed", "interrupted", "contract_draft", "contract_draft",
                "interrupted:contract_draft:1").to_dict()
            app["effective"]["contract_draft"] = draft
            if frozen:
                app["locked_artifacts"]["contract_sha256"] = "a" * 64
            if downstream:
                app["effective"]["contract_review"] = draft
            operations = self.ops._schedule(
                sdk.get_run("interrupted"), header, app,
                "contract_verify", dependencies=[], activate=False,
            )
            sdk.apply_operations(
                "interrupted", command_id="initial-verification", expected_revision=0,
                operations=operations, application_state=app,
            )
            sdk.flush()
            if adjudicate:
                self.verifier.park = True
                parked = runtime.run_once()
                self.assertEqual("recovery_required", parked.state)
                sdk.sync()
                command = sdk.get_run("interrupted")["tasks"]["contract_verify"]["attempts"][-1]["command"]
                effect = runtime.kernel.get_effect("modport:" + command["execution_id"])
                self.assertEqual("indeterminate", effect.state)
                self._adjudicate_receipt(command, effect, sdk)
            runtime.run_once()
            sdk.sync()
            sdk.pump_results()
            self.ops._drain_result_delivery(sdk)
            state = sdk.get_run("interrupted")
            attempt = state["tasks"]["contract_verify"]["attempts"][-1]
            failure = attempt["result"]["value"]
            app["effective"]["contract_verify"] = failure
            app["early_pending"] = ["contract_verify"]
            sdk.apply_operations(
                "interrupted", command_id="maintenance-boundary",
                expected_revision=state["revision"],
                operations=[{"kind": "finish", "state": "failed"}],
                application_state=app,
            )

    def _adjudicate_receipt(self, command, effect, sdk):
        baseline = self.root / "baseline"
        (baseline / ".modport").mkdir(parents=True)
        (baseline / ".modport/functional-contract.json").write_text('{"tests":[]}', encoding="utf-8")
        subprocess.run(["git", "init", "-q", str(baseline)], check=True)
        subprocess.run(["git", "-C", str(baseline), "add", "."], check=True)
        subprocess.run(["git", "-C", str(baseline), "-c", "user.email=test@example.invalid",
                        "-c", "user.name=Test", "commit", "-qm", "baseline"], check=True)
        head = subprocess.check_output(["git", "-C", str(baseline), "rev-parse", "HEAD"], text=True).strip()
        atomic_json(self.root / "artifacts/source.json", {"source_commit": head})
        raw = (b"MODPORT_CLIENT_PREFLIGHT " + json.dumps({
            "execution_id": command["execution_id"], "error_code": "workload_timeout",
        }).encode() + b"\n")
        log = self.root / "audit-logs/original/combined.log"
        log.parent.mkdir(parents=True)
        log.write_bytes(raw)
        evidence = {
            "schema": SCHEMA, "operator_id": "test-operator", "decision_id": "test-adjudication",
            "execution_id": command["execution_id"], "effect_id": effect.effect_id,
            "effect_revision": effect.revision, "reason": "workload_timeout",
            "timeout_log": {"path": log.relative_to(self.root).as_posix(),
                            "sha256": sha256(raw).hexdigest()},
            "before_sha256": sha256((self.root / "artifacts/executions" / command["execution_id"] / "before.json").read_bytes()).hexdigest(),
            "workspace": capture_workspace_facts(self.root),
            "process_scan": {"status": "clear", "checked_at": time.time(),
                             "boot_id": "test-boot", "matching_pids": []},
        }
        with patch("modport.interrupted_verification.capture_process_scan", return_value={
                "status": "clear", "boot_id": "test-boot", "checked_at": time.time(), "matching_pids": []}):
            response = reconcile_interrupted_verification(
                self.root, command, effect, evidence=evidence)
        self.assertEqual("contract_verify_interrupted", unpack_result(self.root, response)["error_code"])
        sdk.resolve_effect(effect.effect_id, decision="applied", response=response,
                           expected_revision=effect.revision, recovery_id="test:adjudicate")
        receipt = read_json(self.root / "artifacts/executions" / command["execution_id"] / "receipt.json")
        self.assertEqual("contract_verify_interrupted", receipt["response"]["error_code"])

    def test_reopen_keeps_original_input_deadline_budget_and_history(self):
        self.seed()
        original = self.ops.status(self.root, "interrupted").snapshot
        recovered = reopen_verification(
            self.ops, self.root, "interrupted", "host effect settled")
        state = recovered.snapshot
        self.assertEqual(1, state["generation"])
        self.assertEqual("running", state["state"])
        self.assertEqual(original["input"], state["input"])
        self.assertEqual(original["definition"], state["definition"])
        self.assertEqual(2, len(state["tasks"]["contract_verify"]["attempts"]))
        self.assertEqual("contract_verify_interrupted", state["tasks"]["contract_verify"]["attempts"][0]["result"]["value"]["error_code"])
        self.assertEqual(0, state["application_state"]["agent_assignments"])
        self.assertNotIn("recovery_deadline_epoch", state["application_state"])
        self.assertNotIn("recovery_budget_override", state["application_state"])
        self.assertEqual(["contract_verify"], state["application_state"]["early_pending"])
        operation = state["tasks"]["contract_verify"]["attempts"][-1]["command"]["payload"]
        self.assertEqual(original["input"]["deadline_epoch"], operation["options"]["deadline_epoch"])
        self.assertEqual(original["input"]["request"], operation["payload"]["request"])
        with self.ops.session(self.root, "interrupted") as (_, execution_header, runtime, _):
            self.assertEqual(runtime.registry_revision, execution_header["registry_revision"])
            self.assertEqual(original["input"]["deadline_epoch"], execution_header["deadline_epoch"])
            self.assertEqual(original["input"]["request"]["budget"], execution_header["request"]["budget"])
        self.assertEqual(state["generation"], reopen_verification(
            self.ops, self.root, "interrupted", "host effect settled").snapshot["generation"])
        with self.assertRaisesRegex(ValueError, "replay differs"):
            reopen_verification(self.ops, self.root, "interrupted", "different request")

    def test_prepared_packet_replays_after_crash_before_sdk_commit(self):
        self.seed()
        with patch.object(Orchestrator, "reopen_run", side_effect=RuntimeError("crash")):
            with self.assertRaisesRegex(RuntimeError, "crash"):
                reopen_verification(self.ops, self.root, "interrupted", "retry")
        packet_path = self.root / "artifacts/recoveries/reopen:contract_verify_interrupted:g1/prepared.json"
        packet = read_json(packet_path)
        _verify_recovery_payload(self.root, packet)
        with self.assertRaisesRegex(ValueError, "replay differs"):
            reopen_verification(self.ops, self.root, "interrupted", "different request")
        self.assertEqual(1, reopen_verification(
            self.ops, self.root, "interrupted", "retry").snapshot["generation"])

    def test_adjudicated_receipt_replays_through_sdk_before_reopen(self):
        self.seed(adjudicate=True)
        original = self.ops.status(self.root, "interrupted").snapshot
        attempt = original["tasks"]["contract_verify"]["attempts"][-1]
        self.assertEqual("succeeded", attempt["state"])
        self.assertEqual("contract_verify_interrupted", attempt["result"]["value"]["error_code"])
        self.assertTrue(attempt["result"]["value"]["outputs"]["operator_adjudicated"])
        restored = reopen_verification(
            self.ops, self.root, "interrupted", "adjudicated timeout")
        self.assertEqual("running", restored.snapshot["state"])
        self.assertEqual(2, len(restored.snapshot["tasks"]["contract_verify"]["attempts"]))

    def test_mutated_prepared_packet_rejected_before_sdk_mutation(self):
        self.seed()
        with patch.object(Orchestrator, "reopen_run", side_effect=RuntimeError("crash")):
            with self.assertRaisesRegex(RuntimeError, "crash"):
                reopen_verification(self.ops, self.root, "interrupted", "retry")
        packet_path = self.root / "artifacts/recoveries/reopen:contract_verify_interrupted:g1/prepared.json"
        packet = read_json(packet_path)
        packet["operations"].append({"kind": "finish", "state": "failed"})
        atomic_json(packet_path, packet)
        with patch.object(Orchestrator, "reopen_run") as reopening:
            with self.assertRaisesRegex(ValueError, "payload digest mismatch"):
                reopen_verification(self.ops, self.root, "interrupted", "retry")
            reopening.assert_not_called()

    def test_rejects_frozen_contract(self):
        self.seed(frozen=True)
        with self.assertRaisesRegex(ValueError, "frozen contract"):
            reopen_verification(self.ops, self.root, "interrupted", "retry")

    def test_rejects_downstream_results(self):
        self.seed(downstream=True)
        with self.assertRaisesRegex(ValueError, "downstream results"):
            reopen_verification(self.ops, self.root, "interrupted", "retry")

    def test_rejects_expired_original_deadline(self):
        self.seed()
        deadline = read_json(self.root / "run.json")["deadline_epoch"]
        self.clock.now = deadline + 1
        with self.assertRaisesRegex(ValueError, "deadline expired"):
            reopen_verification(self.ops, self.root, "interrupted", "retry")

    def test_kernel_failure_with_no_business_value_is_rejected(self):
        self.seed()
        header = read_json(self.root / "run.json")
        state = json_copy(self.ops.status(self.root, "interrupted").snapshot)
        last = state["tasks"]["contract_verify"]["attempts"][-1]
        last["state"] = "cancelled"
        last["result"]["value"] = None
        with self.assertRaisesRegex(ValueError, "truthful interruption result"):
            _validate_source(self.ops, header, state)


if __name__ == "__main__":
    unittest.main()
