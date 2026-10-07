"""Current skill-only CLI routing under the diagnostic-only workflow policy."""

import tempfile
import unittest

from modport.contracts import OperationResult, json_copy
from modport.memory_admission import MemorySnapshot
from modport.models import Budget, MigrationRequest
from modport.operations import MigrationOperations
from modport.workflow import compile_migration_workflow


class SkillGenerationFlowthroughTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="modport-skill-flowthrough-")
        self.addCleanup(temporary.cleanup)
        request = MigrationRequest(
            "java-skill", "skill://standalone", "0", "0", source_java="17",
            target_java="25", workflow_mode="skill_generation", skill_kind="java",
            budget=Budget(max_seconds=600, max_agent_assignments=4,
                          max_rework_rounds=0, execution_max_attempts=1),
        )
        self.header = {
            "request": request.to_dict(), "definition": compile_migration_workflow(request).to_dict(),
            "run_id": "skill-flowthrough", "run_dir": temporary.name,
            "deadline_epoch": None, "initial_refs": {}, "prior_findings": [],
            "registry_revision": "a" * 64, "rubric_sha256": "b" * 64,
        }
        self.snapshot = {"run_id": "skill-flowthrough", "state": "running",
                         "tasks": {}, "waits": {}, "application_state": {}}
        self.operations = MigrationOperations(memory_probe=lambda: MemorySnapshot(
            64 * 1024**3, 64 * 1024**3, "skill-flowthrough-test"))

    def tick(self):
        operations, app = self.operations._decision(self.snapshot, self.header)
        self.snapshot["application_state"] = json_copy(app)
        for operation in operations:
            kind, task_id = operation["kind"], operation.get("task_id")
            if kind == "add_task":
                self.snapshot["tasks"][task_id] = {
                    "dependencies": operation["dependencies"],
                    "attempts": [{"state": "pending", "command": operation["command"]}],
                }
            elif kind == "dispatch":
                self.snapshot["tasks"][task_id]["attempts"][-1]["state"] = "running"
            elif kind == "finish":
                self.snapshot["state"] = operation["state"]
        return operations

    def complete(self, stage, outputs, *, status="completed", error_code=None):
        attempt = self.snapshot["tasks"][stage]["attempts"][-1]
        command = attempt["command"]["payload"]
        attempt.update(state="succeeded", result={"value": OperationResult(
            status, "skill-flowthrough", stage, command["stage_id"],
            command["command_id"], outputs, error_code=error_code).to_dict()})

    def test_standalone_java_skill_routes_generator_reviewer_and_publish(self):
        self.tick()
        self.assertEqual({"skill_lookup"}, set(self.snapshot["tasks"]))
        self.assertNotIn("source", self.snapshot["tasks"])

        self.complete("skill_lookup", {"missing_kinds": ["java"], "needs_review_kinds": []})
        self.tick()
        self.assertIn("java_diff", self.snapshot["tasks"])
        self.assertNotIn("skill_publish", self.snapshot["tasks"])

        self.complete("java_diff", {"candidate": "generated"})
        self.tick()
        self.assertIn("java_skill_review", self.snapshot["tasks"])
        self.assertNotIn("skill_publish", self.snapshot["tasks"])

        self.complete("java_skill_review", {"verdict": "approved"})
        self.tick()
        self.assertIn("skill_publish", self.snapshot["tasks"])
        self.assertNotIn("source", self.snapshot["tasks"])
        # Diagnostic-only v25 routing forwards the settled review result in
        # command input rather than requiring SDK business-success gating.
        publish_command = self.snapshot["tasks"]["skill_publish"]["attempts"][-1]["command"]
        self.assertEqual([], self.snapshot["tasks"]["skill_publish"]["dependencies"])
        self.assertEqual("approved", publish_command["payload"]["upstream_results"][
            "java_skill_review"]["outputs"]["verdict"])

    def test_frozen_v24_skill_entry_keeps_its_prior_route(self):
        self.header["definition"]["workflow_version"] = 24
        self.tick()
        self.assertIn("source", self.snapshot["tasks"])
        self.assertNotIn("skill_lookup", self.snapshot["tasks"])

    def test_unreviewed_cached_skill_goes_directly_to_independent_review(self):
        self.tick()
        self.complete("skill_lookup", {"missing_kinds": [], "needs_review_kinds": ["java"]})
        self.tick()
        self.assertIn("java_skill_review", self.snapshot["tasks"])
        self.assertNotIn("java_diff", self.snapshot["tasks"])
        self.complete("java_skill_review", {"verdict": "approved"})
        self.tick()
        self.assertIn("skill_publish", self.snapshot["tasks"])

    def test_failed_generator_result_reaches_reviewer_as_diagnostic(self):
        self.tick()
        self.complete("skill_lookup", {"missing_kinds": ["java"], "needs_review_kinds": []})
        self.tick()
        self.complete("java_diff", {}, status="failed", error_code="candidate_invalid")
        self.tick()
        self.assertIn("java_skill_review", self.snapshot["tasks"])
        command = self.snapshot["tasks"]["java_skill_review"]["attempts"][-1]["command"]
        self.assertEqual("failed", command["payload"]["upstream_results"]["java_diff"]["status"])
        self.assertNotIn("skill_publish", self.snapshot["tasks"])


if __name__ == "__main__":
    unittest.main()
