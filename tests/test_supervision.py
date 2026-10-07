"""Deterministic five-attempt supervision; no agent or project execution."""
import copy
import unittest

from modport.supervision import (SUPERVISOR_PROMPT, build_evidence_packet,
                                 due_windows, validate_supervisor_decision)


class SupervisionTests(unittest.TestCase):
    def attempts(self, count=5):
        return [{"execution_id": f"execution-{index}", "stage": "coder", "state": "succeeded",
                 "result": {"status": "failed", "error_code": "compile_error"},
                 "blocker": {"error": "cannot_find_symbol", "file": "src/Example.java"},
                 "progress": {"tests_passed": 2},
                 "artifact_refs": {"diagnostic": {"sha256": str(index)}}}
                for index in range(1, count + 1)]

    def decision(self, kind="continue"):
        return {"schema_version": 1, "decision": kind, "reason": "Observed the complete window",
                "evidence_execution_ids": [f"execution-{i}" for i in range(1, 6)],
                "process_improvements": ["Resolve the repeated symbol mismatch from pinned sources"]}

    def validate(self, value):
        return validate_supervisor_decision(value,
            evidence_execution_ids=self.decision()["evidence_execution_ids"],
            allowed_stages=["target_revise"], allowed_profiles=["coder"], allowed_task_ids=["task-a"])

    def test_every_five_assignments_without_waiting_for_results(self):
        self.assertEqual((), due_windows(4))
        self.assertEqual((5,), due_windows(5))
        self.assertEqual((10, 15), due_windows(17, [5]))
        attempts = self.attempts(6)
        attempts[4].update(state="running", result=None, blocker=None)
        packet = build_evidence_packet(attempts)
        self.assertEqual(5, len(packet["attempts"]))
        self.assertEqual("running", packet["attempts"][-1]["state"])
        self.assertIsNone(packet["attempts"][-1]["result"])
        self.assertNotIn("execution-6", packet["evidence_execution_ids"])

    def test_exact_historical_window_excludes_future_results(self):
        attempts = self.attempts(12)
        attempts[-1]["execution_id"] = attempts[0]["execution_id"]
        packet = build_evidence_packet(attempts, window_end=10)
        self.assertEqual(list(range(6, 11)), [row["ordinal"] for row in packet["attempts"]])
        self.assertEqual([6, 7, 8, 9, 10], [row["blocker"]["repeat_count"] for row in packet["attempts"]])

    def test_rewritten_diagnostics_and_repeated_completion_are_not_progress(self):
        attempts = self.attempts(10)
        packet = build_evidence_packet(attempts)
        self.assertFalse(packet["tangible_progress"])
        self.assertEqual(["diagnostic"], packet["attempts"][0]["delta"]["changed_artifact_refs"])
        for row in attempts:
            row.update(result={"status": "completed"}, blocker=None)
        self.assertFalse(build_evidence_packet(attempts)["tangible_progress"])
        self.assertTrue(build_evidence_packet(attempts, window_end=5)["tangible_progress"])

    def test_progress_deltas_and_regressions_are_concrete(self):
        attempts = self.attempts()
        attempts[3]["progress"]["tests_passed"] = 3
        packet = build_evidence_packet(attempts)
        gain, regression = packet["attempts"][3:]
        self.assertTrue(gain["tangible_progress"])
        self.assertEqual({"before": 2, "after": 3, "change": 1}, gain["delta"]["metrics"]["tests_passed"])
        self.assertFalse(regression["tangible_progress"])
        self.assertEqual(-1, regression["delta"]["metrics"]["tests_passed"]["change"])

    def test_blocker_identity_separates_files_stages_and_errors(self):
        attempts = self.attempts()
        attempts[1]["blocker"]["file"] = "src/Other.java"
        attempts[2]["stage"] = "target_revise"
        attempts[3]["blocker"]["error"] = "type_mismatch"
        rows = build_evidence_packet(attempts)["attempts"]
        self.assertEqual([1, 1, 1, 1, 2], [row["blocker"]["repeat_count"] for row in rows])
        self.assertEqual(rows[0]["blocker"]["signature"], rows[4]["blocker"]["signature"])

    def test_no_mutation_or_aliasing_and_deterministic_packets(self):
        attempts = self.attempts()
        original = copy.deepcopy(attempts)
        packet = build_evidence_packet(attempts)
        self.assertEqual(packet, build_evidence_packet(attempts))
        packet["attempts"][0]["result"]["status"] = "completed"
        self.assertEqual(original, attempts)
        decision = self.decision()
        validated = self.validate(decision)
        validated["process_improvements"].clear()
        self.assertTrue(decision["process_improvements"])

    def test_malformed_windows_and_supervisor_recursion_rejected(self):
        for end in (True, 0, 4, 6, 10):
            with self.subTest(end=end), self.assertRaises(ValueError):
                build_evidence_packet(self.attempts(), window_end=end)
        for count in (-1, True, 5.0):
            with self.assertRaises(ValueError):
                due_windows(count)
        with self.assertRaises(ValueError):
            due_windows(10, [True])
        attempts = self.attempts()
        attempts[0]["stage"] = "supervisor"
        with self.assertRaises(ValueError):
            build_evidence_packet(attempts)

    def test_all_decisions_and_allowlisted_interventions(self):
        for kind in ("continue", "pause", "targeted_fix", "replan"):
            value = self.decision(kind)
            if kind in {"targeted_fix", "replan"}:
                value["intervention"] = {"prompt": "Check the pinned declaration", "task_ids": ["task-a"],
                                         "stage": "target_revise", "profile": "coder"}
            self.assertEqual(value, self.validate(value))

    def test_decisions_cannot_waive_host_policy_or_select_unknown_work(self):
        for field in ("budget", "skip_tests", "validators", "workspace", "manifest", "command"):
            for nested in (False, True):
                value = self.decision("replan")
                value["intervention"] = {"prompt": "Repair the pinned API call"}
                (value["intervention"] if nested else value)[field] = "override"
                with self.subTest(field=field, nested=nested), self.assertRaises(ValueError):
                    self.validate(value)
        for field, item in (("task_ids", ["other"]), ("stage", "deliver"), ("profile", "unrestricted"),
                            ("task_ids", []), ("task_ids", ["task-a", "task-a"]), ("prompt", " ")):
            value = self.decision("replan")
            value["intervention"] = {field: item}
            with self.subTest(field=field, item=item), self.assertRaises(ValueError):
                self.validate(value)

    def test_exact_evidence_binding_and_action_shape(self):
        for ids in (["execution-1"], list(reversed(self.decision()["evidence_execution_ids"])),
                    ["execution-1"] * 5):
            value = self.decision()
            value["evidence_execution_ids"] = ids
            with self.assertRaises(ValueError):
                self.validate(value)
        for kind in ("continue", "pause"):
            value = self.decision(kind)
            value["intervention"] = {"prompt": "Modify work"}
            with self.assertRaises(ValueError):
                self.validate(value)
        for kind in ("targeted_fix", "replan"):
            with self.assertRaises(ValueError):
                self.validate(self.decision(kind))
        value = self.decision()
        value["schema_version"] = True
        with self.assertRaises(ValueError):
            self.validate(value)

    def test_prompt_preserves_host_authority(self):
        for requirement in ("budgets", "tests", "validators", "five", "concurrently", "host inputs"):
            self.assertIn(requirement, SUPERVISOR_PROMPT)


if __name__ == "__main__":
    unittest.main()
