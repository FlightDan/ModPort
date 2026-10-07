"""Current policy fixtures shared by research and obligation tests."""
from pathlib import Path
import tempfile
import unittest

from modport.contracts import OperationResult, json_copy
from modport.evidence import file_digest
from modport.models import MigrationRequest
from modport.operations import MigrationOperations
from modport.memory_admission import MemorySnapshot
class EarlyPolicyTests(unittest.TestCase):
    def setUp(self):
        self.operations = MigrationOperations(memory_probe=lambda: MemorySnapshot(64 * 1024**3, 64 * 1024**3, "fixture"))
        temporary = tempfile.TemporaryDirectory(prefix='modport-early-policy-')
        self.addCleanup(temporary.cleanup)
        request = MigrationRequest("early", "https://example.invalid/mod.git", "1.20.1", "26.1.2")
        self.header = {"request": request.to_dict(), "deadline_epoch": None, "run_dir": temporary.name,
                       "initial_refs": {}, "prior_findings": [], "registry_revision": "a" * 64,
                       "rubric_sha256": "b" * 64}
        self.snapshot = {"run_id": "early", "state": "running", "tasks": {}, "waits": {},
                         "application_state": {}}

    def tick(self):
        operations, app = self.operations._decision(self.snapshot, self.header)
        self.snapshot["application_state"] = json_copy(app)
        for operation in operations:
            kind, task_id = operation["kind"], operation.get("task_id")
            if kind == "add_task":
                self.snapshot["tasks"][task_id] = {"dependencies": operation["dependencies"], "attempts": [
                    {"state": "pending", "command": operation["command"]}]}
            elif kind == "new_attempt":
                self.snapshot["tasks"][task_id]["attempts"].append({"state": "pending", "command": operation["command"]})
            elif kind == "dispatch":
                task = self.snapshot["tasks"][task_id]
                for dependency in task["dependencies"]:
                    self.assertIn(dependency, self.snapshot["tasks"])
                    self.assertEqual(self.snapshot["tasks"][dependency]["attempts"][-1]["state"], "succeeded")
                task["attempts"][-1]["state"] = "running"
            elif kind == "finish":
                self.assertTrue(all(task["attempts"][-1]["state"] in
                    {"succeeded", "failed", "cancelled", "dead", "timed_out"}
                    for task in self.snapshot["tasks"].values()))
                self.snapshot["state"] = operation["state"]
            elif kind == "cancel":
                self.snapshot["tasks"][task_id]["attempts"][-1]["state"] = "cancelled"
        return operations

    def complete(self, task_id, *, status="completed", outputs=None, error_code=None):
        attempt = self.snapshot["tasks"][task_id]["attempts"][-1]
        command = attempt["command"]["payload"]
        attempt.update(state="succeeded", result={"value": OperationResult(status, "early", task_id,
            command["stage_id"], command["command_id"], outputs or {}, error_code=error_code).to_dict()})

    def complete_goal(self, name):
        task_id = 'goal.g1.' + name
        self.assertNotIn('coder.g1.' + name, self.snapshot['tasks'])
        self.complete(task_id, outputs={'artifact_refs': {'coder_goal': self.fixture_artifact(
            task_id + '.json', '{"task_id":"' + name + '"}')}})
        self.tick()

    def fixture_artifact(self, name, text):
        root = Path(self.header['run_dir'])
        path = root / 'artifacts' / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        return {'path': path.relative_to(root).as_posix(), 'sha256': file_digest(path)}

    def test_frontier_is_persisted_and_an_idle_tick_does_not_redispatch(self):
        self.tick()
        for stage in ("source", "background", "preparation", "environment"):
            self.complete(stage)
            self.tick()
        self.assertEqual(set(self.snapshot["application_state"]["early_pending"]),
                         {"project_init", "baseline_build", "skill_lookup"})
        self.complete("skill_lookup", outputs={"missing_kinds": ["platform", "java"]})
        self.tick()
        self.assertEqual(set(self.snapshot["application_state"]["early_pending"]),
                         {"project_init", "baseline_build", "platform_diff", "java_diff"})
        before = json_copy(self.snapshot["application_state"])
        changes = self.tick()
        # The fifth business assignment launches an asynchronous supervisor;
        # the persisted business frontier itself must remain unchanged.
        self.assertTrue(changes)
        self.assertTrue(all(change.get("task_id") == "supervisor.window.5"
                            for change in changes))
        after = self.snapshot["application_state"]
        self.assertEqual(before["early_pending"], after["early_pending"])
        self.assertEqual(before["agent_assignments"], after["agent_assignments"])

    def test_contract_rejection_waits_for_repair_before_new_verification(self):
        self.tick()
        for stage in ('source', 'background', 'preparation', 'environment',
                      'project_init', 'baseline_build', 'contract_draft', 'contract_verify'):
            self.complete(stage)
            self.tick()
        self.complete('contract_review', outputs={'verdict': 'rejected'})
        self.tick()
        for stage in ('contract_diagnose', 'contract_repair_plan', 'contract_repair_tasks', 'contract_repair_review'):
            self.assertEqual(len(self.snapshot['tasks']['contract_verify']['attempts']), 1)
            self.assertEqual(len(self.snapshot['tasks']['contract_review']['attempts']), 1)
            self.assertNotIn('contract_freeze', self.snapshot['tasks'])
            self.complete(stage)
            self.tick()
        self.complete('contract_revise', outputs={'goal_scope': 'contract',
            'development_base': 'a' * 40, 'development_tasks': [
                {'id': 'repair', 'dependencies': [], 'model': 'gpt-5.6-luna', 'reasoning_effort': 'max'}]})
        self.tick()
        self.assertEqual(len(self.snapshot['tasks']['contract_verify']['attempts']), 1)
        self.complete_goal('repair')
        self.assertEqual(len(self.snapshot['tasks']['contract_verify']['attempts']), 1)
        self.complete('coder.g1.repair', outputs={'artifact_refs': {
            'coder_patch': self.fixture_artifact('repair.patch', 'fixture patch')}})
        self.tick()
        self.assertEqual(len(self.snapshot['tasks']['contract_verify']['attempts']), 1)
        self.assertNotIn('contract_freeze', self.snapshot['tasks'])
        self.complete('contract_repair_integrate')
        self.tick()
        self.assertEqual(len(self.snapshot['tasks']['contract_verify']['attempts']), 2)
        self.assertEqual(self.snapshot['tasks']['contract_verify']['attempts'][-1]['command']['causation_id'],
                         self.snapshot['tasks']['contract_repair_integrate']['attempts'][-1]['command']['execution_id'])
        self.complete('contract_verify')
        self.tick()
        self.complete('contract_review', outputs={'verdict': 'approved'})
        self.tick()
        self.assertEqual(len(self.snapshot['tasks']['contract_review']['attempts']), 2)
        self.assertIn('contract_freeze', self.snapshot['tasks'])

    def test_complete_unreviewed_skill_schedules_only_review(self):
        for mode in ("migration", "skill_generation"):
            with self.subTest(mode=mode):
                self.setUp()
                self.header["request"]["workflow_mode"] = mode
                self.tick()
                if mode == "migration":
                    for stage in ("source", "background", "preparation", "environment"):
                        self.complete(stage)
                        self.tick()
                self.complete("skill_lookup", outputs={"missing_kinds": [], "needs_review_kinds": ["java"]})
                self.tick()
                self.assertNotIn("java_diff", self.snapshot["tasks"])
                self.assertEqual(self.snapshot["tasks"]["java_skill_review"]["dependencies"], ["skill_lookup"])
                self.complete("java_skill_review", outputs={"verdict": "approved"})
                self.tick()
                self.assertIn("skill_publish", self.snapshot["tasks"])

    def start_background_research(self, *, reassessing=False):
        self.tick()
        for stage in ("source", "background", "preparation", "environment"):
            self.complete(stage)
            self.tick()
        for stage in ("project_init", "baseline_build", "skill_lookup", "skill_publish", "mod_scan"):
            self.complete(stage)
            self.tick()
        self.complete("mod_analysis", status="failed", error_code="relevant_skill_gap", outputs={
            "research_repairable": True, "unresolved_relevant_gaps": [{"skill": "platform", "index": 0}]})
        self.tick()
        self.assertIn("gap_research", self.snapshot["application_state"]["early_pending"])
        for stage in ("contract_draft", "contract_verify", "contract_review", "contract_freeze"):
            if stage == "contract_freeze" and reassessing:
                self.complete("gap_research")
                self.tick()
                self.complete("research_review", outputs={"verdict": "approved"})
                self.tick()
            self.complete(stage, outputs={"verdict": "approved"})
            self.tick()

    def test_harness_advances_while_analysis_research_is_pending(self):
        self.start_background_research()
        self.assertIn("migration_inventory", self.snapshot["tasks"])
        self.assertEqual(self.snapshot["application_state"]["gap_pending"], ["gap_research"])
        self.assertEqual(self.snapshot["application_state"]["rework_context"], None)
        self.assertEqual(self.snapshot["application_state"]["gap_rework"]["stage"], "mod_analysis")

    def test_planning_consumes_initial_analysis_without_waiting_for_new_attempt(self):
        self.start_background_research(reassessing=True)
        self.assertEqual(self.snapshot["application_state"]["gap_pending"], ["mod_analysis"])
        self.assertEqual(self.snapshot["tasks"]["migration_inventory"]["attempts"][-1]["state"], "running")

    def start_local_coders(self):
        self.start_background_research()
        for stage in ("migration_inventory", "migration_plan", "migration_tasks", "parallel_review"):
            self.complete(stage, outputs={"parallel_decision": "sequential"})
            self.tick()
        self.complete("implementation", outputs={"development_base": "a" * 40, "development_tasks": [
            {"id": name, "dependencies": [], "blocked_by_gaps": ["platform:0"] if name == "a" else [],
             "model": "gpt-5.6-luna", "reasoning_effort": "max"} for name in ("a", "b")]})
        self.tick()

        self.complete_goal('a')
        self.complete_goal('b')

    def test_invalid_reassessment_cannot_release_affected_coder(self):
        self.start_local_coders()
        self.assertIn("coder.g1.b", self.snapshot["tasks"])
        self.assertNotIn("coder.g1.a", self.snapshot["tasks"])
        self.complete("gap_research")
        self.tick()
        self.complete("research_review", outputs={"verdict": "approved"})
        self.tick()
        self.complete("mod_analysis", status="failed", error_code="analysis_output_invalid",
                      outputs={"format_repairable": True})
        self.tick()
        self.assertNotIn("coder.g1.a", self.snapshot["tasks"])
        self.assertEqual(self.operations._knowledge_gaps(self.snapshot["application_state"])[0]["gap_id"], "platform:0")
        self.complete("mod_analysis")
        self.tick()
        self.assertNotIn("coder.g1.a", self.snapshot["tasks"])
        self.assertEqual(self.operations._knowledge_gaps(self.snapshot["application_state"])[0]["gap_id"], "platform:0")
        self.assertEqual(self.snapshot["application_state"]["research_budget"]["platform"]["dispatched"], 1)
        self.assertEqual(len(self.snapshot["tasks"]["gap_research"]["attempts"]), 1)

    def test_coder_failure_cancels_research_before_finish(self):
        self.start_local_coders()
        self.header["request"]["budget"]["max_rework_rounds"] = 0
        self.complete("coder.g1.b", status="failed", error_code="fixture_failure")
        operations = self.tick()
        self.assertNotIn("finish", [op["kind"] for op in operations])
        self.assertEqual(self.snapshot["tasks"]["gap_research"]["attempts"][-1]["state"], "cancelled")
        self.tick()
        self.assertEqual(self.snapshot["state"], "failed")
        self.assertEqual(self.snapshot["application_state"]["terminal_reason"], "migration_plan_rounds_exhausted")

    def test_foreground_failure_cancels_background_research_before_finish(self):
        self.start_background_research()
        self.complete("migration_inventory", status="blocked", error_code="planning_artifact_invalid")
        operations = self.tick()
        self.assertNotIn("finish", [op["kind"] for op in operations])
        self.assertEqual(self.snapshot["tasks"]["gap_research"]["attempts"][-1]["state"], "cancelled")
        self.tick()
        self.assertEqual(self.snapshot["state"], "failed")
        self.assertEqual(self.snapshot["application_state"]["terminal_reason"], "planning_artifact_invalid")

    def test_foreground_budget_exhaustion_cancels_research_before_finish(self):
        self.start_background_research()
        self.header["request"]["budget"]["max_agent_assignments"] = self.snapshot["application_state"]["agent_assignments"]
        self.complete("migration_inventory")
        operations = self.tick()
        self.assertNotIn("finish", [op["kind"] for op in operations])
        self.tick()
        self.assertEqual(self.snapshot["application_state"]["terminal_reason"], "agent_assignment_budget_exhausted")

    def test_fresh_pair_spends_initial_and_one_targeted_assignment_and_keeps_budget_on_restart(self):
        self.tick()
        for stage in ("source", "background", "preparation", "environment", "project_init", "baseline_build"):
            self.complete(stage)
            self.tick()
        self.complete("skill_lookup", outputs={"missing_kinds": ["platform"]})
        self.tick()
        for stage in ("platform_diff", "platform_skill_review", "skill_publish", "mod_scan"):
            self.complete(stage, outputs={"verdict": "approved"})
            self.tick()
        self.complete("mod_analysis", status="failed", error_code="relevant_skill_gap", outputs={
            "research_repairable": True, "unresolved_relevant_gaps": [{"skill": "platform", "index": 0}]})
        self.tick()
        self.complete("gap_research", outputs={"gap_findings": [{"gap_id": "platform:0", "status": "evidence_added"}]})
        self.tick()
        self.complete("research_review", outputs={"verdict": "approved"})
        self.tick()
        self.complete("mod_analysis", status="failed", error_code="relevant_skill_gap", outputs={
            "research_repairable": True, "unresolved_relevant_gaps": [{"skill": "platform", "index": 0}]})
        self.tick()
        app = self.snapshot["application_state"]
        self.assertEqual(app["research_budget"]["platform"], {"origin": "new", "limit": 2, "dispatched": 2,
                         "executions": ["early:platform_diff:1", "early:gap_research:1"]})
        self.assertEqual(set(app["research_attempts"]), {"early:platform_diff:1", "early:gap_research:1"})
        self.assertEqual(len(self.snapshot["tasks"]["gap_research"]["attempts"]), 1)
        self.assertNotIn("gap_research", app["rounds"])
        before = json_copy(app["research_budget"])
        self.operations = MigrationOperations(memory_probe=lambda: MemorySnapshot(64 * 1024**3, 64 * 1024**3, "fixture"))
        self.tick()
        self.assertEqual(self.snapshot["application_state"]["research_budget"], before)
        self.assertEqual(len(self.snapshot["tasks"]["gap_research"]["attempts"]), 1)

    def test_independent_resolution_releases_affected_coder_after_reassessment(self):
        self.start_local_coders()
        self.complete("gap_research", outputs={"gap_findings": [{"gap_id": "platform:0", "status": "evidence_added"}]})
        self.tick()
        self.assertNotIn("coder.g1.a", self.snapshot["tasks"])
        self.complete("research_review", outputs={"verdict": "approved", "approved_gap_resolutions": [
            {"gap_id": "platform:0", "project_status": "resolved"}]})
        self.tick()
        self.complete("mod_analysis")
        self.tick()
        self.assertIn("coder.g1.a", self.snapshot["tasks"])
        self.assertEqual(self.operations._knowledge_gaps(self.snapshot["application_state"]), [])
