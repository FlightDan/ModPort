"""Focused tests for review-triggered coder continuation assignments."""
from dataclasses import replace
from hashlib import sha256
import json
from pathlib import Path
import subprocess
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from modport.contracts import OperationInput, OperationResult
from modport.development import _artifact, validate_plan
from modport.handlers import _remaining_timeout, _result
from modport.rework_coder import run_coder_rework
from modport.supervised_goals import collect, prepare, target_key
from modport.workflow import WORKFLOW_VERSION
from modport.execution_budget import execution_budget


def git(root, *args):
    return subprocess.check_output(
        ["git", "-c", "user.name=Test", "-c", "user.email=test@example.invalid", *args],
        cwd=root,
        stderr=subprocess.DEVNULL,
    ).decode().strip()


class CoderReworkTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.worktree = self.root / "worktree"
        self.worktree.mkdir()
        git(self.worktree, "init")
        (self.worktree / "a.txt").write_text("original a\n")
        (self.worktree / "b.txt").write_text("original b\n")
        git(self.worktree, "add", ".")
        git(self.worktree, "commit", "-m", "initial")

        # Simulate work that was integrated after the selected coder ran.  A
        # rework delta must retain this commit instead of replaying the old one.
        (self.worktree / "b.txt").write_text("already integrated b\n")
        git(self.worktree, "add", "b.txt")
        git(self.worktree, "commit", "-m", "integrated peer")
        self.integrated_base = git(self.worktree, "rev-parse", "HEAD")

        task = {
            "id": "author-a",
            "objective": "Correct A without changing peer work",
            "dependencies": ["historical-dependency"],
            "owned_paths": ["a.txt"],
            "acceptance": ["A contains the corrected value"],
            "complexity": "simple",
            "validation_kind": "structural",
            "structural_reason": "The fixture checks exact file content",
            "validation_checks": [{
                "id": "a-exists",
                "type": "file_exists",
                "path": "a.txt",
                "acceptance": ["A contains the corrected value"],
            }],
        }
        context = {}
        seed = OperationInput("run", "author-a", "coder", "coder-original", str(self.root))
        for index in range(4):
            context[f"round-{index}"] = _artifact(seed, f"context-{index}.json", b"{}")
        goal = {
            "task_id": task["id"],
            "objective": task["objective"],
            "owned_paths": task["owned_paths"],
            "dependencies": task["dependencies"],
            "acceptance": task["acceptance"],
            "context_refs": context,
            "stop_conditions": ["Preserve the reviewed task boundary"],
            "acceptance_report": ".modport/goal-reports/author-a.json",
            "checks": task["validation_checks"],
            "validation_kind": "structural",
            "structural_reason": task["structural_reason"],
        }
        goal_ref = _artifact(seed, "coder-goal.json", json.dumps(goal).encode())
        self.original = replace(
            seed,
            payload={
                "development_task": task,
                "planning_context": context,
                "goal_scope": "migration",
            },
            options={"workflow_version": 12},
            artifact_refs={"coder_goal": goal_ref},
        )
        self.context = context
        report = self.worktree / ".modport/code-review.json"
        report.parent.mkdir()
        report.write_text("review in progress\n")
        self.report = report
        self.child = OperationInput(
            "run",
            "agent-rework-1",
            "agent_rework",
            "rework-child",
            str(self.root),
            payload={
                "rework_generation": 91,
                "rework_context_refs": context,
                "reviewer_execution_id": "code-review-1",
                "reviewer_report_paths": [".modport/code-review.json"],
                "reviewer_report": "A is still wrong",
                "review_tool_context": {"request_id": "request-1"},
            },
            options={"workflow_version": 12, "agent_assignment": 91},
        )

    def fake_coder(self, changed="a.txt", *, drift=False):
        outer = self

        def invoke(_handler, command):
            outer.assertEqual("agent_rework", command.stage_id)
            outer.assertEqual(outer.integrated_base, command.payload["development_base"])
            outer.assertEqual([], command.payload["development_task"]["dependencies"])
            outer.assertFalse(command.options["native_goal_resume"])
            outer.assertEqual("workspaces/development/g91/author-a", command.options["workspace"])
            workspace = outer.root / command.options["workspace"]
            workspace.parent.mkdir(parents=True, exist_ok=True)
            git(outer.root, "clone", "--no-hardlinks", "--no-checkout", str(outer.worktree), str(workspace))
            git(workspace, "checkout", "--detach", outer.integrated_base)
            (workspace / changed).write_text("reworked value\n")
            git(workspace, "add", changed)
            git(workspace, "commit", "-m", "review requested rework")
            head = git(workspace, "rev-parse", "HEAD")
            data = subprocess.check_output(
                ["git", "diff", "--binary", "--full-index", "--no-renames",
                 outer.integrated_base, head, "--"],
                cwd=workspace,
            )
            destination = outer.root / "artifacts/executions/rework-child/coder.patch"
            destination.write_bytes(data)
            ref = {
                "path": destination.relative_to(outer.root).as_posix(),
                "sha256": sha256(data).hexdigest(),
                "metadata": {
                    "task_id": "author-a",
                    "base": outer.integrated_base,
                    "start": outer.integrated_base,
                    "head": head,
                    "paths": [changed],
                    "generation": 91,
                },
            }
            if drift:
                (outer.worktree / "b.txt").write_text("concurrent drift\n")
                git(outer.worktree, "add", "b.txt")
                git(outer.worktree, "commit", "-m", "unexpected concurrent writer")
            return _result(command, "completed", outputs={
                "development_task_id": "author-a",
                "base": outer.integrated_base,
                "start": outer.integrated_base,
                "head": head,
                "paths": [changed],
                "artifact_refs": {"coder_patch": ref},
            })

        return invoke

    def run_rework(self, fake):
        with patch("modport.rework_coder.CoderHandler.__call__", fake):
            return run_coder_rework(
                self.child,
                self.original,
                self.worktree,
                "Apply the review finding to A and preserve all integrated work.",
            )

    def test_integrates_only_fresh_delta_and_preserves_review_and_peer_change(self):
        result = self.run_rework(self.fake_coder())
        self.assertEqual("completed", result.status, result.detail)
        self.assertEqual("reworked value\n", (self.worktree / "a.txt").read_text())
        self.assertEqual("already integrated b\n", (self.worktree / "b.txt").read_text())
        self.assertEqual("review in progress\n", self.report.read_text())
        self.assertEqual(self.integrated_base, result.outputs["before_head"])
        self.assertNotEqual(self.integrated_base, result.outputs["after_head"])
        self.assertEqual("coder-original", result.outputs["target_execution_id"])
        self.assertEqual({"request_id": "request-1"}, result.outputs["review_tool_context"])
        self.assertEqual(["a.txt"], result.outputs["paths"])
        self.assertIn("coder_rework_integration", result.outputs["artifact_refs"])

    def test_rejects_delta_outside_original_coder_ownership(self):
        result = self.run_rework(self.fake_coder("b.txt"))
        self.assertEqual("failed", result.status)
        self.assertEqual("coder_rework_invalid", result.error_code)
        self.assertIn("unowned", result.detail)
        self.assertEqual(self.integrated_base, git(self.worktree, "rev-parse", "HEAD"))
        self.assertEqual("already integrated b\n", (self.worktree / "b.txt").read_text())
        self.assertEqual("review in progress\n", self.report.read_text())

    def test_rejects_reviewer_base_drift_without_replaying_or_resetting_it(self):
        result = self.run_rework(self.fake_coder(drift=True))
        self.assertEqual("failed", result.status)
        self.assertIn("HEAD changed", result.detail)
        self.assertNotEqual(self.integrated_base, git(self.worktree, "rev-parse", "HEAD"))
        self.assertEqual("concurrent drift\n", (self.worktree / "b.txt").read_text())
        self.assertEqual("original a\n", (self.worktree / "a.txt").read_text())
        self.assertEqual("review in progress\n", self.report.read_text())

    def test_model_budget_timeout_preserves_rework_artifacts_and_diagnostics(self):
        def timed_out(_handler, command):
            expired = replace(
                command,
                options={**command.options, "model_deadline_epoch": 0},
            )
            try:
                _remaining_timeout(expired, 120)
            except TimeoutError as exc:
                exc.metadata = {"phase": "model", "reason": "deadline exhausted"}
                raise

        result = self.run_rework(timed_out)

        self.assertEqual("failed", result.status)
        self.assertEqual("budget_exhausted", result.error_code)
        self.assertEqual("execution has no remaining model time", result.detail)
        self.assertEqual(
            {"phase": "model", "reason": "deadline exhausted"},
            result.outputs["inner_diagnostics"],
        )
        refs = result.outputs["artifact_refs"]
        for name in ("coder_rework_request", "coder_rework_plan", "coder_rework_goal"):
            self.assertIn(name, refs)
            artifact = self.root / refs[name]["path"]
            self.assertTrue(artifact.is_file())
            self.assertEqual(refs[name]["sha256"], sha256(artifact.read_bytes()).hexdigest())
        self.assertEqual("review in progress\n", self.report.read_text())

    def test_plan_is_rebased_to_integrated_head_and_keeps_reviewed_checks(self):
        original_goal = json.loads((self.root / self.original.artifact_refs["coder_goal"]["path"])
                                   .read_text(encoding="utf-8"))
        original_goal["objective"] += "\n\nCoder context:\nLegacy prepared context."
        goal_ref = _artifact(self.original, "legacy-coder-goal-with-context.json",
                             json.dumps(original_goal, sort_keys=True).encode())
        self.original = replace(self.original, artifact_refs={"coder_goal": goal_ref})
        observed = {}

        def inspect(_handler, command):
            plan = json.loads((self.root / command.artifact_refs["development_plan"]["path"]).read_text())
            goal = json.loads((self.root / command.artifact_refs["coder_goal"]["path"]).read_text())
            observed.update(plan=plan, goal=goal)
            return self.fake_coder()(_handler, command)

        result = self.run_rework(inspect)
        self.assertEqual("completed", result.status, result.detail)
        self.assertEqual(self.integrated_base, observed["plan"]["base_commit"])
        self.assertEqual([], observed["plan"]["tasks"][0]["dependencies"])
        self.assertEqual(self.context, observed["goal"]["context_refs"])
        self.assertIn("Reviewer-requested rework", observed["goal"]["objective"])
        self.assertNotIn("Legacy prepared context.", observed["goal"]["objective"])
        self.assertEqual(
            self.original.payload["development_task"]["validation_checks"],
            observed["goal"]["checks"],
        )

    def test_current_workflow_rework_rebinds_the_actual_coder_plan_and_context(self):
        old_base = git(self.worktree, "rev-parse", "HEAD^")
        source_task = {**self.original.payload["development_task"], "dependencies": ["peer"]}
        source_plan = validate_plan({
            "schema_version": 1,
            "base_commit": old_base,
            "shared_paths": [],
            "tasks": [source_task, {**source_task, "id": "peer", "dependencies": [],
                                    "objective": "Prepare the shared peer interface"}],
        }, workflow_version=WORKFLOW_VERSION)
        source_plan_ref = _artifact(
            self.original,
            "source-development-plan.json",
            json.dumps(source_plan, ensure_ascii=False, sort_keys=True).encode(),
            {"development_base": old_base},
        )
        source_context = {**self.context, "development_plan": source_plan_ref}
        source_goal = json.loads(
            (self.root / self.original.artifact_refs["coder_goal"]["path"]).read_text()
        )
        source_goal["context_refs"] = source_context
        source_goal["objective"] += "\n\nCoder context:\nPreserve the integrated peer change."
        source_goal_ref = _artifact(
            self.original,
            "source-coder-goal-v26.json",
            json.dumps(source_goal, ensure_ascii=False, sort_keys=True).encode(),
        )
        original = replace(
            self.original,
            payload={**self.original.payload,
                     "development_task": next(task for task in source_plan["tasks"]
                                              if task["id"] == source_task["id"]),
                     "development_base": old_base,
                     "execution_development_plan": source_plan},
            options={"workflow_version": WORKFLOW_VERSION},
            artifact_refs={"development_plan": source_plan_ref,
                           "coder_goal": source_goal_ref},
        )
        self.assertEqual(["peer"], original.payload["development_task"]["dependencies"])
        self.assertEqual(["peer"], original.payload["development_task"]["dependencies"])
        normalized_task = original.payload["development_task"]
        target = {"key": target_key(normalized_task, source_plan_ref),
                  "task": normalized_task, "plan_ref": source_plan_ref,
                  "source_execution_id": original.command_id,
                  "source_workspace": "worktree", "goal_ref": source_goal_ref}
        supervisor = OperationInput(
            "run", "supervisor.window.5", "supervisor", "supervisor-5", str(self.root),
            options={"workflow_version": WORKFLOW_VERSION},
            payload={"supervised_goal_targets": [target]},
        )
        prepared = prepare(supervisor)
        document = (self.root / prepared.options["workspace"] / "goals"
                    / (target["key"] + ".md"))
        document.write_text("Diagnose the root cause before editing A.", encoding="utf-8")
        collected = collect(prepared, OperationResult(
            status="completed", run_id="run", task_id=supervisor.task_id,
            stage_id="supervisor", command_id=supervisor.command_id))
        revision_ref = collected.outputs["supervised_goal_revisions"][0]["revision_ref"]
        # Exercise both replay of the original supervised goal and binding a
        # later revision, under the real child's SDK execution identity.
        original = replace(original, artifact_refs={**original.artifact_refs,
                           "supervised_goal_revision": revision_ref})
        from modport.supervised_goals import apply_to_goal
        source_goal = apply_to_goal(original, source_goal, normalized_task)
        source_goal_ref = _artifact(original, "source-coder-goal-supervised.json",
                                    json.dumps(source_goal).encode())
        original = replace(original, artifact_refs={**original.artifact_refs,
                           "coder_goal": source_goal_ref})
        original_receipt = self.root / f"artifacts/executions/{original.command_id}/supervised-goal-application.json"
        original_receipt_bytes = original_receipt.read_bytes()
        document.write_text("Diagnose the root cause before editing A. Preserve B.", encoding="utf-8")
        collected = collect(prepared, OperationResult(
            status="completed", run_id="run", task_id=supervisor.task_id,
            stage_id="supervisor", command_id=supervisor.command_id))
        revision_ref = collected.outputs["supervised_goal_revisions"][0]["revision_ref"]
        child = replace(
            self.child,
            payload={**self.child.payload,
                     "development_base": old_base,
                     "execution_development_plan": source_plan,
                     "rework_context_refs": source_context},
            options={**self.child.options, "workflow_version": WORKFLOW_VERSION},
            artifact_refs={"supervised_goal_revision": revision_ref},
        )
        observed = {}
        outer = self

        class StopAfterCoderPreparation:
            def __init__(self, _prompt, **kwargs):
                self.goal = kwargs["native_goal"]

            def __call__(self, command):
                outer.assertIn("Diagnose the root cause before editing A.",
                               self.goal["objective"])
                outer.assertIn("Preserve the integrated peer change.",
                               self.goal["objective"])
                outer.assertIn("Reviewer-requested rework", self.goal["objective"])
                plan_ref = command.artifact_refs["development_plan"]
                plan = json.loads((outer.root / plan_ref["path"]).read_text())
                goal = json.loads(
                    (outer.root / command.artifact_refs["coder_goal"]["path"]).read_text()
                )
                outer.assertEqual(outer.integrated_base, command.payload["development_base"])
                outer.assertEqual(plan["base_commit"], command.payload["development_base"])
                outer.assertEqual(plan, command.payload["execution_development_plan"])
                outer.assertEqual(plan_ref, command.payload["planning_context"]["development_plan"])
                outer.assertEqual(
                    source_plan_ref,
                    command.payload["planning_context"]["rework_source_development_plan"],
                )
                outer.assertEqual(command.payload["planning_context"], goal["context_refs"])
                outer.assertEqual(source_task["id"], command.payload["development_task"]["id"])
                outer.assertEqual(source_task["objective"], command.payload["development_task"]["objective"])
                outer.assertEqual([], command.payload["development_task"]["dependencies"])
                outer.assertEqual(plan_ref, command.artifact_refs["development_plan"])
                outer.assertNotIn("supervised_goal_revision", command.artifact_refs)
                observed.update(plan=plan, goal=goal, command=command)
                raise ValueError("stop after checking the real CoderHandler handoff")

        sdk_context = SimpleNamespace(
            command=SimpleNamespace(execution_id=child.command_id,
                                    timeout_seconds=300, payload=child.to_dict()),
            lease=SimpleNamespace(expires_at=time.time() + 335))
        with execution_budget(sdk_context), patch(
                "modport.handlers.CodexStageHandler", StopAfterCoderPreparation):
            result = run_coder_rework(
                child,
                original,
                self.worktree,
                "Apply the review finding to A and preserve all integrated work.",
            )

        self.assertEqual("blocked", result.status, result.detail)
        self.assertIn("stop after checking", result.detail)
        self.assertIn("development_plan", observed["goal"]["context_refs"])
        self.assertIn("Diagnose the root cause before editing A.", observed["goal"]["objective"])
        self.assertIn("Preserve the integrated peer change.", observed["goal"]["objective"])
        self.assertIn("Reviewer-requested rework", observed["goal"]["objective"])
        request_ref = result.outputs["artifact_refs"]["coder_rework_request"]
        request = json.loads((self.root / request_ref["path"]).read_text())
        self.assertEqual(
            dict(original.artifact_refs),
            request["source_artifact_refs"],
        )
        self.assertEqual(revision_ref, request["selected_supervised_goal_revision"])
        self.assertEqual(source_plan, child.payload["execution_development_plan"])
        self.assertEqual(source_plan_ref, original.artifact_refs["development_plan"])
        self.assertEqual("review in progress\n", self.report.read_text())
        self.assertEqual(original_receipt_bytes, original_receipt.read_bytes())
        receipt = json.loads((self.root / f"artifacts/executions/{child.command_id}/supervised-goal-application.json").read_text())
        self.assertEqual(child.command_id, receipt["consumer_execution_id"])
        self.assertEqual(revision_ref, receipt["revision_ref"])

    def test_current_rework_preserves_source_authentication_error(self):
        original = replace(self.original, options={"workflow_version": WORKFLOW_VERSION})
        child = replace(self.child, options={**self.child.options,
                                            "workflow_version": WORKFLOW_VERSION})
        (self.root / original.artifact_refs["coder_goal"]["path"]).write_text("{}")
        sdk_context = SimpleNamespace(
            command=SimpleNamespace(execution_id=child.command_id,
                                    timeout_seconds=300, payload=child.to_dict()),
            lease=SimpleNamespace(expires_at=time.time() + 335))
        with execution_budget(sdk_context), patch("modport.rework_coder.CoderHandler") as coder:
            result = run_coder_rework(child, original, self.worktree, "Apply review finding.")
        coder.assert_not_called()
        self.assertEqual("failed", result.status)
        self.assertIn("digest mismatch", result.detail)

    def test_supervised_rework_rejects_missing_source_goal(self):
        original = replace(
            self.original,
            options={"workflow_version": WORKFLOW_VERSION},
            artifact_refs={"supervised_goal_revision": {
                "path": "artifacts/missing-revision.json", "sha256": "0" * 64}},
        )
        child = replace(self.child,
                        options={**self.child.options, "workflow_version": WORKFLOW_VERSION})
        result = run_coder_rework(child, original, self.worktree,
                                  "Apply the reviewer finding.")
        self.assertEqual("failed", result.status)
        self.assertIn("authenticated source goal", result.detail)

    def _use_advisory_inputs(self):
        self.child = replace(self.child, options={**self.child.options, 'workflow_version': 17},
            payload={**self.child.payload, 'rework_context_refs': {}})
        self.original = replace(self.original, options={'workflow_version': 17}, artifact_refs={},
            payload={**self.original.payload, 'planning_context': {},
                     'development_task': {'id': 'author-a', 'objective': 'Use the available report'}})

    def test_v17_rework_accepts_missing_goal_and_partial_task_context(self):
        self._use_advisory_inputs()
        result = self.run_rework(self.fake_coder())
        self.assertEqual('completed', result.status, result.detail)
        self.assertEqual('reworked value\n', (self.worktree / 'a.txt').read_text())
        self.assertEqual('review in progress\n', self.report.read_text())

    def test_v17_failed_coder_revision_integrates_its_available_patch(self):
        self._use_advisory_inputs()
        invoke = self.fake_coder()
        def failed(_handler, command):
            return replace(invoke(_handler, command), status='failed', error_code='agent_failed')
        result = self.run_rework(failed)
        self.assertEqual('failed', result.status, result.detail)
        self.assertEqual('agent_failed', result.error_code)
        self.assertEqual('reworked value\n', (self.worktree / 'a.txt').read_text())
        self.assertIn('coder_rework_integration', result.outputs['artifact_refs'])
        self.assertNotEqual(self.integrated_base, result.outputs['after_head'])
        self.assertEqual('integrated', result.outputs['integration_status'])

    def test_v17_empty_patch_is_reported_as_no_changes(self):
        self._use_advisory_inputs()
        invoke = self.fake_coder()

        def empty(_handler, command):
            result = invoke(_handler, command)
            ref = result.outputs['artifact_refs']['coder_patch']
            (self.root / ref['path']).write_bytes(b'')
            ref['sha256'] = sha256(b'').hexdigest()
            ref['metadata']['paths'] = []
            result.outputs['paths'] = []
            return replace(result, status='failed', error_code='agent_failed')

        result = self.run_rework(empty)
        self.assertEqual('failed', result.status)
        self.assertEqual('no_changes', result.outputs['integration_status'])
        self.assertEqual(self.integrated_base, result.outputs['after_head'])
        self.assertIn('no integrated changes', result.detail)


if __name__ == "__main__":
    unittest.main()
