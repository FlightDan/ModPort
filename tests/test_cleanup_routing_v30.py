"""Focused routing and handoff checks for the current v30 cleanup stages."""

import hashlib
import json
from pathlib import Path
import tempfile
import time
import unittest

from modport.contracts import OperationInput, OperationResult
from modport.evidence import file_digest
from modport.handlers import build_registry
from modport.memory_admission import MIB, MemoryPolicy, MemorySnapshot
from modport.models import Budget, MigrationRequest
from modport.operations import MigrationOperations
from modport.payload_storage import unpack_input
from modport.rework_tools import is_interactive_review, prepare_session, rework_targets
from modport.workflow import (AGENT_STAGES, WORKFLOW_VERSION, WorkflowDefinition,
                              agent_model_policy, compile_migration_workflow,
                              stage_routes)


class CleanupRoutingV30Tests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "run"
        self.root.mkdir()
        self.run_id = "cleanup-v30"
        self.request = MigrationRequest(
            "example", "https://example.invalid/source.git", "1.20.1", "26.1.2",
            source_revision="a" * 40,
            budget=Budget(max_agent_assignments=100, max_rework_rounds=10),
            max_parallel_coders=2,
        )
        self.definition = compile_migration_workflow(self.request).to_dict()
        self.header = {
            "request": self.request.to_dict(),
            "definition": self.definition,
            "deadline_epoch": None,
            "run_dir": str(self.root),
            "initial_refs": {},
            "prior_findings": [],
            "registry_revision": "a" * 64,
            "rubric_sha256": "b" * 64,
            "continuation": {},
        }
        self.operations = MigrationOperations(memory_probe=lambda: MemorySnapshot(
            64 * 1024**3, 64 * 1024**3, "fixture"))
        self.app = self.operations._new_application()
        self.snapshot = {
            "run_id": self.run_id,
            "state": "running",
            "tasks": {},
            "waits": {},
            "application_state": self.app,
        }

    def _operation(self, stage, *, task_id=None, command_id=None, options=None,
                   payload=None, upstream_results=None, artifact_refs=None):
        task_id = task_id or stage
        return OperationInput(
            self.run_id, task_id, stage, command_id or f"{self.run_id}:{task_id}:1",
            str(self.root), options={"workflow_version": WORKFLOW_VERSION, **(options or {})},
            payload=payload or {}, upstream_results=upstream_results or {},
            artifact_refs=artifact_refs or {},
        )

    @staticmethod
    def _result(command, *, status="completed", outputs=None, error_code=None):
        return OperationResult(
            status, command.run_id, command.task_id, command.stage_id, command.command_id,
            outputs or {}, error_code=error_code,
        )

    def _store_result(self, command, outcome, *, state=None):
        self.app["effective"][command.stage_id] = outcome.to_dict()
        self.snapshot["tasks"].setdefault(command.task_id, {"attempts": []})[
            "attempts"].append({
                "state": state or ("succeeded" if outcome.status == "completed" else "failed"),
                "command": {"execution_id": command.command_id, "payload": command.to_dict()},
                "result": {"value": outcome.to_dict()},
            })

    def _operation_from_change(self, change):
        logical = unpack_input(self.root, change["command"]["payload"])
        return OperationInput.from_dict(logical)

    def _scheduled(self, changes):
        return [self._operation_from_change(change) for change in changes
                if change.get("kind") in {"add_task", "new_attempt"}]

    def _artifact(self, relative, contents, media_type="text/markdown"):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(contents, encoding="utf-8")
        return {"path": relative, "sha256": file_digest(path), "media_type": media_type}

    def test_current_workflow_edges_keep_v29_definition_byte_identical(self):
        self.assertEqual(30, WORKFLOW_VERSION)
        current = compile_migration_workflow(self.request).to_dict()
        main, early, successors, dependencies = stage_routes(current)
        self.assertEqual(("source",), dependencies["research_cleanup"])
        self.assertEqual(("early_compile", "contract_freeze", "research_cleanup"),
                         dependencies["migration_inventory"])
        self.assertLess(early.index("source"), early.index("research_cleanup"))
        self.assertLess(early.index("research_cleanup"), early.index("preparation"))
        self.assertEqual(("development_integrate",), dependencies["code_cleanup"])
        self.assertLess(main.index("development_integrate"), main.index("code_cleanup"))
        self.assertLess(main.index("code_cleanup"), main.index("target_build"))
        for stage in ("development_integrate", "target_repair_integrate", "target_revise"):
            self.assertEqual("code_cleanup", successors[stage])
        self.assertEqual("target_build", successors["code_cleanup"])
        self.assertIn("research_cleanup", AGENT_STAGES)
        self.assertIn("code_cleanup", AGENT_STAGES)
        registry = build_registry()
        self.assertIn("modport.research_cleanup", registry)
        self.assertIn("modport.code_cleanup", registry)

        # These hashes were captured from the pre-change v29 source before
        # adding any v30 route. They cover the complete canonical definition.
        expected = {
            "full": "c423dadac1357f087a622900aa40b69f6f467c9a26e6a4668397608b6b171ff6",
            "compile_package": "8450d2732460e8456b3081725ad201f02cee3b01bcb72fadce8e500d5c77caed",
        }
        for scope, options in (("full", {}),
                               ("compile_package", {"validation_scope": "compile_package"})):
            request = MigrationRequest(
                "example", "https://example.invalid/source.git", "1.20.1", "26.1.2",
                **options,
            )
            serialized = json.dumps(
                WorkflowDefinition(request.to_dict(), version=29).to_dict(),
                sort_keys=True, separators=(",", ":"), ensure_ascii=False,
            ).encode("utf-8")
            self.assertEqual(expected[scope], hashlib.sha256(serialized).hexdigest())

        skill_request = MigrationRequest(
            "example", "https://example.invalid/source.git", "1.20.1", "26.1.2",
            workflow_mode="skill_generation", skill_kind="java",
        )
        old_mode_serialized = json.dumps(
            WorkflowDefinition(skill_request.to_dict(), version=29).to_dict(),
            sort_keys=True, separators=(",", ":"), ensure_ascii=False,
        ).encode("utf-8")
        self.assertEqual(
            "ce642d67d6af7df1ffcc220d8728984d05c6dfc231471540030494ded4f97a73",
            hashlib.sha256(old_mode_serialized).hexdigest(),
        )
        skill_generation = WorkflowDefinition(skill_request.to_dict(), version=30).to_dict()
        skill_stage_ids = {row["stage_id"] for row in skill_generation["stages"]}
        self.assertNotIn("research_cleanup", skill_stage_ids)
        self.assertNotIn("code_cleanup", skill_stage_ids)
        skill_dependencies = {row["stage_id"]: row["depends_on"]
                              for row in skill_generation["stages"]}
        self.assertNotIn("research_cleanup", skill_dependencies["migration_inventory"])

    def test_cleanup_starts_alongside_preparation_and_failure_joins_inventory(self):
        # Once source and codemod prerequisites are observed, cleanup and
        # preparation are independently dispatchable in the same early tick.
        self.app["early_active"] = True
        for stage in ("source", "skill_resolve", "mod_scan", "codemod", "background"):
            command = self._operation(stage)
            self.app["effective"][stage] = self._result(command).to_dict()
        changes = self.operations._flowthrough_early_decision(
            self.snapshot, self.header, self.app)
        stages = {row.stage_id for row in self._scheduled(changes)}
        self.assertIn("research_cleanup", stages)
        self.assertIn("preparation", stages)

        # A terminal cleanup failure is still an observed outcome. It reaches
        # the authenticated inventory input without automatic rework or a gate.
        self.app = self.operations._new_application()
        self.snapshot = {**self.snapshot, "tasks": {}, "application_state": self.app}
        self.app["early_active"] = True
        for stage in self.definition["early_stages"]:
            if stage == "research_cleanup":
                continue
            command = self._operation(stage)
            self.app["effective"][stage] = self._result(command).to_dict()
        cleanup = self._operation("research_cleanup")
        failed = self._result(cleanup, status="failed", error_code="cleanup_report_missing")
        self._store_result(cleanup, failed)
        changes = self.operations._flowthrough_early_decision(
            self.snapshot, self.header, self.app)
        scheduled = self._scheduled(changes)
        self.assertEqual(["migration_inventory"], [row.stage_id for row in scheduled])
        self.assertEqual("failed", self.app["effective"]["research_cleanup"]["status"])
        self.assertEqual(1, len(self.snapshot["tasks"]["research_cleanup"]["attempts"]))
        self.assertEqual({}, self.app["rounds"])

    def test_research_cleanup_report_artifact_reaches_migration_inventory(self):
        ref = self._artifact("artifacts/research-cleanup/index.md", "# Source navigation\n")
        cleanup = self._operation("research_cleanup")
        self._store_result(cleanup, self._result(cleanup, outputs={
            "source_commit": "a" * 40,
            "artifact_refs": {"research_cleanup": ref},
        }))
        changes = self.operations._schedule(
            self.snapshot, self.header, self.app, "migration_inventory", dependencies=[])
        inventory, = self._scheduled(changes)
        self.assertEqual(ref, inventory.artifact_refs["research_cleanup"])
        self.assertEqual(ref, inventory.upstream_results["research_cleanup"][
            "outputs"]["artifact_refs"]["research_cleanup"])

    def test_merged_cleanup_and_repair_successors_reach_target_verification(self):
        routes = (
            ("development_integrate", "code_cleanup"),
            ("target_repair_integrate", "code_cleanup"),
            ("target_revise", "code_cleanup"),
            ("code_cleanup", "target_build"),
        )
        for predecessor, expected in routes:
            with self.subTest(predecessor=predecessor):
                app = self.operations._new_application()
                snapshot = {**self.snapshot, "tasks": {}, "application_state": app}
                changes = self.operations._flowthrough_schedule_successor(
                    snapshot, self.header, app, predecessor, "cause:" + predecessor)
                self.assertEqual([expected], [row.stage_id for row in self._scheduled(changes)])

        # Cleanup failure remains evidence and advances to a fresh verification
        # attempt without retrying cleanup or inserting an approval stage.
        app = self.operations._new_application()
        snapshot = {**self.snapshot, "tasks": {}, "application_state": app}
        cleanup = self._operation("code_cleanup")
        failed = self._result(cleanup, status="failed", error_code="cleanup_integrity")
        self.operations._flowthrough_record(app, cleanup, failed)
        changes = self.operations._flowthrough_schedule_successor(
            snapshot, self.header, app, "code_cleanup", cleanup.command_id)
        self.assertEqual(["target_build"], [row.stage_id for row in self._scheduled(changes)])
        self.assertEqual("failed", app["effective"]["code_cleanup"]["status"])
        self.assertEqual({}, app["rounds"])

    def test_frozen_v29_successor_skips_v30_cleanup_stage(self):
        # The immediately preceding workflow remains frozen: its actual
        # flowthrough scheduler must follow the serialized v29 successor map.
        legacy_header = dict(self.header)
        legacy_header["definition"] = WorkflowDefinition(
            self.request.to_dict(), version=29).to_dict()
        app = self.operations._new_application()
        snapshot = {**self.snapshot, "tasks": {}, "application_state": app}
        changes = self.operations._flowthrough_schedule_successor(
            snapshot, legacy_header, app, "development_integrate", "v29:integrate")
        self.assertEqual(["target_build"],
                         [row.stage_id for row in self._scheduled(changes)])

    def test_reviewer_rework_of_code_cleanup_runs_fresh_target_build(self):
        report = self._artifact("artifacts/cleanup/original-report.md", "# Cleanup report\n")
        patch = self._artifact("artifacts/cleanup/original.patch", "diff --git a/A.java b/A.java\n",
                               "application/octet-stream")
        candidate = self._artifact("artifacts/cleanup/original-candidate.json", "{}",
                                   "application/json")
        last_message = self._artifact("artifacts/cleanup/last-message.md", "Simplified two helpers.\n")
        cleanup = self._operation("code_cleanup")
        cleanup_result = self._result(cleanup, outputs={
            "artifact_refs": {
                "code_cleanup_report": report,
                "code_cleanup_patch": patch,
                "code_cleanup_candidate": candidate,
                "agent_last_message": last_message,
            },
            "last_message": last_message["path"],
            "candidate_before": "a" * 40,
            "candidate_after": "b" * 40,
            "changed_paths": ["src/Example.java"],
        })
        self._store_result(cleanup, cleanup_result)

        review_changes = self.operations._schedule(
            self.snapshot, self.header, self.app, "code_review", dependencies=[])
        review, = self._scheduled(review_changes)
        targets = review.payload["review_rework_targets"]
        self.assertEqual("code_cleanup", targets[0]["stage"])
        self.assertEqual("code_cleanup", targets[0]["target_agent"])
        self.assertEqual(report, review.artifact_refs["code_cleanup_report"])

        self.snapshot["tasks"][review.task_id] = {"attempts": [{
            "state": "running",
            "command": {"execution_id": review.command_id, "payload": review.to_dict()},
        }]}
        session = self.root / "artifacts" / "rework-tools" / review.command_id
        (session / "requests").mkdir(parents=True)
        (session / "session.json").write_text(json.dumps({
            "run_id": self.run_id,
            "reviewer_execution_id": review.command_id,
            "reviewer_task_id": review.task_id,
            "reviewer_stage": review.stage_id,
            "workflow_version": WORKFLOW_VERSION,
            "workspace": "workspaces/worktree",
            "deadline_epoch": time.time() + 3600,
            "drain_pending_on_eof": False,
            "targets": targets,
        }), encoding="utf-8")
        (session / "requests" / "cleanup-001.json").write_text(json.dumps({
            "request_id": "cleanup-001",
            "run_id": self.run_id,
            "reviewer_execution_id": review.command_id,
            "target_agent": "code_cleanup",
            "instructions": "Address the reviewer finding while preserving the existing assertions.",
        }), encoding="utf-8")

        changes = self.operations._review_rework_decision(
            self.snapshot, self.header, self.app)
        rework_change = next(row for row in changes if row["kind"] == "add_task")
        rework = self._operation_from_change(rework_change)
        self.assertEqual("code_cleanup", rework.stage_id)
        self.assertEqual("Address the reviewer finding while preserving the existing assertions.",
                         rework.payload["reviewer_rework"]["instructions"])
        self.assertIn(".modport/code-review.json", rework.payload["reviewer_report_paths"])
        self.assertEqual(agent_model_policy(WORKFLOW_VERSION, "code_cleanup"),
                         (rework.options["model"], rework.options["reasoning_effort"]))

        revised_report = self._artifact(
            "artifacts/cleanup/reworked-report.md", "# Revised cleanup report\n")
        revised_patch = self._artifact(
            "artifacts/cleanup/reworked.patch", "diff --git a/A.java b/A.java\n",
            "application/octet-stream")
        revised_candidate = self._artifact(
            "artifacts/cleanup/reworked-candidate.json", "{}", "application/json")
        revised_message = self._artifact(
            "artifacts/cleanup/reworked-last-message.md", "Revised cleanup complete.\n")
        revised_result = self._result(rework, outputs={
            "artifact_refs": {
                "code_cleanup_report": revised_report,
                "code_cleanup_patch": revised_patch,
                "code_cleanup_candidate": revised_candidate,
                "agent_last_message": revised_message,
            },
            "last_message": revised_message["path"],
            "candidate_before": "b" * 40,
            "candidate_after": "c" * 40,
            "changed_paths": ["src/Example.java"],
        })
        self.snapshot["tasks"][rework.task_id] = {"attempts": [{
            "state": "succeeded",
            "command": {"execution_id": rework.command_id, "payload": rework.to_dict()},
            "result": {"value": revised_result.to_dict()},
        }]}
        followups = self.operations._review_rework_decision(
            self.snapshot, self.header, self.app)
        verification, = [row for row in self._scheduled(followups)
                         if row.stage_id == "target_build"]
        self.assertEqual([".modport/code-review.json"],
                         verification.payload["reviewer_report_paths"])
        self.assertEqual(revised_report, verification.artifact_refs["code_cleanup_report"])
        self.assertEqual(rework.command_id,
                         verification.upstream_results["code_cleanup"]["command_id"])
        self.assertEqual("target_build",
                         self.app["review_rework"]["requests"][review.command_id + "/cleanup-001"][
                             "followup_stage"])

    def test_cleanup_workers_are_noninteractive_and_keep_their_stage_locks(self):
        code = self._operation("code_cleanup", payload={
            "review_rework_targets": [{"target_agent": "code_cleanup"}],
        })
        self.assertFalse(is_interactive_review(code))
        self.assertEqual([], rework_targets(self.snapshot, code))
        self.assertIsNone(prepare_session(code, self.root, 60))
        from modport.kernel_runtime import operation_lock
        self.assertEqual(self.root / ".locks" / "scopes" / "worktree",
                         operation_lock(self.root, code))

        research = self._operation("research_cleanup")
        self.assertFalse(is_interactive_review(research))
        self.assertEqual(self.root / ".locks" / "scopes" / ".modport" / "research-cleanup",
                         operation_lock(self.root, research))
        self.assertEqual(2048 * MIB, MemoryPolicy().for_stage("code_cleanup").heavy_slot_bytes)
        self.assertEqual(512 * MIB, MemoryPolicy().for_stage("research_cleanup").heavy_slot_bytes)

    def test_target_repair_invalidates_cleanup_and_recovery_projections_include_it(self):
        self.app["effective"].update(code_cleanup={"stage_id": "code_cleanup"},
                                      target_build={"stage_id": "target_build"})
        self.operations._invalidate_from(self.app, "target_build")
        self.assertNotIn("code_cleanup", self.app["effective"])
        self.assertNotIn("target_build", self.app["effective"])

        from modport.repair_reset import contract_repair_tail
        from modport.verification_recovery import _DOWNSTREAM
        self.assertIn("code_cleanup", contract_repair_tail())
        self.assertIn("code_cleanup", _DOWNSTREAM)

    def test_only_exact_v29_definition_can_upgrade_to_v30(self):
        from modport.workflow_upgrade import validate_upgrade_definition

        previous = WorkflowDefinition(self.request.to_dict(), version=29).to_dict()
        upgraded = validate_upgrade_definition({
            "request": self.request.to_dict(), "definition": previous,
        })
        self.assertEqual(WORKFLOW_VERSION, upgraded["workflow_version"])
        self.assertIn("code_cleanup", upgraded["main_stages"])

        retired = WorkflowDefinition(self.request.to_dict(), version=28).to_dict()
        with self.assertRaisesRegex(ValueError, "exact supported upgrade source"):
            validate_upgrade_definition({
                "request": self.request.to_dict(), "definition": retired,
            })


if __name__ == "__main__":
    unittest.main()
