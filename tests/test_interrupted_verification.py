"""An uncertain client timeout is reconciled only with independent evidence."""

from hashlib import sha256
import json
from pathlib import Path
import subprocess
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from modport.contracts import OperationInput, OperationResult
from modport.evidence import atomic_json, digest, read_json
from modport.interrupted_verification import (SCHEMA, capture_workspace_facts,
                                              reconcile_interrupted_verification)


class InterruptedVerificationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        baseline = self.root / "baseline"
        (baseline / ".modport").mkdir(parents=True)
        (baseline / ".modport/functional-contract.json").write_text('{"tests":[]}', encoding="utf-8")
        subprocess.run(["git", "init", "-q", str(baseline)], check=True)
        subprocess.run(["git", "-C", str(baseline), "add", "."], check=True)
        subprocess.run(["git", "-C", str(baseline), "-c", "user.email=test@example.invalid",
                        "-c", "user.name=Test", "commit", "-qm", "baseline"], check=True)
        head = subprocess.check_output(["git", "-C", str(baseline), "rev-parse", "HEAD"], text=True).strip()
        atomic_json(self.root / "artifacts/source.json", {"source_commit": head})
        self.operation = OperationInput("r", "contract_verify", "contract_verify", "r:contract_verify:1", str(self.root))
        self.directory = self.root / "artifacts/executions" / self.operation.command_id
        atomic_json(self.directory / "input.json", self.operation.to_dict())
        atomic_json(self.directory / "before.json", {"scope": "run", "workspace": None})
        raw = (b'MODPORT_CLIENT_PREFLIGHT '
               + json.dumps({"execution_id": self.operation.command_id,
                             "error_code": "workload_timeout"}).encode() + b"\n")
        log = self.root / "audit-logs/original/combined.log"
        log.parent.mkdir(parents=True)
        log.write_bytes(raw)
        self.effect = SimpleNamespace(
            execution_id=self.operation.command_id, name="modport.stage", state="indeterminate",
            effect_id="modport:r:contract_verify:1", revision=3,
            response={"code": "effect_call_raised", "message": "workload timed out",
                      "exception_type": "TimeoutError"},
            request={"input_sha256": digest(self.operation.to_dict()),
                     "run_dir": str(self.root), "stage": "contract_verify"})
        self.command = {"payload": self.operation.to_dict()}
        self.evidence = {
            "schema": SCHEMA, "operator_id": "operator-1", "decision_id": "manual-timeout-1",
            "execution_id": self.operation.command_id, "effect_id": self.effect.effect_id,
            "effect_revision": self.effect.revision, "reason": "workload_timeout",
            "timeout_log": {"path": log.relative_to(self.root).as_posix(),
                            "sha256": sha256(raw).hexdigest()},
            "before_sha256": sha256((self.directory / "before.json").read_bytes()).hexdigest(),
            "workspace": capture_workspace_facts(self.root),
            "process_scan": {"status": "clear", "checked_at": time.time(),
                             "boot_id": "test-boot", "matching_pids": []}}
        scan = patch("modport.interrupted_verification.capture_process_scan",
                     return_value={"status": "clear", "boot_id": "test-boot",
                                   "checked_at": time.time(), "matching_pids": []})
        scan.start()
        self.addCleanup(scan.stop)

    def _reconcile(self, evidence=None, effect=None):
        return reconcile_interrupted_verification(self.root, self.command,
                                                   effect or self.effect,
                                                   evidence=evidence or self.evidence)

    def test_valid_timeout_emits_failed_receipt_recoverable_by_old_handler(self):
        result = self._reconcile()
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["error_code"], "contract_verify_interrupted")
        self.assertEqual(result["outputs"]["acceptance_status"], "unverified")
        self.assertFalse(result["outputs"]["artifacts_complete"])
        self.assertEqual(read_json(self.directory / "receipt.json")["response"], result)
        self.assertEqual(read_json(self.directory / "interrupted-verification.json")["effect_request"], self.effect.request)
        self.assertEqual(self._reconcile(), result)

    def test_frozen_effect_or_input_mismatch_cannot_write_receipt(self):
        changed = SimpleNamespace(**{**vars(self.effect), "revision": 4})
        with self.assertRaisesRegex(ValueError, "effect or revision"):
            self._reconcile(effect=changed)
        atomic_json(self.directory / "input.json", {"different": True})
        with self.assertRaisesRegex(ValueError, "input differs"):
            self._reconcile()
        self.assertFalse((self.directory / "receipt.json").exists())

    def test_forged_timeout_event_and_stale_process_scan_rejected(self):
        log = self.root / self.evidence["timeout_log"]["path"]
        changed = log.read_bytes().replace(b"workload_timeout", b"ok")
        log.write_bytes(changed)
        evidence = {**self.evidence, "timeout_log": {**self.evidence["timeout_log"],
                            "sha256": sha256(changed).hexdigest()}}
        with self.assertRaisesRegex(ValueError, "no matching workload_timeout"):
            self._reconcile(evidence=evidence)
        log.write_bytes(log.read_bytes().replace(b'"ok"', b'"workload_timeout"'))
        stale = {**self.evidence, "process_scan": {**self.evidence["process_scan"],
                                                   "checked_at": time.time() - 1000}}
        with self.assertRaisesRegex(ValueError, "fresh same-host"):
            self._reconcile(evidence=stale)
        self.assertFalse((self.directory / "receipt.json").exists())

    def test_existing_receipt_takes_precedence_over_operator_evidence(self):
        original = OperationResult("failed", "r", "contract_verify", "contract_verify",
                                   self.operation.command_id, error_code="original_failure").to_dict()
        atomic_json(self.directory / "receipt.json", {
            "execution_id": self.operation.command_id, "effect_request": self.effect.request,
            "response": original, "after": {"scope": "run", "workspace": None}})
        self.assertEqual(self._reconcile(evidence={"invalid": True}), original)
        self.assertFalse((self.directory / "interrupted-verification.json").exists())

    def test_live_process_or_new_boot_refuses_manual_adjudication(self):
        with patch("modport.interrupted_verification.capture_process_scan", return_value={
                "status": "active", "boot_id": "test-boot", "matching_pids": [1444]}):
            with self.assertRaisesRegex(ValueError, "still active"):
                self._reconcile()
        with patch("modport.interrupted_verification.capture_process_scan", return_value={
                "status": "clear", "boot_id": "later-boot", "matching_pids": []}):
            with self.assertRaisesRegex(ValueError, "rebooted"):
                self._reconcile()
        self.assertFalse((self.directory / "receipt.json").exists())

    def test_changed_source_and_preexisting_unrelated_note_refuse_receipt(self):
        atomic_json(self.root / "artifacts/source.json", {"source_commit": "0" * 40})
        with self.assertRaisesRegex(ValueError, "baseline HEAD disagrees"):
            self._reconcile()
        head = self.evidence["workspace"]["baseline_head"]
        atomic_json(self.root / "artifacts/source.json", {"source_commit": head})
        atomic_json(self.directory / "interrupted-verification.json", {"operator_id": "another"})
        with self.assertRaisesRegex(ValueError, "different interruption adjudication"):
            self._reconcile()
        self.assertFalse((self.directory / "receipt.json").exists())

    def test_sdk_parked_effect_diagnostic_accepted_but_business_success_refused(self):
        from dispatcher_sdk.execution_kernel import ExecutionError
        uncertain = {"code": "effect_outcome_uncertain", "message": "execution authority ended",
                     "retryable": False, "details": {"trigger": "reap_timeout",
                     "prior_state": "performing", "attempt": 1, "fence": 1},
                     "schema_version": ExecutionError.__dataclass_fields__["schema_version"].default}
        response = self._reconcile(effect=SimpleNamespace(**{**vars(self.effect), "response": uncertain}))
        self.assertEqual(response["error_code"], "contract_verify_interrupted")
        self.assertEqual(read_json(self.directory / "interrupted-verification.json")["original_effect_diagnostic"], uncertain)
        (self.directory / "receipt.json").unlink()
        (self.directory / "interrupted-verification.json").unlink()
        completed = SimpleNamespace(**{**vars(self.effect), "response": {"status": "completed"}})
        with self.assertRaisesRegex(ValueError, "not an SDK uncertainty diagnostic"):
            self._reconcile(effect=completed)
        self.assertFalse((self.directory / "receipt.json").exists())

    def test_crash_after_note_can_resume_with_fresh_same_boot_process_scan(self):
        def interrupt_receipt(path, value):
            if path.name == "receipt.json":
                raise RuntimeError("simulated interruption after durable note")
            atomic_json(path, value)

        with patch("modport.interrupted_verification.atomic_json", side_effect=interrupt_receipt):
            with self.assertRaisesRegex(RuntimeError, "simulated interruption"):
                self._reconcile()
        note = self.directory / "interrupted-verification.json"
        initial_bytes = note.read_bytes()
        fresh = {**self.evidence, "process_scan": {
            **self.evidence["process_scan"], "checked_at": time.time()}}
        self.assertEqual(self._reconcile(evidence=fresh)["error_code"], "contract_verify_interrupted")
        self.assertEqual(note.read_bytes(), initial_bytes)
        (self.directory / "receipt.json").unlink()
        changed = {**fresh, "decision_id": "another-operator-decision"}
        with self.assertRaisesRegex(ValueError, "different interruption adjudication"):
            self._reconcile(evidence=changed)


if __name__ == "__main__":
    unittest.main()
