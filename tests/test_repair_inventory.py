import json
from pathlib import Path
import re
import tempfile
import unittest
from unittest.mock import patch

from modport.repair_inventory import InventoryRule, collect_inventory


class RepairInventoryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self):
        self.temporary.cleanup()

    def write(self, relative, text=""):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def issues_for(self, report, rule_id):
        return [issue for issue in report["issues"] if issue["rule_id"] == rule_id]

    def test_source_locations_merge_by_file_rule_and_symbol(self):
        self.write(
            "core/src/main/java/example/Example.java",
            "package example;\n"
            "import net.minecraftforge.eventbus.api.SubscribeEvent;\n"
            "\n"
            "net.minecraftforge.eventbus.api.SubscribeEvent marker;\n",
        )
        report = collect_inventory(self.root, candidate_id="candidate", execution_id="exec")

        matches = self.issues_for(report, "forge-package-reference")
        self.assertEqual(1, len(matches))
        self.assertEqual([2, 4], [location["line"] for location in matches[0]["locations"]])
        self.assertEqual("core/src/main/java/example/Example.java",
                         matches[0]["locations"][0]["path"])
        self.assertEqual("candidate", report["candidate_id"])
        self.assertEqual("exec", report["execution_id"])
        self.assertFalse(report["acceptance_evidence"])

    def test_issue_id_does_not_depend_on_line_number(self):
        path = self.write("src/main/java/A.java", "import net.minecraftforge.common.MinecraftForge;\n")
        first = self.issues_for(collect_inventory(self.root), "forge-package-reference")[0]
        path.write_text("\n\n" + path.read_text(encoding="utf-8"), encoding="utf-8")
        second = self.issues_for(collect_inventory(self.root), "forge-package-reference")[0]

        self.assertEqual(first["issue_id"], second["issue_id"])
        self.assertNotEqual(first["locations"][0]["line"], second["locations"][0]["line"])

    def test_scans_multiple_modules_resource_paths_and_evidenced_build_configuration(self):
        self.write("alpha/src/main/resources/data/forge/tags/blocks/common.json", "{}")
        self.write("beta/src/main/resources/data/example/forge/recipes/old.json", "{}")
        self.write("alpha/build.gradle", "dependencies { minecraft 'net.minecraftforge:forge:1.20-47.1' }")
        self.write("beta/build.gradle.kts", "// the word Forge alone is not evidence")

        report = collect_inventory(self.root)

        resources = self.issues_for(report, "forge-resource-path")
        self.assertEqual(
            {
                "alpha/src/main/resources/data/forge/tags/blocks/common.json",
                "beta/src/main/resources/data/example/forge/recipes/old.json",
            },
            {issue["locations"][0]["path"] for issue in resources},
        )
        self.assertEqual(1, len(self.issues_for(report, "forge-build-dependency")))

    def test_collects_java_and_kotlin_compiler_errors_and_missing_inputs(self):
        java = self.write("app/src/main/java/example/App.java", "first\nBadType value;\n")
        kotlin = self.write("lib/src/main/kotlin/example/Thing.kt", "val good = 1\nmissing()\n")
        logs = {
            "javac": f"{java}:2: error: cannot find symbol\n",
            "kotlin": (
                f"e: file://{kotlin}: (2, 1): Unresolved reference: missing\n"
                "e: file:///workspace/lib/src/main/kotlin/example/Thing.kt:2:1 "
                "Unresolved reference: missing\n"
            ),
        }

        report = collect_inventory(self.root, log_texts=logs,
                                   missing_inputs=["target loader source", "target loader source"])

        compiler = self.issues_for(report, "compiler-error")
        self.assertEqual(2, len(compiler))
        self.assertEqual({"BadType value;", "missing()"},
                         {issue["locations"][0]["excerpt"] for issue in compiler})
        kotlin_issue = next(issue for issue in compiler if issue["locations"][0]["path"].endswith("Thing.kt"))
        self.assertEqual(2, len(kotlin_issue["evidence"]))
        missing = self.issues_for(report, "missing-input")
        self.assertEqual(1, len(missing))
        self.assertEqual(0, missing[0]["locations"][0]["line"])

    def test_does_not_follow_symlinks_and_discloses_incomplete_coverage(self):
        outside = Path(self.temporary.name).parent / (self.root.name + "-outside.java")
        outside.write_text("import net.minecraftforge.common.MinecraftForge;\n", encoding="utf-8")
        try:
            (self.root / "Linked.java").symlink_to(outside)
            report = collect_inventory(self.root)
        finally:
            outside.unlink(missing_ok=True)

        self.assertEqual([], self.issues_for(report, "forge-package-reference"))
        self.assertFalse(report["coverage"]["scan_complete"])
        self.assertFalse(report["coverage"]["complete"])
        self.assertIn({"path": "Linked.java", "reason": "symbolic_link"},
                      report["coverage"]["skipped"])

    def test_limits_mark_scan_truncated_so_empty_results_cannot_prove_absence(self):
        self.write("a.java", "class A {}\n")
        self.write("b.java", "import net.minecraftforge.common.MinecraftForge;\n")

        with patch("modport.repair_inventory.MAX_FILES", 1):
            report = collect_inventory(self.root)

        self.assertTrue(report["coverage"]["truncated"])
        self.assertFalse(report["coverage"]["scan_complete"])
        self.assertFalse(report["coverage"]["zero_issues_proves_absence"])
        self.assertIn("file_count_limit", report["coverage"]["truncation_reasons"])

    def test_issue_location_and_skipped_output_limits_are_bounded(self):
        self.write(
            "A.java",
            "net.minecraftforge.A first;\nnet.minecraftforge.A second;\n",
        )
        self.write("B.java", "net.minecraftforge.B value;\n")
        with patch("modport.repair_inventory.MAX_ISSUES", 1), \
                patch("modport.repair_inventory.MAX_LOCATIONS", 1):
            report = collect_inventory(self.root)
        self.assertEqual(1, len(report["issues"]))
        self.assertEqual(1, len(report["issues"][0]["locations"]))
        self.assertTrue(report["coverage"]["issues_omitted"]
                        or report["coverage"]["locations_omitted"])
        self.assertFalse(report["coverage"]["source_complete"])
        self.assertFalse(report["coverage"]["complete"])

        with patch("modport.repair_inventory.MAX_LOCATIONS", 1):
            locations = collect_inventory(self.root)
        self.assertEqual(1, locations["coverage"]["locations_returned"])
        self.assertGreaterEqual(locations["coverage"]["locations_omitted"], 1)
        self.assertIn("location_limit", locations["coverage"]["truncation_reasons"])

        outside = self.root.parent / (self.root.name + "-outside")
        outside.write_text("outside", encoding="utf-8")
        try:
            (self.root / "one-link").symlink_to(outside)
            (self.root / "two-link").symlink_to(outside)
            with patch("modport.repair_inventory.MAX_SKIPPED", 1):
                skipped = collect_inventory(self.root)
        finally:
            outside.unlink(missing_ok=True)
        self.assertEqual(1, len(skipped["coverage"]["skipped"]))
        self.assertGreaterEqual(skipped["coverage"]["skipped_omitted"], 1)
        self.assertIn("skipped_entry_limit", skipped["coverage"]["truncation_reasons"])

    def test_log_bytes_are_bounded_and_have_independent_scope(self):
        source = self.write("src/main/java/A.java", "Missing first;\nMissing second;\n")
        first = f"{source}:1: error: first failure\n"
        logs = {"compile": first + f"{source}:2: error: second failure\n"}
        with patch("modport.repair_inventory.MAX_LOG_BYTES", len(first.encode("utf-8"))):
            report = collect_inventory(self.root, log_texts=logs)

        self.assertEqual(1, len(self.issues_for(report, "compiler-error")))
        self.assertFalse(report["coverage"]["logs_complete"])
        self.assertTrue(report["coverage"]["source_complete"])
        self.assertIn("log_byte_limit", report["coverage"]["truncation_reasons"])
        self.assertEqual(["compile"], report["scan_scope"]["log_sources"])
        self.assertEqual(["compile"], report["scan_scope"]["scanned_log_sources"])
        self.assertEqual(
            {"parser": "compiler-diagnostics-v3", "sources": ["compile"], "executions": [{
                "source": "compile",
                "build_status": "unknown",
                "execution_scope_known": False,
                "tasks": [],
                "tasks_truncated": False,
            }]},
            report["scan_scope"]["log_scope"],
        )

        absent = collect_inventory(self.root)
        self.assertEqual(report["scan_scope"]["ruleset"], absent["scan_scope"]["ruleset"])
        self.assertEqual(report["scan_scope"]["source_files"], absent["scan_scope"]["source_files"])
        self.assertFalse(absent["scan_scope"]["logs_provided"])
        self.assertEqual([], absent["scan_scope"]["log_sources"])

    def test_multiline_javac_symbol_distinguishes_stable_issue_ids(self):
        source = self.write(
            "src/main/java/A.java",
            "MissingOne one;\nMissingTwo two;\n",
        )

        def compile_log(offset):
            return (
                f"{source}:{1 + offset}: error: cannot find symbol\n"
                "  MissingOne one;\n  ^\n  symbol: class MissingOne\n"
                f"{source}:{2 + offset}: error: cannot find symbol\n"
                "  MissingTwo two;\n  ^\n  symbol: class MissingTwo\n"
            )

        first = collect_inventory(self.root, log_texts={"compile": compile_log(0)})
        source.write_text("\n\n" + source.read_text(encoding="utf-8"), encoding="utf-8")
        second = collect_inventory(self.root, log_texts={"compile": compile_log(2)})
        first_ids = {issue["locations"][0]["symbol"]: issue["issue_id"]
                     for issue in self.issues_for(first, "compiler-error")}
        second_ids = {issue["locations"][0]["symbol"]: issue["issue_id"]
                      for issue in self.issues_for(second, "compiler-error")}
        self.assertEqual(2, len(first_ids))
        self.assertEqual(first_ids, second_ids)
        self.assertIn("cannot find symbol: class missingone", first_ids)

    def test_rejects_workspace_with_symlink_ancestor(self):
        container = self.root / "container"
        real = container / "real"
        workspace = real / "workspace"
        workspace.mkdir(parents=True)
        self.write("container/real/workspace/A.java",
                   "import net.minecraftforge.common.MinecraftForge;\n")
        linked = container / "linked"
        linked.symlink_to(real, target_is_directory=True)

        report = collect_inventory(
            linked / "workspace",
            log_texts={"compile": "/workspace/A.java:1: error: cannot find symbol\n"},
        )

        source_issues = self.issues_for(report, "forge-package-reference")
        self.assertEqual([], source_issues)
        compiler = self.issues_for(report, "compiler-error")
        self.assertEqual(1, len(compiler))
        self.assertNotEqual("import net.minecraftforge.common.MinecraftForge;",
                            compiler[0]["locations"][0]["excerpt"])
        self.assertFalse(report["coverage"]["source_complete"])
        self.assertIn({"path": ".", "reason": "workspace_symlink_component"},
                      report["coverage"]["skipped"])

    def test_extension_rules_and_observations_share_the_bounded_schema(self):
        self.write("src/main/java/A.java", "// REVIEW_ME\n")
        custom = InventoryRule("review-marker", "review", "Review marker remains",
                               re.compile(r"REVIEW_ME"), "source")
        report = collect_inventory(
            self.root,
            rules=(custom,),
            observations=[{
                "category": "runtime_failure",
                "summary": "Client failed during reload",
                "path": "src/main/java/A.java",
                "line": 1,
                "symbol": "resource-reload",
                "evidence": "runtime:client",
            }],
        )

        self.assertEqual(1, len(self.issues_for(report, "review-marker")))
        self.assertEqual(1, len(self.issues_for(report, "caller-observation")))
        self.assertEqual(["runtime_failure"], report["scan_scope"]["observation_categories"])

    def test_final_serialized_report_limit_truncates_without_becoming_complete(self):
        for index in range(120):
            self.write(
                f"module-{index:03d}/src/main/java/Example{index}.java",
                f"net.minecraftforge.example.Type{index} value;\n",
            )
        with patch("modport.repair_inventory.MAX_REPORT_BYTES", 8_000):
            report = collect_inventory(self.root, log_texts={})
        encoded = json.dumps(
            report, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")

        self.assertLessEqual(len(encoded), 8_000)
        self.assertEqual(len(encoded), report["coverage"]["report_bytes"])
        self.assertFalse(report["coverage"]["complete"])
        self.assertFalse(report["coverage"]["source_complete"])
        self.assertIn("report_byte_limit", report["coverage"]["truncation_reasons"])
        self.assertGreater(report["coverage"]["issues_omitted"], 0)

    def test_compiler_truncation_marker_makes_log_coverage_incomplete(self):
        self.write("app/src/main/java/A.java", "Missing value;\n")
        log = (
            "> Task :app:compileJava FAILED\n"
            "/workspace/app/src/main/java/A.java:1: error: cannot find symbol\n"
            "only showing the first 100 errors, of 147 total; use -Xmaxerrs if you would like to see more\n"
            "BUILD FAILED in 2s\n"
        )

        report = collect_inventory(self.root, log_texts={"gradle_log:target_build": log})

        self.assertFalse(report["coverage"]["logs_complete"])
        self.assertFalse(report["coverage"]["scan_complete"])
        self.assertIn("compiler_diagnostics_truncated",
                      report["coverage"]["truncation_reasons"])
        scope = report["scan_scope"]["log_scope"]
        self.assertEqual("compiler-diagnostics-v3", scope["parser"])
        self.assertEqual("failed", scope["executions"][0]["build_status"])
        self.assertEqual(
            [{"task": ":app:compileJava", "status": "failed"}],
            scope["executions"][0]["tasks"],
        )

    def test_unknown_compiler_execution_scope_is_conservatively_incomplete(self):
        self.write("src/main/java/A.java", "Missing value;\n")
        report = collect_inventory(
            self.root,
            log_texts={"compile": "/workspace/src/main/java/A.java:1: error: cannot find symbol\n"},
        )

        self.assertFalse(report["coverage"]["logs_complete"])
        self.assertFalse(report["coverage"]["scan_complete"])
        self.assertIn("compiler_execution_scope_unknown",
                      report["coverage"]["incomplete_reasons"])
        self.assertFalse(
            report["scan_scope"]["log_scope"]["executions"][0]["execution_scope_known"]
        )

    def test_log_task_scope_limit_is_bounded_and_incomplete(self):
        log = (
            "> Task :alpha:compileJava\n"
            "> Task :beta:compileJava FAILED\n"
            "BUILD FAILED in 1s\n"
        )
        with patch("modport.repair_inventory.MAX_LOG_TASKS", 1):
            report = collect_inventory(self.root, log_texts={"compile": log})

        execution = report["scan_scope"]["log_scope"]["executions"][0]
        self.assertEqual(1, len(execution["tasks"]))
        self.assertTrue(execution["tasks_truncated"])
        self.assertFalse(report["coverage"]["logs_complete"])
        self.assertIn("log_task_scope_limit", report["coverage"]["truncation_reasons"])

    def test_successful_gradle_scope_records_task_status(self):
        report = collect_inventory(
            self.root,
            log_texts={"compile": (
                "> Task :app:compileJava UP-TO-DATE\n"
                "BUILD SUCCESSFUL in 1s\n"
            )},
        )

        execution = report["scan_scope"]["log_scope"]["executions"][0]
        self.assertEqual("successful", execution["build_status"])
        self.assertTrue(execution["execution_scope_known"])
        self.assertEqual(
            [{"task": ":app:compileJava", "status": "up_to_date"}],
            execution["tasks"],
        )
        self.assertTrue(report["coverage"]["logs_complete"])

    def test_earlier_module_failure_changes_scope_and_cannot_close_hidden_issue(self):
        from modport.repair_progress import _repair_inventory

        self.write("moduleA/src/main/java/A.java", "MissingA value;\n")
        self.write("moduleB/src/main/java/B.java", "MissingB value;\n")
        before_log = (
            "> Task :moduleB:compileJava FAILED\n"
            "/workspace/moduleB/src/main/java/B.java:1: error: cannot find symbol\n"
            "  symbol: class MissingB\n"
            "BUILD FAILED in 2s\n"
        )
        after_log = (
            "> Task :moduleA:compileJava FAILED\n"
            "/workspace/moduleA/src/main/java/A.java:1: error: cannot find symbol\n"
            "  symbol: class MissingA\n"
            "BUILD FAILED in 1s\n"
        )
        before = collect_inventory(
            self.root, candidate_id="before", log_texts={"gradle_log:target_build": before_log}
        )
        after = collect_inventory(
            self.root, candidate_id="after", log_texts={"gradle_log:target_build": after_log}
        )

        comparison = _repair_inventory(
            {"before_inventory": before, "after_inventory": after}, [],
            current_candidate="after", previous_candidate="before",
        )
        self.assertTrue(before["coverage"]["logs_complete"])
        self.assertTrue(after["coverage"]["logs_complete"])
        self.assertNotEqual(before["scan_scope"]["log_scope"],
                            after["scan_scope"]["log_scope"])
        self.assertFalse(comparison["comparable"])
        self.assertEqual(0, comparison["closed_issue_count"])


if __name__ == "__main__":
    unittest.main()
