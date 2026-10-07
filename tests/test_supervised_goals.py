import json
from pathlib import Path
import tempfile
import unittest

from modport.contracts import OperationInput, OperationResult
from modport.evidence import seal_ref, verified_path
from modport.supervised_goals import apply_to_goal, collect, prepare, target_key


class SupervisedGoalTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        (self.root / "worktree" / "src").mkdir(parents=True)
        (self.root / "worktree" / "src" / "Example.java").write_text(
            "class Example {}\n", encoding="utf-8")
        (self.root / "worktree" / "build").mkdir()
        (self.root / "worktree" / "build" / "generated.java").write_text(
            "generated", encoding="utf-8")
        (self.root / "worktree" / ".modport" / "harness").mkdir(parents=True)
        (self.root / "worktree" / ".modport" / "harness" / "AGENT_RULES.md").write_text(
            "Harness guidance", encoding="utf-8")
        (self.root / "worktree" / ".modport" / "run-client").mkdir()
        (self.root / "worktree" / ".modport" / "run-client" / "client.log").write_text(
            "generated runtime log", encoding="utf-8")
        self.plan_ref = self._json_ref("artifacts/plan.json", {"plan": "frozen"})
        self.task = {"id": "task-1", "objective": "Implement the task objective.",
                     "acceptance": ["Keep the behavior"]}
        self.goal_ref = self._json_ref("artifacts/source-goal.json", {
            "task_id": "task-1", "objective": "Prior detailed coder context."})
        self.target = {
            "key": target_key(self.task, self.plan_ref),
            "task": self.task,
            "plan_ref": self.plan_ref,
            "source_execution_id": "coder-source-1",
            "source_workspace": "worktree",
            "goal_ref": self.goal_ref,
        }
        self.command = OperationInput(
            run_id="run-1", task_id="supervisor-window-5", stage_id="supervisor",
            command_id="supervisor-5", run_dir=str(self.root),
            options={"workflow_version": 26},
            payload={"supervised_goal_targets": [self.target]},
        )

    def test_prepare_collect_apply_and_preserve_context(self):
        prepared = prepare(self.command)
        self.assertEqual(prepared.options["workspace"].split("/")[:2],
                         ["workspaces", "supervisor"])
        self.assertIn("supervised_goal_manifest", prepared.artifact_refs)
        source_manifest = self._manifest(prepared)["source_snapshot"]["roots"][0]
        source_paths = {item["path"] for item in source_manifest["files"]}
        skipped_paths = {item["path"] for item in source_manifest["skipped"]}
        self.assertIn("src/Example.java", source_paths)
        self.assertIn(".modport/harness/AGENT_RULES.md", source_paths)
        self.assertNotIn("build/generated.java", source_paths)
        self.assertIn(".modport/run-client", skipped_paths)

        document = self.root / prepared.options["workspace"] / "goals" / (self.target["key"] + ".md")
        self.assertEqual(document.read_text(encoding="utf-8"), "Prior detailed coder context.")
        document.write_text("Use the supervised objective and keep compatibility.\n",
                            encoding="utf-8")
        resumed = prepare(self.command)
        self.assertEqual(document.read_text(encoding="utf-8"),
                         "Use the supervised objective and keep compatibility.\n")

        stage_result = OperationResult(
            status="completed", run_id=prepared.run_id, task_id=prepared.task_id,
            stage_id=prepared.stage_id, command_id=prepared.command_id,
            outputs={"last_message": "supervisor completed"},
        )
        collected = collect(resumed, stage_result)
        self.assertEqual(collected.status, "completed")
        self.assertEqual(collected.outputs["last_message"], "supervisor completed")
        revision_ref = collected.outputs["supervised_goal_revisions"][0]["revision_ref"]
        self.assertEqual(collected.outputs["artifact_refs"]["supervised_goal_manifest"],
                         prepared.artifact_refs["supervised_goal_manifest"])
        self.assertEqual(collected.outputs["artifact_refs"][
            f"supervised_goal_revision:{self.target['key']}"], revision_ref)
        revision = self._read_json(revision_ref)
        self.assertEqual(revision["supervisor_execution_id"], "supervisor-5")
        self.assertEqual(revision["original_objective"], self.task["objective"])
        self.assertEqual(self._read_text(revision["before_ref"]), "Prior detailed coder context.")
        self.assertEqual(self._read_text(revision["after_ref"]),
                         "Use the supervised objective and keep compatibility.\n")

        # Editing the live document after collection cannot change the published revision.
        document.write_text("later workspace edit", encoding="utf-8")
        self.assertEqual(self._read_text(revision["after_ref"]),
                         "Use the supervised objective and keep compatibility.\n")
        consumer = self._consumer(revision_ref)
        original_goal = {"objective": self.task["objective"], "acceptance": ["Keep the behavior"]}
        applied = apply_to_goal(consumer, original_goal, self.task)
        self.assertEqual(applied["objective"], "Use the supervised objective and keep compatibility.\n")
        self.assertEqual(applied["acceptance"], original_goal["acceptance"])
        receipt = self._read_json(applied["supervised_goal_revision"]["receipt_ref"])
        self.assertEqual(receipt["consumer_execution_id"], "coder-1")
        self.assertEqual(receipt["before_ref"], revision["before_ref"])

        context_goal = dict(applied)
        context_goal["objective"] += "\n\nCoder context:\nPrepared context"
        preserved = apply_to_goal(consumer, context_goal, self.task)
        self.assertEqual(preserved["objective"], context_goal["objective"])
        self.assertEqual(preserved["supervised_goal_revision"]["receipt_ref"],
                         applied["supervised_goal_revision"]["receipt_ref"])

    def test_next_supervisor_starts_from_latest_revision(self):
        first_ref = self._collect_revision(self.command, "First revision.")
        second_target = {**self.target, "previous_revision_ref": first_ref}
        second_command = OperationInput(
            run_id="run-1", task_id="supervisor-window-10", stage_id="supervisor",
            command_id="supervisor-10", run_dir=str(self.root),
            options={"workflow_version": 26},
            payload={"supervised_goal_targets": [second_target]},
        )
        prepared = prepare(second_command)
        document = self.root / prepared.options["workspace"] / "goals" / (self.target["key"] + ".md")
        self.assertEqual(document.read_text(encoding="utf-8"), "First revision.")
        document.write_text("Second revision.", encoding="utf-8")
        collected = collect(prepared, OperationResult(
            status="completed", run_id=prepared.run_id, task_id=prepared.task_id,
            stage_id=prepared.stage_id, command_id=prepared.command_id))
        revision = self._read_json(collected.outputs["supervised_goal_revisions"][0]["revision_ref"])
        self.assertEqual(revision["previous_revision_ref"], first_ref)
        self.assertEqual(revision["before_ref"], self._read_json(first_ref)["after_ref"])
        self.assertEqual(revision["original_objective"], self.task["objective"])

    def test_later_revision_keeps_prepared_coder_context(self):
        first_ref = self._collect_revision(self.command, "First revision.")
        first_goal = apply_to_goal(self._consumer(first_ref, command_id="goal-prepare-1"),
                                   {"objective": self.task["objective"]}, self.task)
        first_goal["objective"] += "\n\nCoder context:\nAuthenticated preparation facts."
        second_target = {**self.target, "previous_revision_ref": first_ref}
        second_command = OperationInput(
            run_id="run-1", task_id="supervisor-window-10", stage_id="supervisor",
            command_id="supervisor-10", run_dir=str(self.root),
            options={"workflow_version": 26},
            payload={"supervised_goal_targets": [second_target]},
        )
        second_ref = self._collect_revision(second_command, "Second revision.")
        rebased = apply_to_goal(self._consumer(second_ref), first_goal, self.task)
        self.assertEqual(rebased["objective"],
                         "Second revision.\n\nCoder context:\nAuthenticated preparation facts.")
        self.assertEqual(rebased["supervised_goal_revision"]["revision_ref"], second_ref)

    def test_first_revision_keeps_unsupervised_prepared_context(self):
        revision_ref = self._collect_revision(self.command, "First revision.")
        prepared = {"objective": self.task["objective"]
                    + "\n\nCoder context:\nAuthenticated preparation facts."}
        applied = apply_to_goal(self._consumer(revision_ref), prepared, self.task)
        self.assertEqual(applied["objective"],
                         "First revision.\n\nCoder context:\nAuthenticated preparation facts.")

    def test_unrecognized_prepared_objective_cannot_silently_lose_context(self):
        revision_ref = self._collect_revision(self.command, "Revised objective.")
        with self.assertRaisesRegex(ValueError, "authenticated base"):
            apply_to_goal(self._consumer(revision_ref),
                          {"objective": "Unexpected preparation.\n\nCoder context:\nNeeded facts."},
                          self.task)

    def test_rebase_rejects_unbound_previous_revision(self):
        first_ref = self._collect_revision(self.command, "First revision.")
        prepared = apply_to_goal(self._consumer(first_ref, command_id="goal-prepare-1"),
                                 {"objective": self.task["objective"]}, self.task)
        prepared["objective"] += "\n\nCoder context:\nNeeded facts."
        second_target = {**self.target, "previous_revision_ref": first_ref}
        second_command = OperationInput(
            run_id="run-1", task_id="supervisor-window-10", stage_id="supervisor",
            command_id="supervisor-10", run_dir=str(self.root),
            options={"workflow_version": 26},
            payload={"supervised_goal_targets": [second_target]},
        )
        second_ref = self._collect_revision(second_command, "Second revision.")
        prepared["supervised_goal_revision"]["after_ref"] = self._read_json(second_ref)["after_ref"]
        with self.assertRaisesRegex(ValueError, "previous revision reference mismatch"):
            apply_to_goal(self._consumer(second_ref), prepared, self.task)

    def test_apply_rejects_task_or_plan_mismatch(self):
        revision_ref = self._collect_revision(self.command, "Revised objective.")
        consumer = self._consumer(revision_ref)
        with self.assertRaisesRegex(ValueError, "identity mismatch"):
            apply_to_goal(consumer, {"objective": "anything"},
                          {**self.task, "objective": "a different original objective"})
        changed_plan = {"path": "artifacts/other-plan.json", "sha256": "0" * 64,
                        "media_type": "application/json"}
        changed_consumer = OperationInput(
            run_id="run-1", task_id="coder.task-1", stage_id="coder", command_id="coder-1",
            run_dir=str(self.root), options={"workflow_version": 26},
            artifact_refs={"development_plan": changed_plan,
                           "supervised_goal_revision": revision_ref})
        with self.assertRaisesRegex(ValueError, "identity mismatch"):
            apply_to_goal(changed_consumer, {"objective": self.task["objective"]}, self.task)

    def test_collect_rejects_symlink_and_empty_documents(self):
        prepared = prepare(self.command)
        document = self.root / prepared.options["workspace"] / "goals" / (self.target["key"] + ".md")
        document.unlink()
        document.symlink_to(self.root / "worktree" / "src" / "Example.java")
        result = self._stage_result(prepared)
        collected = collect(prepared, result)
        self.assertEqual(collected.status, "failed")
        self.assertEqual(collected.outputs["supervised_goal_revisions"], [])
        self.assertIn("symlink", collected.outputs["supervised_goal_diagnostics"][0]["detail"])

        document.unlink()
        document.write_bytes(b"")
        collected = collect(prepared, result)
        self.assertEqual(collected.status, "failed")
        self.assertIn("empty", collected.outputs["supervised_goal_diagnostics"][0]["detail"])
        self.assertIn("supervised_goal_manifest", collected.outputs["artifact_refs"])

    def test_failed_supervisor_keeps_partial_revision_as_non_applicable_evidence(self):
        prepared = prepare(self.command)
        document = self.root / prepared.options["workspace"] / "goals" / (self.target["key"] + ".md")
        document.write_text("Partial supervised edit.", encoding="utf-8")
        failed_result = OperationResult(
            status="failed", run_id=prepared.run_id, task_id=prepared.task_id,
            stage_id=prepared.stage_id, command_id=prepared.command_id,
            outputs={"last_message": "raw failure report"}, detail="agent failed",
            error_code="agent_failed")
        collected = collect(prepared, failed_result)
        revision_ref = collected.outputs["supervised_goal_revisions"][0]["revision_ref"]
        revision = self._read_json(revision_ref)
        self.assertEqual(collected.status, "failed")
        self.assertFalse(revision["applicable"])
        self.assertEqual(collected.outputs["artifact_refs"]["supervised_goal_manifest"],
                         prepared.artifact_refs["supervised_goal_manifest"])
        self.assertEqual(collected.outputs["artifact_refs"][
            f"supervised_goal_revision:{self.target['key']}"], revision_ref)
        with self.assertRaisesRegex(ValueError, "identity mismatch"):
            apply_to_goal(self._consumer(revision_ref),
                          {"objective": self.task["objective"]}, self.task)

    def test_revision_is_v26_only_when_present(self):
        revision_ref = self._collect_revision(self.command, "Revised objective.")
        old_no_revision = OperationInput(
            run_id="run-1", task_id="coder.task-1", stage_id="coder", command_id="old-coder",
            run_dir=str(self.root), options={"workflow_version": 25})
        goal = {"objective": "unchanged", "acceptance": ["fixed"]}
        self.assertEqual(apply_to_goal(old_no_revision, goal, self.task), goal)
        old_with_revision = OperationInput(
            run_id="run-1", task_id="coder.task-1", stage_id="coder", command_id="old-coder-2",
            run_dir=str(self.root), options={"workflow_version": 25},
            artifact_refs={"supervised_goal_revision": revision_ref})
        with self.assertRaisesRegex(ValueError, "workflow v26"):
            apply_to_goal(old_with_revision, goal, self.task)

    def _collect_revision(self, command, edited_text):
        prepared = prepare(command)
        path = self.root / prepared.options["workspace"] / "goals" / (self.target["key"] + ".md")
        path.write_text(edited_text, encoding="utf-8")
        collected = collect(prepared, self._stage_result(prepared))
        self.assertEqual(collected.status, "completed")
        return collected.outputs["supervised_goal_revisions"][0]["revision_ref"]

    @staticmethod
    def _stage_result(command):
        return OperationResult(status="completed", run_id=command.run_id,
                               task_id=command.task_id, stage_id=command.stage_id,
                               command_id=command.command_id,
                               outputs={"diagnostic": "kept"})

    def _consumer(self, revision_ref, *, command_id="coder-1"):
        return OperationInput(
            run_id="run-1", task_id="coder.task-1", stage_id="coder",
            command_id=command_id, run_dir=str(self.root),
            options={"workflow_version": 26},
            artifact_refs={"development_plan": self.plan_ref,
                           "supervised_goal_revision": revision_ref},
        )

    def _json_ref(self, relative, value):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value, sort_keys=True), encoding="utf-8")
        return seal_ref(self.root, {"path": relative, "sha256": "ignored",
                                    "media_type": "application/json"},
                        execution_id="test-source")

    def _manifest(self, command):
        return self._read_json(command.payload["supervised_goal_manifest_ref"])

    def _read_json(self, ref):
        return json.loads(verified_path(self.root, ref).read_text(encoding="utf-8"))

    def _read_text(self, ref):
        return verified_path(self.root, ref).read_text(encoding="utf-8")


if __name__ == "__main__":
    unittest.main()
