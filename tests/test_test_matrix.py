"""Behavioral checks for matrix validation and migration-suite projection."""
from copy import deepcopy
import unittest

from modport.test_matrix import matrix_protocol, select_migration_tests, validate_matrix


def _contract():
    behaviors = [{
        "id": "behavior.main",
        "source_evidence": "src/main/java/Example.java:12",
        "preconditions": ["A configured example"],
        "action": ["Invoke the behavior"],
        "assertions": ["The result remains stable", "The original edge case is visible"],
        "side": "both",
        "test_mapping": ["test.keep", "test.merge", "test.gap"],
        "assertion_contracts": [
            {
                "assertion_id": "behavior.result",
                "text": "The result remains stable",
                "source_anchor": {"path": "src/main/java/Example.java", "start_line": 12, "end_line": 12},
                "test_ids": ["test.keep", "test.merge"],
            },
            {
                "assertion_id": "behavior.edge",
                "text": "The original edge case is visible",
                "source_anchor": {"path": "src/main/java/Example.java", "start_line": 18, "end_line": 20},
                "test_ids": ["test.gap"],
            },
        ],
    }]
    test_evidence = {
        test_id: {
            "path": f".modport/evidence/{test_id}.json",
            "result_identity": {
                "kind": "junit_xml", "gradle_task": "test",
                "classname": "example.ExampleTest", "name": test_id.replace(".", "_"),
            },
        }
        for test_id in ("test.keep", "test.merge", "test.gap")
    }
    return {
        "schema_version": 1,
        "generator_id": "characterization-agent",
        "source_fingerprint": "source-commit-1",
        "rubric_id": "acceptance-rubric",
        "rubric_version": 4,
        "behaviors": behaviors,
        "baseline_gradle_tasks": ["test"],
        "test_evidence": test_evidence,
        "baseline_evidence_files": [item["path"] for item in test_evidence.values()],
        "extension_metadata": {"owner": "source characterization"},
    }


def _matrix():
    return {
        "schema_version": 1,
        "discovery_notes": {"project": "example", "test_command": "./gradlew test"},
        "exploration_notes": ["Found the test project under example/src/test."],
        "cases": [
            {
                "test_id": "test.keep", "behavior_id": "behavior.main",
                "entry_point": "example.ExampleTest.keepsResult", "action": "invoke behavior",
                "conditions": ["configured example"], "assertion_ids": ["behavior.result"],
            },
            {
                "test_id": "test.merge", "behavior_id": "behavior.main",
                "entry_point": "example.ExampleTest.repeatsResult", "action": "invoke behavior twice",
                "conditions": [], "assertion_ids": ["behavior.result"],
                "exploration": {"strategy": "boundary inputs"},
            },
            {
                "test_id": "test.gap", "behavior_id": "behavior.main",
                "entry_point": "example.ExampleTest.edgeCase", "action": "invoke edge case",
                "conditions": ["edge input"], "assertion_ids": ["behavior.edge"],
            },
        ],
    }


def _assessment(*decisions):
    return {"schema_version": 1, "decisions": list(decisions)}


