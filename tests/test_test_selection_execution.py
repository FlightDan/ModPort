"""Selected behavior-test execution inputs remain exact and host generated."""

import json
import unittest

from modport.test_selection_execution import (
    build_selected_test_execution,
    find_executed_excluded_junit_cases,
)


def contract_with_cases():
    return {
        "test_evidence": {
            "keep.alpha": {
                "evidence_kind": "runtime",
                "executor": "junit",
                "result_identity": {
                    "kind": "junit_xml",
                    "gradle_task": ":test",
                    "classname": "example.AlphaTests",
                    "name": "opensScreen",
                },
            },
            "keep.inner": {
                "evidence_kind": "runtime",
                "executor": "junit",
                "result_identity": {
                    "kind": "junit_xml",
                    "gradle_task": "test",
                    "classname": "example.Outer$InnerTests",
                    "name": "opensScreen",
                },
            },
            "remove.beta": {
                "evidence_kind": "runtime",
                "executor": "junit",
                "result_identity": {
                    "kind": "junit_xml",
                    "gradle_task": ":integrationTest",
                    "classname": "example.BetaTests",
                    "name": "keepsLegacyValue",
                },
            },
        },
    }


class SelectedTestExecutionTests(unittest.TestCase):
    def test_builds_exact_task_filters_and_custom_harness_environment(self):
        selected = build_selected_test_execution(
            contract_with_cases(), ["keep.alpha", "keep.inner"],
        )

        self.assertEqual((":test",), selected.gradle_tasks)
        self.assertEqual(("keep.alpha", "keep.inner"), selected.test_ids)
        self.assertEqual(
            ["keep.alpha", "keep.inner"],
            json.loads(selected.environment["MODPORT_SELECTED_TEST_IDS"]),
        )
        script = selected.gradle_init_script
        self.assertIn("':test': ['example.AlphaTests.opensScreen', 'example.Outer$InnerTests.opensScreen']", script)
        self.assertIn("graphTask.filter.setIncludePatterns(*selectedPatterns)", script)
        self.assertIn("graphTask.filter.setFailOnNoMatchingTests(true)", script)
        self.assertIn("modportSelectedTaskPaths - observedSelectedTaskPaths", script)
        self.assertIn("modportSelectedTaskPaths.contains(graphTask.path)", script)
        self.assertIn("graphTask instanceof Test", script)
        self.assertIn("graphTask.enabled = false", script)
        self.assertIn("Selected Gradle tasks are absent from this graph", script)
        self.assertNotIn("__MODPORT_NO_SELECTED_TEST__", script)

    def test_rejects_missing_duplicate_or_unsafe_selection(self):
        contract = contract_with_cases()
        with self.assertRaisesRegex(ValueError, "requires at least one"):
            build_selected_test_execution(contract, [])
        with self.assertRaisesRegex(ValueError, "unique safe identifiers"):
            build_selected_test_execution(contract, ["keep.alpha", "keep.alpha"])
        with self.assertRaisesRegex(ValueError, "not declared"):
            build_selected_test_execution(contract, ["missing"])

        contract["test_evidence"]["keep.alpha"]["result_identity"]["classname"] = "Example.*"
        with self.assertRaisesRegex(ValueError, "classname"):
            build_selected_test_execution(contract, ["keep.alpha"])

    def test_allows_a_selected_custom_harness_task_to_reach_the_graph(self):
        contract = contract_with_cases()
        contract["test_evidence"]["keep.alpha"]["result_identity"]["gradle_task"] = ":runGameTestServer"

        selected = build_selected_test_execution(contract, ["keep.alpha"])

        self.assertEqual((":runGameTestServer",), selected.gradle_tasks)
        self.assertEqual(
            ["keep.alpha"],
            json.loads(selected.environment["MODPORT_SELECTED_TEST_IDS"]),
        )
        self.assertIn("modportSelectedTaskPaths.contains(graphTask.path)",
                      selected.gradle_init_script)
        self.assertNotIn("not Test tasks in this graph", selected.gradle_init_script)

    def test_rejects_reused_junit_identity_and_non_junit_case(self):
        contract = contract_with_cases()
        contract["test_evidence"]["keep.inner"]["result_identity"] = dict(
            contract["test_evidence"]["keep.alpha"]["result_identity"],
        )
        with self.assertRaisesRegex(ValueError, "reuse a JUnit result identity"):
            build_selected_test_execution(contract, ["keep.alpha", "keep.inner"])

        contract["test_evidence"]["keep.alpha"]["executor"] = "client_smoke"
        with self.assertRaisesRegex(ValueError, "runtime JUnit executor"):
            build_selected_test_execution(contract, ["keep.alpha"])

    def test_detects_only_excluded_exact_identities_from_their_gradle_task(self):
        xml = b"""<?xml version='1.0'?>
<testsuite>
  <testcase classname='example.BetaTests' name='keepsLegacyValue'/>
  <testcase classname='example.OtherTests' name='keepsLegacyValue'/>
  <testcase classname='example.BetaTests' name='differentMethod'/>
</testsuite>"""
        reports = find_executed_excluded_junit_cases(
            contract_with_cases(), ["remove.beta"],
            {":integrationTest": [xml]},
        )

        self.assertEqual([{
            "test_id": "remove.beta",
            "gradle_task": ":integrationTest",
            "classname": "example.BetaTests",
            "name": "keepsLegacyValue",
            "outcome": "passed",
        }], reports)

    def test_ignores_same_test_identity_reported_by_a_different_task(self):
        xml = b"<testsuite><testcase classname='example.BetaTests' name='keepsLegacyValue'/></testsuite>"
        reports = find_executed_excluded_junit_cases(
            contract_with_cases(), ["remove.beta"], {":test": [xml]},
        )
        self.assertEqual([], reports)

    def test_empty_exclusion_set_needs_no_report_and_malformed_xml_is_rejected(self):
        self.assertEqual([], find_executed_excluded_junit_cases(
            contract_with_cases(), [], {},
        ))
        with self.assertRaisesRegex(ValueError, "malformed"):
            find_executed_excluded_junit_cases(
                contract_with_cases(), ["remove.beta"],
                {":integrationTest": [b"<testsuite>"]},
            )
        with self.assertRaisesRegex(ValueError, "forbidden XML declaration"):
            find_executed_excluded_junit_cases(
                contract_with_cases(), ["remove.beta"],
                {":integrationTest": [b"<!DOCTYPE testsuite><testsuite/>"]},
            )


if __name__ == "__main__":
    unittest.main()
