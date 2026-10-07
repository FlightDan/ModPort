"""Coder revival planning consumes failed-task evidence and emits a strict decision."""
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from dataclasses import replace
from unittest.mock import patch

from modport.contracts import OperationInput
from modport.evidence import verified_path
from modport.handlers import _result
from modport.revival_planning import (
    CoderRevivalPlannerHandler,
    build_revival_planning_prompt,
    validate_decision,
)


def _ref(root: Path, relative: str, contents: str, media_type="text/plain"):
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(contents, encoding="utf-8")
    return {"path": relative, "sha256": hashlib.sha256(contents.encode()).hexdigest(),
            "media_type": media_type}


def _request(root: Path):
    error_ref = _ref(root, "logs/task-A-error.log",
                     "Dependency resolution timed out while fetching artifact X; process exit 124.\n")
    patch_ref = _ref(root, "artifacts/task-A.patch", "diff --git a/src/A.java b/src/A.java\n", "text/x-diff")
    return {
        "request_id": "revival-request-4",
        "generation": 4,
        "base_commit": "a" * 40,
        "trigger_execution_ids": ["coder.g4.B.execution-2"],
        "requested_tasks": ["A", "B", "C"],
        "required_tasks": ["A", "C"],
        "tasks": [
            {"id": "A", "objective": "Repair the client API call", "dependencies": []},
            {"id": "B", "objective": "Complete the shared provider change", "dependencies": []},
            {"id": "C", "objective": "Adapt the downstream integration", "dependencies": ["B"]},
        ],
        "results": {
            "A": {
                "status": "failed", "task_id": "coder.g4.A", "stage_id": "coder",
                "error_code": "agent_failed", "detail": "coding agent failed",
                "outputs": {"raw_report": "coder exited", "artifact_refs": {"error_log": error_ref,
                                                                                 "partial_patch": patch_ref}},
            },
        },
        "attempts": {"A": 2, "B": 1, "C": 0},
        "budget_context": {"deadline_epoch": 1_798_244_100,
                           "agent_assignments_used": 31, "agent_assignments_limit": 210},
        "execution_evidence": {
            "A": {"state": "succeeded", "error": None,
                  "result": {"status": "failed", "error_code": "agent_failed"}},
            "B": {"state": "running", "error": None, "result": None},
        },
        "prior_decisions": [],
    }


def _command(root: Path, request):
    context_ref = _ref(root, "artifacts/context.json", '{"locked_api":"neo target"}\n',
                       "application/json")
    return OperationInput(
        "run-revival", "revival-planner", "coder_revival_plan", "revival-execution",
        str(root), payload={"revival_request": request},
        options={"workflow_version": 25,
                 "agent_dialogue_policy": {"version": 1, "turns": ["plan", "execute"]}},
        artifact_refs={
            "failed_task_log": request["results"]["A"]["outputs"]["artifact_refs"]["error_log"],
            "partial_patch": request["results"]["A"]["outputs"]["artifact_refs"]["partial_patch"],
            "migration_context": context_ref,
        },
    )


def _report(*, include_required=True):
    decisions = [
        {"task_id": "A", "action": "resume",
         "instruction": ("The authenticated task-A-error.log records dependency fetch timeout and exit 124, "
                         "while the process log has no OOM witness. Inspect the dependency cache and "
                         "current resolver configuration, then make a targeted retry-safe correction."),
         "wait_for": [], "reuse_partial": False},
    ]
    if include_required:
        decisions.append({
            "task_id": "C", "action": "wait",
            "instruction": "Wait for B because C consumes the provider API that B supplies.",
            "wait_for": ["B"],
        })
    return json.dumps({"decisions": decisions,
                       "reason": "The raw A log identifies a dependency fetch timeout; C has a real provider prerequisite."})


class RevivalDecisionValidationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.request = _request(self.root)

    def test_normalizes_partial_patch_choice_and_permitted_wait(self):
        normalized = validate_decision(json.loads(_report()), self.request)
        self.assertEqual(["A", "C"], [row["task_id"] for row in normalized["decisions"]])
        self.assertFalse(normalized["decisions"][0]["reuse_partial"])
        self.assertEqual(["B"], normalized["decisions"][1]["wait_for"])
        defaulted = json.loads(_report())
        del defaulted["decisions"][0]["reuse_partial"]
        self.assertTrue(validate_decision(defaulted, self.request)["decisions"][0]["reuse_partial"])

    def test_requires_each_host_marked_required_task(self):
        with self.assertRaisesRegex(ValueError, "omit required tasks: C"):
            validate_decision(json.loads(_report(include_required=False)), self.request)

    def test_rejects_unknown_or_self_wait_but_accepts_nonempty_unicode_instruction(self):
        base = json.loads(_report())
        base["decisions"][1]["wait_for"] = ["missing"]
        with self.assertRaisesRegex(ValueError, "unknown task IDs"):
            validate_decision(base, self.request)
        base = json.loads(_report())
        base["decisions"][1]["wait_for"] = ["C"]
        with self.assertRaisesRegex(ValueError, "cannot wait for itself"):
            validate_decision(base, self.request)
        base = json.loads(_report())
        base["decisions"][0]["instruction"] = "检查原始日志并修复缓存配置。"
        del base["decisions"][0]["reuse_partial"]
        normalized = validate_decision(base, self.request)
        self.assertEqual("检查原始日志并修复缓存配置。", normalized["decisions"][0]["instruction"])
        self.assertTrue(normalized["decisions"][0]["reuse_partial"])


class CoderRevivalPlannerHandlerTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.request = _request(self.root)
        self.command = _command(self.root, self.request)

    def _fake_agent(self, report):
        response_ref = _ref(self.root, "logs/planner-final.json", report, "application/json")
        response_path = Path(response_ref["path"])
        return response_path.as_posix(), response_ref

    def test_read_only_two_turn_producer_preserves_failure_refs_and_binds_artifact(self):
        raw = _report()
        response_path, response_ref = self._fake_agent(raw)
        calls = []

        def fake_agent(handler, command):
            calls.append((handler.prompt, handler.read_only, handler.baseline, command))
            return _result(command, "completed", outputs={
                "last_message": response_path,
                "agent_dialogue": {"transport": "opencode", "turns": 2},
                "artifact_refs": {"agent_last_message": response_ref},
            })

        with patch("modport.handlers.CodexStageHandler.__call__", fake_agent):
            result = CoderRevivalPlannerHandler()(self.command)

        self.assertEqual("completed", result.status)
        prompt, read_only, baseline, received = calls[0]
        self.assertTrue(read_only)
        self.assertFalse(baseline)
        self.assertIs(received, self.command)
        self.assertIn("payload.revival_request and artifact_refs", prompt)
        self.assertIn("no fixed per-task retry cap", prompt)
        self.assertIn("exit 137", prompt)
        self.assertIn("SDK-delivered planner request", prompt)
        self.assertIn('"state":"succeeded"', prompt)
        self.assertIn('"agent_assignments_limit":210', prompt)
        self.assertIn("logs/task-A-error.log", prompt)
        self.assertFalse(result.outputs["revival_decision"]["decisions"][0]["reuse_partial"])
        self.assertEqual(["B"], result.outputs["revival_decision"]["decisions"][1]["wait_for"])

        refs = result.outputs["artifact_refs"]
        self.assertEqual(self.command.artifact_refs["failed_task_log"], refs["failed_task_log"])
        self.assertEqual(response_ref, refs["agent_last_message"])
        decision_path = verified_path(self.root, refs["revival_decision"])
        document = json.loads(decision_path.read_text(encoding="utf-8"))
        self.assertEqual("revival-request-4", document["request_id"])
        self.assertEqual(4, document["generation"])
        self.assertEqual("a" * 40, document["base_commit"])
        self.assertEqual(result.outputs["revival_decision"], document["decision"])
        self.assertEqual("application/json", refs["revival_decision"]["media_type"])

    def test_contract_scope_selects_baseline_workspace(self):
        raw = _report()
        response_path, response_ref = self._fake_agent(raw)
        contract_command = replace(
            self.command,
            command_id="contract-revival-execution",
            payload={**self.command.payload, "goal_scope": "contract"},
        )
        observed = []

        def fake_agent(handler, command):
            observed.append((handler.read_only, handler.baseline))
            return _result(command, "completed", outputs={
                "last_message": response_path,
                "agent_dialogue": {"transport": "opencode", "turns": 2},
                "artifact_refs": {"agent_last_message": response_ref},
            })

        with patch("modport.handlers.CodexStageHandler.__call__", fake_agent):
            result = CoderRevivalPlannerHandler()(contract_command)

        self.assertEqual("completed", result.status)
        self.assertEqual([(True, True)], observed)

    def test_invalid_planner_output_returns_failure_feedback_with_raw_report(self):
        raw = _report(include_required=False)
        response_path, response_ref = self._fake_agent(raw)

        def fake_agent(handler, command):
            return _result(command, "completed", outputs={
                "last_message": response_path,
                "agent_dialogue": {"transport": "opencode", "turns": 2},
                "artifact_refs": {"agent_last_message": response_ref},
            })

        with patch("modport.handlers.CodexStageHandler.__call__", fake_agent):
            result = CoderRevivalPlannerHandler()(self.command)

        self.assertEqual("failed", result.status)
        self.assertEqual("revival_decision_invalid", result.error_code)
        self.assertIn("omit required tasks: C", result.detail)
        self.assertEqual(raw, result.outputs["raw_report"])
        self.assertIn("agent_last_message", result.outputs["artifact_refs"])
        self.assertIn("failed_task_log", result.outputs["artifact_refs"])

    def test_prompt_factory_names_required_scope_without_copying_identity(self):
        prompt = build_revival_planning_prompt(self.request, self.command.artifact_refs)
        self.assertIn('["A", "B", "C"]', prompt)
        self.assertIn('["A", "C"]', prompt)
        self.assertNotIn(self.request["request_id"], prompt)
        self.assertNotIn(self.request["base_commit"], prompt)
        self.assertIn("Attempt counts are context", prompt)
        self.assertIn('"agent_assignments_used":31', prompt)
        self.assertIn('"path":"logs/task-A-error.log"', prompt)


if __name__ == "__main__":
    unittest.main()