class TestMatrixTests(unittest.TestCase):
    def test_protocol_and_validation_preserve_exploration_fields(self):
        protocol = matrix_protocol()
        self.assertEqual(protocol["schema_version"], 1)
        self.assertIn("matrix_schema", protocol)
        self.assertIn("assessment_schema", protocol)
        self.assertNotIn("exploration_notes", protocol["matrix_schema"]["required"])
        self.assertEqual(protocol["matrix_schema"]["properties"]["exploration_notes"]["items"]["minLength"], 1)
        self.assertTrue(any("source defect" in item for item in protocol["guidance"]))
        self.assertTrue(any("setup and teardown" in item for item in protocol["guidance"]))
        self.assertTrue(any("operation sequence" in item and "fixture dependencies" in item
                            for item in protocol["guidance"]))
        self.assertTrue(any("observed test may pass" in item for item in protocol["guidance"]))

        matrix = _matrix()
        normalized = validate_matrix(matrix, _contract())
        self.assertEqual(normalized["cases"][0]["assertion_ids"], ["behavior.result"])
        self.assertEqual(normalized["cases"][1]["exploration"], {"strategy": "boundary inputs"})
        self.assertEqual(normalized["exploration_notes"], ["Found the test project under example/src/test."])
        self.assertEqual(matrix["cases"][1]["exploration"], {"strategy": "boundary inputs"})

    def test_exploration_notes_must_be_nonblank_strings(self):
        for notes in (None, "a string", ["", "  "], ["ok", 7]):
            with self.subTest(notes=notes), self.assertRaisesRegex(ValueError, "exploration_notes"):
                matrix = _matrix()
                matrix["exploration_notes"] = notes
                validate_matrix(matrix, _contract())

    def test_repair_decision_keeps_case_and_records_followup_without_scheduling_work(self):
        result = select_migration_tests(
            _contract(), _matrix(),
            _assessment({"test_id": "test.keep", "decision": "repair",
                         "reason": "Its fixture needs a target-compatible revision."}),
            {},
        )
        self.assertIn("test.keep", result["selected_test_ids"])
        self.assertTrue(any("pending explicit rework and fresh assessment" in item
                            for item in result["diagnostics"]))

    def test_projection_keeps_assertions_covered_by_a_retained_case_and_reports_lost_coverage(self):
        contract = _contract()
        original = deepcopy(contract)
        matrix = _matrix()
        original_matrix = deepcopy(matrix)
        assessment = _assessment(
            {"test_id": "test.merge", "decision": "merge", "reason": "Its sequence is already covered.",
             "replacement_test_ids": ["test.keep"]},
            {"test_id": "test.gap", "decision": "drop", "reason": "The action is redundant."},
        )
        original_assessment = deepcopy(assessment)
        baseline_report = {}
        original_report = deepcopy(baseline_report)
        result = select_migration_tests(
            contract,
            matrix,
            assessment,
            baseline_report,
        )

        self.assertEqual(result["selected_test_ids"], ["test.keep"])
        self.assertEqual(result["uncovered_assertion_ids"], ["behavior.edge"])
        projected = result["migration_contract"]
        self.assertEqual(projected["source_fingerprint"], "source-commit-1")
        self.assertEqual(projected["rubric_id"], "acceptance-rubric")
        self.assertEqual(projected["rubric_version"], 4)
        self.assertEqual(projected["extension_metadata"], {"owner": "source characterization"})
        behavior = projected["behaviors"][0]
        self.assertEqual(behavior["test_mapping"], ["test.keep"])
        self.assertEqual(behavior["assertions"], ["The result remains stable"])
        self.assertEqual(behavior["assertion_contracts"], [{
            "assertion_id": "behavior.result",
            "text": "The result remains stable",
            "source_anchor": {"path": "src/main/java/Example.java", "start_line": 12, "end_line": 12},
            "test_ids": ["test.keep"],
        }])
        self.assertEqual(set(projected["test_evidence"]), {"test.keep"})
        self.assertEqual(projected["baseline_evidence_files"], [".modport/evidence/test.keep.json"])
        self.assertEqual(result["excluded_cases"][0]["case"]["test_id"], "test.merge")
        self.assertEqual(result["excluded_cases"][0]["case"]["exploration"],
                         {"strategy": "boundary inputs"})
        self.assertEqual(contract, original)
        self.assertEqual(matrix, original_matrix)
        self.assertEqual(assessment, original_assessment)
        self.assertEqual(baseline_report, original_report)

    def test_unbound_source_defect_claim_is_kept_as_a_migration_test(self):
        contract = _contract()
        matrix = _matrix()
        bad_case_result = {
            "test_id": "test.gap", "status": "failed", "test_outcome": "failed",
            "category": "mod_behavior", "candidate_unchanged": True,
            "result_identity": {"kind": "junit_xml", "name": "wrong"},
            "source_commit": "source-commit-1", "execution_nonce": "nonce-1",
        }
        result = select_migration_tests(
            contract,
            matrix,
            _assessment({"test_id": "test.gap", "decision": "source_defect",
                         "reason": "The original mod already fails this case.",
                         "evidence": ["Observed a failed original-mod case."]}),
            {"source_commit": "source-commit-1", "execution_nonce": "nonce-1",
             "case_results": {"test.gap": bad_case_result}},
        )
        self.assertIn("test.gap", result["selected_test_ids"])
        self.assertEqual(result["source_defects"], [])
        self.assertTrue(any("result_identity" in item for item in result["diagnostics"]))

    def test_verified_source_defect_is_ledgered_with_original_assertion_anchor_and_case_evidence(self):
        contract = _contract()
        declaration = contract["test_evidence"]["test.gap"]
        case_result = {
            "test_id": "test.gap", "status": "failed", "test_outcome": "failed",
            "category": "mod_behavior", "candidate_unchanged": True,
            "result_identity": deepcopy(declaration["result_identity"]),
            "source_commit": "source-commit-1", "execution_nonce": "nonce-1",
            "test_result": {"outcome": "failed"},
        }
        result = select_migration_tests(
            contract,
            _matrix(),
            _assessment({"test_id": "test.gap", "decision": "source_defect",
                         "reason": "This failure is already present in the original mod.",
                         "evidence": ["Exact original-mod JUnit result"]}),
            {"source_commit": "source-commit-1", "execution_nonce": "nonce-1",
             "case_results": {"test.gap": case_result}},
        )

        self.assertEqual(result["selected_test_ids"], ["test.keep", "test.merge"])
        self.assertEqual(result["uncovered_assertion_ids"], [])
        defect = result["source_defects"][0]
        self.assertEqual(defect["original_behavior_assertions"], [
            "The result remains stable", "The original edge case is visible",
        ])
        self.assertEqual(defect["source_anchors"]["behavior.edge"], {
            "path": "src/main/java/Example.java", "start_line": 18, "end_line": 20,
        })
        self.assertEqual(defect["defect_assertion_ids"], ["behavior.edge"])
        self.assertEqual(defect["case"]["test_id"], "test.gap")
        self.assertEqual(defect["original_behavior"], contract["behaviors"][0])
        self.assertEqual(defect["case_evidence"], case_result)
        self.assertEqual(result["migration_contract"]["behaviors"][0]["assertion_contracts"], [{
            "assertion_id": "behavior.result",
            "text": "The result remains stable",
            "source_anchor": {"path": "src/main/java/Example.java", "start_line": 12, "end_line": 12},
            "test_ids": ["test.keep", "test.merge"],
        }])

    def test_source_defect_case_with_mixed_assertions_preserves_healthy_coverage_gap(self):
        contract = _contract()
        behavior = contract["behaviors"][0]
        behavior["assertions"].append("The edge case keeps its normal invariant")
        behavior["assertion_contracts"].append({
            "assertion_id": "behavior.edge_invariant",
            "text": "The edge case keeps its normal invariant",
            "source_anchor": {"path": "src/main/java/Example.java", "start_line": 18, "end_line": 20},
            "test_ids": ["test.gap"],
        })
        matrix = _matrix()
        gap_case = next(case for case in matrix["cases"] if case["test_id"] == "test.gap")
        gap_case["assertion_ids"].append("behavior.edge_invariant")
        declaration = contract["test_evidence"]["test.gap"]
        case_result = {
            "test_id": "test.gap", "status": "failed", "test_outcome": "failed",
            "category": "mod_behavior", "candidate_unchanged": True,
            "result_identity": deepcopy(declaration["result_identity"]),
            "source_commit": "source-commit-1", "execution_nonce": "nonce-1",
        }
        result = select_migration_tests(
            contract,
            matrix,
            _assessment({"test_id": "test.gap", "decision": "source_defect",
                         "reason": "The expected edge result is already incorrect in source.",
                         "defect_assertion_ids": ["behavior.edge"],
                         "evidence": ["Observed failed original-mod case"]}),
            {"source_commit": "source-commit-1", "execution_nonce": "nonce-1",
             "case_results": {"test.gap": case_result}},
        )

        self.assertEqual(result["selected_test_ids"], ["test.keep", "test.merge"])
        self.assertEqual(result["uncovered_assertion_ids"], ["behavior.edge_invariant"])
        defect = result["source_defects"][0]
        self.assertEqual(defect["assertion_ids"], ["behavior.edge", "behavior.edge_invariant"])
        self.assertEqual(defect["defect_assertion_ids"], ["behavior.edge"])
        self.assertEqual(defect["case"], gap_case)
        self.assertEqual(defect["original_behavior"], behavior)
        self.assertEqual(
            [item["assertion_id"] for item in result["migration_contract"]["behaviors"][0]["assertion_contracts"]],
            ["behavior.result"],
        )

    def test_source_defect_with_multiple_assertions_requires_explicit_defect_links(self):
        contract = _contract()
        behavior = contract["behaviors"][0]
        behavior["assertions"].append("A second edge property")
        behavior["assertion_contracts"].append({
            "assertion_id": "behavior.edge_invariant",
            "text": "A second edge property",
            "source_anchor": {"path": "src/main/java/Example.java", "start_line": 18, "end_line": 20},
            "test_ids": ["test.gap"],
        })
        matrix = _matrix()
        next(case for case in matrix["cases"] if case["test_id"] == "test.gap")["assertion_ids"].append(
            "behavior.edge_invariant"
        )
        declaration = contract["test_evidence"]["test.gap"]
        case_result = {
            "test_id": "test.gap", "status": "failed", "test_outcome": "failed",
            "category": "mod_behavior", "candidate_unchanged": True,
            "result_identity": deepcopy(declaration["result_identity"]),
            "source_commit": "source-commit-1", "execution_nonce": "nonce-1",
        }
        result = select_migration_tests(
            contract, matrix,
            _assessment({"test_id": "test.gap", "decision": "source_defect",
                         "reason": "The edge case is defective in source.",
                         "evidence": ["Observed failed original-mod result"]}),
            {"source_commit": "source-commit-1", "execution_nonce": "nonce-1",
             "case_results": {"test.gap": case_result}},
        )
        self.assertIn("test.gap", result["selected_test_ids"])
        self.assertEqual(result["source_defects"], [])
        self.assertEqual(result["uncovered_assertion_ids"], [])
        self.assertTrue(any("omitted defect_assertion_ids" in item for item in result["diagnostics"]))

    def test_projection_removes_gradle_tasks_used_only_by_excluded_cases(self):
        contract = _contract()
        contract["baseline_gradle_tasks"] = ["project:taskA", ":project:taskB", "unrelated"]
        contract["test_evidence"]["test.keep"]["result_identity"]["gradle_task"] = ":project:taskA"
        contract["test_evidence"]["test.merge"]["result_identity"]["gradle_task"] = "project:taskA"
        contract["test_evidence"]["test.gap"]["result_identity"]["gradle_task"] = "project:taskB"
        result = select_migration_tests(
            contract,
            _matrix(),
            _assessment(
                {"test_id": "test.keep", "decision": "keep", "reason": "Retain task A."},
                {"test_id": "test.merge", "decision": "merge", "reason": "Same behavior as keep."},
                {"test_id": "test.gap", "decision": "source_defect",
                 "reason": "The source implementation fails its original edge case.",
                 "evidence": ["Original baseline case result"]},
            ),
            {"source_commit": "source-commit-1", "execution_nonce": "nonce-1",
             "case_results": {"test.gap": {
                 "test_id": "test.gap", "status": "failed", "test_outcome": "failed",
                 "category": "mod_behavior", "candidate_unchanged": True,
                 "result_identity": contract["test_evidence"]["test.gap"]["result_identity"],
                 "source_commit": "source-commit-1", "execution_nonce": "nonce-1",
             }}},
        )
        self.assertEqual(result["selected_test_ids"], ["test.keep"])
        self.assertEqual(result["migration_contract"]["baseline_gradle_tasks"], ["project:taskA"])

    def test_duplicate_assessment_decisions_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "duplicate assessment decision"):
            select_migration_tests(
                _contract(), _matrix(),
                _assessment(
                    {"test_id": "test.keep", "decision": "keep", "reason": "Retain."},
                    {"test_id": "test.keep", "decision": "drop", "reason": "Remove."},
                ),
                {},
            )

    def test_all_excluded_cases_are_explicitly_not_an_accepting_empty_suite(self):
        contract = _contract()
        matrix = _matrix()
        decisions = [
            {"test_id": case["test_id"], "decision": "drop", "reason": "Planner removed this case."}
            for case in matrix["cases"]
        ]
        result = select_migration_tests(contract, matrix, _assessment(*decisions), {})
        self.assertEqual(result["selected_test_ids"], [])
        self.assertEqual(result["migration_contract"]["behaviors"], [])
        self.assertEqual(result["migration_contract"]["test_evidence"], {})
        self.assertEqual(result["migration_contract"]["baseline_evidence_files"], [])
        self.assertEqual(result["migration_contract"]["baseline_gradle_tasks"], [])
        self.assertTrue(any("cannot establish acceptance" in item for item in result["diagnostics"]))
        self.assertEqual(result["uncovered_assertion_ids"], ["behavior.result", "behavior.edge"])


if __name__ == "__main__":
    unittest.main()
