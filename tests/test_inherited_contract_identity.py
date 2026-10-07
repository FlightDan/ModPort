"""Selected handoff identities survive a fresh v25 verifier, including early failures."""

import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from modport.contracts import OperationInput
from modport.evidence import file_digest
from modport.handlers import BaselineContractVerificationHandler


class InheritedContractIdentityTests(unittest.TestCase):
    def _fixture(self, root: Path, *, selected_ids, candidate_ids,
                 selected_contract_id="selected-contract", candidate_contract_id="selected-contract"):
        original = root / "artifacts/parent-evidence/functional-contract.json"
        candidate = root / "baseline/.modport/functional-contract.json"
        manifest = root / "artifacts/inherited-harness.json"
        source = root / "artifacts/source.json"
        for path in (original, candidate, manifest, source):
            path.parent.mkdir(parents=True, exist_ok=True)

        def contract(contract_id, ids):
            value = {"source_fingerprint": "source-commit",
                     "behaviors": [{"id": item, "source_evidence": "src/Example.java:1",
                                    "preconditions": ["fixture exists"], "action": ["invoke fixture"],
                                    "assertions": ["fixture responds"], "side": "both",
                                    "test_mapping": [item + ".test"]} for item in ids],
                     "baseline_gradle_tasks": []}
            if contract_id is not None:
                value["contract_id"] = contract_id
            return value

        original.write_text(json.dumps(contract(selected_contract_id, selected_ids)))
        candidate.write_text(json.dumps(contract(candidate_contract_id, candidate_ids)))
        source.write_text(json.dumps({"source_commit": "source-commit"}))
        selected_ref = {"path": original.relative_to(root).as_posix(),
                        "sha256": file_digest(original)}
        manifest.write_text(json.dumps({"source_commit": "source-commit",
            "files": [{"path": ".modport/functional-contract.json", "ref": selected_ref}]}))
        manifest_ref = {"path": manifest.relative_to(root).as_posix(),
                        "sha256": file_digest(manifest)}
        return OperationInput("identity-run", "contract_verify", "contract_verify",
            "identity-run:contract_verify:1", str(root),
            options={"workflow_version": 25},
            artifact_refs={"inherited_harness": manifest_ref,
                           "inherited_harness:.modport/functional-contract.json": selected_ref})

    def test_allowed_revision_preserves_selected_ids_through_early_failure(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            command = self._fixture(root, selected_ids=["a"], candidate_ids=["a", "b"])
            candidate = root / "baseline/.modport/functional-contract.json"
            revised = json.loads(candidate.read_text())
            revised["description"] = "Repair the selected behavior's source link."
            candidate.write_text(json.dumps(revised))
            result = BaselineContractVerificationHandler()(command)
            identity = result.outputs["inherited_contract_identity"]
            self.assertEqual("failed", result.status)
            self.assertEqual("baseline_tasks_invalid", result.error_code)
            self.assertEqual("preserved", identity["status"])
            self.assertEqual([], identity["missing_behavior_ids"])
            self.assertEqual(["b"], identity["added_behavior_ids"])
            self.assertNotEqual(identity["selected_artifact_sha256"],
                                identity["candidate_sha256"])

    def test_dropped_id_is_diagnostic_without_changing_verifier_result(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            command = self._fixture(root, selected_ids=["a", "b"], candidate_ids=["a"])
            result = BaselineContractVerificationHandler()(command)
            identity = result.outputs["inherited_contract_identity"]
            self.assertEqual("failed", result.status)
            self.assertEqual("baseline_tasks_invalid", result.error_code)
            self.assertEqual("changed", identity["status"])
            self.assertEqual(["b"], identity["missing_behavior_ids"])
            self.assertEqual("unverified", result.outputs["acceptance_status"])
            self.assertIn("inherited contract identity observation: changed",
                          result.outputs["business_diagnostics"])

    def test_tampered_selected_ref_is_reported_without_overwriting_failure(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            command = self._fixture(root, selected_ids=["a"], candidate_ids=["a"])
            original = root / "artifacts/parent-evidence/functional-contract.json"
            original.write_text(original.read_text() + "\n")
            result = BaselineContractVerificationHandler()(command)
            self.assertEqual("failed", result.status)
            self.assertEqual("baseline_tasks_invalid", result.error_code)
            self.assertEqual("unavailable",
                result.outputs["inherited_contract_identity"]["status"])
            self.assertIn("digest differs",
                result.outputs["inherited_contract_identity"]["diagnostic"])

    def test_missing_selected_id_uses_contract_default_instead_of_wildcard(self):
        with tempfile.TemporaryDirectory() as raw:
            command = self._fixture(Path(raw), selected_ids=["a"], candidate_ids=["a"],
                                    selected_contract_id=None, candidate_contract_id="replacement")
            result = BaselineContractVerificationHandler()(command)
            identity = result.outputs["inherited_contract_identity"]
            self.assertEqual("changed", identity["status"])
            self.assertEqual("modport.functional-contract.v1", identity["selected_contract_id"])
            self.assertEqual("replacement", identity["candidate_contract_id"])

    def test_malformed_manifest_ref_does_not_override_verifier_error(self):
        with tempfile.TemporaryDirectory() as raw:
            command = self._fixture(Path(raw), selected_ids=["a"], candidate_ids=["a"])
            refs = dict(command.artifact_refs)
            refs["inherited_harness"] = None
            malformed = replace(command, artifact_refs=refs)
            result = BaselineContractVerificationHandler()(malformed)
            self.assertEqual("failed", result.status)
            self.assertEqual("baseline_tasks_invalid", result.error_code)
            self.assertEqual("unavailable",
                             result.outputs["inherited_contract_identity"]["status"])


if __name__ == "__main__":
    unittest.main()
