import json
from pathlib import Path
import tempfile
import unittest

from modport.monitor_progress import (
    budget_extension_decision,
    collect_progress_evidence,
    compare_progress_evidence,
)
from modport.run_monitor import RunMonitor


class MonitorProgressTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        (self.root / "artifacts").mkdir()

    def write_json(self, relative, value):
        path = self.root / "artifacts" / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value), encoding="utf-8")
        return path

    @staticmethod
    def inventory(issue_ids):
        return {
            "kind": "repair_inventory",
            "coverage": {"complete": True, "scan_complete": True,
                          "source_complete": True},
            "scan_scope": {"ruleset": "rules-1", "rule_ids": ["forge"]},
            "issues": [{"issue_id": value, "status": "open"} for value in issue_ids],
        }

    def test_only_complete_inventory_closures_are_progress(self):
        self.write_json("repair-diagnostics/a/review-inventory.json",
                        self.inventory(["a", "b"]))
        before = collect_progress_evidence(self.root)
        self.write_json("repair-diagnostics/z/review-inventory.json",
                        self.inventory(["b"]))
        after = collect_progress_evidence(self.root)
        delta = compare_progress_evidence(before, after)
        self.assertEqual(1, delta["inventory"]["closed"])
        self.assertTrue(delta["tangible_progress"])

    def test_verification_delta_uses_authenticated_host_records(self):
        self.write_json("baseline-contract-tests.json", {
            "execution_id": "verify:1",
            "diagnostics": {"authenticated_test_ids": ["one"],
                             "error_code": "assertion_failed"},
        })
        before = collect_progress_evidence(self.root)
        self.write_json("executions/verify:2/characterization-diagnostic.json", {
            "kind": "characterization_diagnostic",
            "execution_id": "verify:2",
            "authenticated_test_ids": ["two"],
        })
        after = collect_progress_evidence(self.root)
        delta = compare_progress_evidence(before, after)
        self.assertEqual(1, delta["verification"]["new_runs"])
        self.assertEqual(1, delta["verification"]["new_tests"])
        self.assertTrue(delta["tangible_progress"])

    def test_repeated_failed_verification_is_reported_without_counting_progress(self):
        diagnostic = {
            "kind": "characterization_diagnostic",
            "execution_id": "verify:1",
            "authenticated_test_ids": ["same"],
            "error_code": "assertion_failed",
            "failure_signature": "same-failure",
        }
        self.write_json("executions/verify:1/characterization-diagnostic.json", diagnostic)
        before = collect_progress_evidence(self.root)
        diagnostic["execution_id"] = "verify:2"
        self.write_json("executions/verify:2/characterization-diagnostic.json", diagnostic)
        after = collect_progress_evidence(self.root)
        delta = compare_progress_evidence(before, after)
        self.assertEqual(1, delta["verification"]["new_runs"])
        self.assertFalse(delta["verification"]["tangible"])
        self.assertFalse(delta["tangible_progress"])

    def test_unverified_pass_claim_is_not_verification_evidence(self):
        self.write_json("verification.json", {
            "execution_id": "untrusted:1", "status": "passed",
        })
        evidence = collect_progress_evidence(self.root)
        self.assertEqual(0, evidence["public"]["verification"]["run_count"])
        delta = compare_progress_evidence(None, evidence)
        self.assertFalse(delta["tangible_progress"])

    def test_candidate_docs_do_not_count_but_product_bytes_do(self):
        self.write_json("goal-checks/a/candidate-files.json", {
            "README.md": {"sha256": "docs-1"},
            "src/main/java/A.java": {"sha256": "source-1"},
        })
        before = collect_progress_evidence(self.root)
        self.write_json("goal-checks/z/candidate-files.json", {
            "README.md": {"sha256": "docs-2"},
            "src/main/java/A.java": {"sha256": "source-2"},
        })
        after = collect_progress_evidence(self.root)
        delta = compare_progress_evidence(before, after)
        self.assertEqual(1, delta["patch_bytes"]["candidate_changed_files"])
        self.assertEqual(1, delta["patch_bytes"]["source_changed_files"])
        self.assertTrue(delta["tangible_progress"])
        unchanged = collect_progress_evidence(self.root)
        delta = compare_progress_evidence(after, unchanged)
        self.assertEqual(0, delta["patch_bytes"]["candidate_changed_files"])
        self.assertEqual(0, delta["patch_bytes"]["source_changed_files"])
        self.assertFalse(delta["tangible_progress"])

    def test_xvfb_keysym_warning_is_not_a_dependency_failure(self):
        log = self.root / "artifacts" / "repair-diagnostics" / "verify" / "log.txt"
        log.parent.mkdir(parents=True)
        log.write_text("> Warning:          Could not resolve keysym XF86CameraAccessEnable\n",
                       encoding="utf-8")
        evidence = collect_progress_evidence(self.root)
        self.assertEqual(0, evidence["public"]["dependency_failures"]["count"])

        log.write_text(log.read_text(encoding="utf-8")
                       + "> Could not resolve net.neoforged:neoforge:26.1.2\n",
                       encoding="utf-8")
        evidence = collect_progress_evidence(self.root)
        self.assertEqual(1, evidence["public"]["dependency_failures"]["count"])

    def test_budget_extension_is_evidence_bound_and_absolutely_capped(self):
        progress = {"tangible_progress": True, "inventory": {"closed": 1},
                    "verification": {"new_runs": 0, "new_tests": 0}}
        decision = budget_extension_decision(
            progress, now=9 * 3600, started_at=0,
            current_deadline=8 * 3600)
        self.assertTrue(decision["eligible"])
        self.assertEqual(3 * 3600, decision["additional_seconds"])

        no_progress = dict(progress, tangible_progress=False)
        decision = budget_extension_decision(
            no_progress, now=9 * 3600, started_at=0,
            current_deadline=8 * 3600)
        self.assertFalse(decision["eligible"])
        self.assertEqual("no_authenticated_progress", decision["reason"])

        decision = budget_extension_decision(
            progress, now=7 * 3600, started_at=0,
            current_deadline=8 * 3600)
        self.assertFalse(decision["eligible"])
        self.assertEqual("initial_budget_not_exhausted", decision["reason"])

    def test_monitor_uses_original_progress_policy_cap_for_successor_segment(self):
        (self.root / "run.json").write_text(json.dumps({
            "started_at": 10, "deadline_epoch": 20,
        }), encoding="utf-8")
        policy = self.root / "artifacts" / "progress-continuations" / "segment"
        policy.mkdir(parents=True)
        (policy / "policy.json").write_text(json.dumps({
            "started_at": 10,
            "request": {"next_run_id": "segment:progress-extension",
                         "initial_seconds": 28800, "maximum_seconds": 43200},
            "extension": {"next_run_id": "segment:progress-extension"},
        }), encoding="utf-8")
        monitor = RunMonitor(self.root, "segment:progress-extension",
                             self.root / "monitor")
        self.assertEqual((10.0, 20.0, 28800, 43200, "progress_policy"),
                         monitor._run_budget())


if __name__ == "__main__":
    unittest.main()
