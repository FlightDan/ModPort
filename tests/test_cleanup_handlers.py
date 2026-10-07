"""Real-Git cleanup handlers using only a controlled local OpenCode model server."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from modport.contracts import OperationInput
from modport.evidence import file_digest
from modport.manifest import canonical_json
from modport.prompt_compressor import CompressedPrompt
from modport.rubric import acceptance_rubric
from test_opencode_agent import _Server


POLICY = {"version": 1, "turns": ["plan", "execute"]}


class _CleanupServer(_Server):
    def __init__(self, *, final_text: str, mutate=None):
        super().__init__()
        self.final_text = final_text
        self.mutate = mutate
        self.workspace: Path | None = None
        self.plan_seen_by_execution: str | None = None

    def mcp_status(self, *, cwd, deadline):
        return {"modport_sandbox": {"status": "connected"}}

    def send_message(self, session_id, prompt, **kwargs):
        if len(self.turns) == 1:
            self.plan_seen_by_execution = self._messages["msg_test_1"]["parts"][0]["text"]
            if self.mutate is not None:
                self.mutate(self.workspace)
        response = super().send_message(session_id, prompt, **kwargs)
        if len(self.turns) == 2:
            response["parts"] = [{"type": "text", "text": self.final_text}]
        return response


class _Compressor:
    def compress(self, text, *, model, **_kwargs):
        return CompressedPrompt(text, {
            "compressed": False,
            "model": model,
            "context_window": 1_000_000,
            "input_token_budget": 700_000,
            "output_token_reserve": 200_000,
            "tool_token_reserve": 100_000,
            "original_sha256": hashlib.sha256(text.encode()).hexdigest(),
        })


class CleanupHandlerTests(unittest.TestCase):
    @staticmethod
    def _git(*args: str, cwd: Path) -> str:
        result = subprocess.run(
            ["git", *args], cwd=cwd, check=True, capture_output=True, text=True)
        return result.stdout.strip()

    @classmethod
    def _repository(cls, workspace: Path, files: dict[str, str]) -> str:
        workspace.mkdir(parents=True, exist_ok=True)
        cls._git("init", "-q", cwd=workspace)
        cls._git("config", "user.name", "Cleanup Test", cwd=workspace)
        cls._git("config", "user.email", "cleanup-test@localhost", cwd=workspace)
        for relative, content in files.items():
            path = workspace / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
        cls._git("add", "--all", cwd=workspace)
        cls._git("commit", "--no-gpg-sign", "-m", "integrated migration candidate", cwd=workspace)
        return cls._git("rev-parse", "HEAD", cwd=workspace)

    @staticmethod
    def _command(root: Path, stage: str, *, payload=None) -> OperationInput:
        rubric = acceptance_rubric()
        refs = {}
        for alias in ("agent_rules", "evidence_protocol"):
            path = root / "artifacts" / f"{alias}.md"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(f"Shared {alias} for this cleanup assignment.\n", encoding="utf-8")
            refs[alias] = {
                "path": path.relative_to(root).as_posix(),
                "sha256": file_digest(path),
                "media_type": "text/markdown",
            }
        rubric_path = root / "artifacts" / "acceptance-rubric.json"
        rubric_path.write_text(canonical_json(rubric) + "\n", encoding="utf-8")
        refs["acceptance_rubric"] = {
            "path": rubric_path.relative_to(root).as_posix(),
            "sha256": file_digest(rubric_path),
            "media_type": "application/json",
            "metadata": {"rubric_sha256": rubric["rubric_sha256"]},
        }
        source_material = root / "artifacts" / "source-notes.md"
        source_material.write_text("Authenticated source locator.\n", encoding="utf-8")
        refs["source_notes"] = {
            "path": source_material.relative_to(root).as_posix(),
            "sha256": file_digest(source_material),
            "media_type": "text/markdown",
        }
        return OperationInput(
            run_id="cleanup-test-run",
            task_id=stage,
            stage_id=stage,
            command_id=f"{stage}-execution",
            run_dir=str(root),
            payload=payload or {},
            options={
                "workflow_version": 30,
                "agent_dialogue_policy": POLICY,
                "model": "gpt-6-luna",
                "reasoning_effort": "max",
                "acceptance_rubric_sha256": rubric["rubric_sha256"],
            },
            artifact_refs=refs,
        )

    @staticmethod
    def _invoke(handler, server: _CleanupServer):
        def start(**kwargs):
            server.workspace = kwargs["cwd"]
            return server

        with (
            patch("modport.handlers.PromptCompressor.from_environment", return_value=_Compressor()),
            patch("modport.rework_tools.prepare_session", return_value=None),
            patch("modport.rework_tools.opencode_tool_config", return_value={}),
            patch("modport.opencode_agent.OpenCodeServer.start", side_effect=start),
        ):
            return handler()

    def test_research_index_uses_read_only_two_turn_source_and_archives_identity(self):
        from modport.cleanup import ResearchCleanupHandler
        from modport.prompts import STAGE_PROMPTS

        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            baseline = root / "baseline"
            before = self._repository(baseline, {
                "build.gradle": "plugins { id 'java' }\n",
                "src/main/java/Example.java": "class Example {}\n",
            })
            baseline_bytes = (baseline / "src/main/java/Example.java").read_bytes()
            command = self._command(root, "research_cleanup")
            server = _CleanupServer(final_text="# Source index\n- `src/main/java/Example.java`\n")

            result = self._invoke(lambda: ResearchCleanupHandler()(command), server)

            self.assertEqual("completed", result.status, result.detail)
            self.assertEqual(2, len(server.turns))
            self.assertEqual(server.turns[0][0], server.turns[1][0])
            self.assertIn("task-instructions.plan.json", server.turns[0][1])
            self.assertIn("task-instructions.execute.json", server.turns[1][1])
            self.assertEqual("Plan fact: nonce-83749", server.plan_seen_by_execution)
            execution_task = json.loads((root / "artifacts" / "executions" / command.command_id
                                        / "task-instructions.execute.json").read_text())['task']
            self.assertIn(STAGE_PROMPTS["research_cleanup"], execution_task)
            self.assertIn("Do not edit, delete, rename, or rewrite", execution_task)
            plan_task = json.loads((root / "artifacts" / "executions" / command.command_id
                                    / "task-instructions.plan.json").read_text())['task']
            self.assertIn("Turn 1 of 2: prepare only a task plan", plan_task)
            for turn in server.turns:
                for tool in ("edit", "write", "apply_patch", "patch", "multiedit"):
                    self.assertIs(turn[2]["tools"][tool], False)
            self.assertEqual(before, self._git("rev-parse", "HEAD", cwd=baseline))
            self.assertEqual(baseline_bytes, (baseline / "src/main/java/Example.java").read_bytes())
            ref = result.outputs["artifact_refs"]["research_cleanup"]
            self.assertEqual("text/markdown", ref["media_type"])
            self.assertEqual(before, ref["metadata"]["source_commit"])
            self.assertEqual("baseline", ref["metadata"]["source_workspace"])
            self.assertEqual(
                command.artifact_refs["source_notes"]["sha256"],
                ref["metadata"]["material_refs"]["source_notes"]["sha256"],
            )
            self.assertIn("# Source index", (root / ref["path"]).read_text(encoding="utf-8"))
            self.assertIn("agent_last_message", result.outputs["artifact_refs"])

    def test_research_missing_index_is_advisory_and_keeps_raw_agent_output(self):
        from modport.cleanup import ResearchCleanupHandler

        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            baseline = root / "baseline"
            self._repository(baseline, {"Source.java": "class Source {}\n"})
            command = self._command(root, "research_cleanup")
            server = _CleanupServer(final_text=" \n ")

            result = self._invoke(lambda: ResearchCleanupHandler()(command), server)

            self.assertEqual("completed", result.status)
            self.assertEqual("unverified", result.outputs["acceptance_status"])
            self.assertNotIn("research_cleanup", result.outputs["artifact_refs"])
            self.assertIn("agent_last_message", result.outputs["artifact_refs"])
            self.assertTrue(any("unavailable or empty" in item
                                for item in result.outputs["business_diagnostics"]))

    def test_code_cleanup_integrates_edits_and_preserves_empty_report_and_reviewer_output(self):
        from modport.cleanup import CodeCleanupHandler

        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            worktree = root / "worktree"
            self._repository(worktree, {
                "src/Example.java": "class Example { int value() { return 1; } }\n",
                ".modport/functional-contract.json": '{"behavior":"stable"}\n',
                ".modport/tests/ExampleTest.java": "assertEquals(1, example.value());\n",
            })
            source = worktree / "src/Example.java"
            frozen_paths = [worktree / ".modport/functional-contract.json",
                            worktree / ".modport/tests/ExampleTest.java"]
            frozen_bytes = [path.read_bytes() for path in frozen_paths]
            reviewer_output = worktree / "reviewer-report.md"
            reviewer_output.write_text("Preserve this untracked reviewer report.\n", encoding="utf-8")
            report_bytes = reviewer_output.read_bytes()
            command = self._command(root, "code_cleanup", payload={
                "reviewer_report_paths": ["reviewer-report.md"],
                "reviewer_rework": {"instructions": "Remove the duplicate helper safely."},
            })

            def edit_candidate(workspace: Path):
                (workspace / "src/Example.java").write_text(
                    "class Example { int value() { return 1; } } // simplified\n",
                    encoding="utf-8")

            server = _CleanupServer(final_text=" \n ", mutate=edit_candidate)
            result = self._invoke(lambda: CodeCleanupHandler()(command), server)

            self.assertEqual("completed", result.status, result.detail)
            self.assertEqual(2, len(server.turns))
            self.assertEqual(server.turns[0][0], server.turns[1][0])
            self.assertEqual("Plan fact: nonce-83749", server.plan_seen_by_execution)
            self.assertIn("task-instructions.plan.json", server.turns[0][1])
            self.assertIn("task-instructions.execute.json", server.turns[1][1])
            execution_task = json.loads((root / "artifacts" / "executions" / command.command_id
                                        / "task-instructions.execute.json").read_text())['task']
            self.assertIn("Reviewer-requested cleanup revision", execution_task)
            self.assertIn("Remove the duplicate helper safely.", execution_task)
            self.assertNotEqual("class Example { int value() { return 1; } }\n",
                                source.read_text(encoding="utf-8"))
            self.assertEqual(frozen_bytes,
                             [path.read_bytes() for path in frozen_paths])
            self.assertEqual(report_bytes, reviewer_output.read_bytes())
            self.assertEqual("?? reviewer-report.md", self._git(
                "status", "--porcelain=v1", cwd=worktree))
            refs = result.outputs["artifact_refs"]
            self.assertNotIn("code_cleanup_report", refs)
            self.assertIn("code_cleanup_patch", refs)
            self.assertIn("code_cleanup_candidate", refs)
            self.assertTrue(any("report was unavailable or empty" in item
                                for item in result.outputs["business_diagnostics"]))
            candidate = json.loads((root / refs["code_cleanup_candidate"]["path"])
                                   .read_text(encoding="utf-8"))
            self.assertEqual("integrated", candidate["status"])
            self.assertEqual(candidate["integrated_candidate"], self._git(
                "rev-parse", "HEAD", cwd=worktree))
            self.assertIn("src/Example.java", candidate["paths"])

    def test_partial_apply_timeout_rolls_back_candidate_and_restores_reviewer_report(self):
        from modport.cleanup import CodeCleanupHandler
        from modport.execution_budget import current_settlement_budget, execution_budget

        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            worktree = root / "worktree"
            before = self._repository(worktree, {
                "src/Example.java": "class Example { int value() { return 1; } }\n",
                ".modport/functional-contract.json": '{"behavior":"stable"}\n',
            })
            original_source = (worktree / "src/Example.java").read_bytes()
            reviewer_output = worktree / "reviewer-report.md"
            reviewer_output.write_text("Reviewer output survives rollback.\n", encoding="utf-8")
            report_bytes = reviewer_output.read_bytes()
            report_mode = reviewer_output.stat().st_mode & 0o777
            command = self._command(root, "code_cleanup", payload={
                "reviewer_report_paths": ["reviewer-report.md"],
            })

            def edit_candidate(workspace: Path):
                (workspace / "src/Example.java").write_text(
                    "class Example { int value() { return 1; } } // cleaned\n",
                    encoding="utf-8")
                (workspace / "src/Added.java").write_text("class Added {}\n", encoding="utf-8")

            server = _CleanupServer(final_text="Applied the cleanup.", mutate=edit_candidate)
            clock = {"now": 1000.0}
            context = SimpleNamespace(
                command=SimpleNamespace(execution_id=command.command_id,
                                        timeout_seconds=1000, payload={}),
                lease=SimpleNamespace(expires_at=2000.0),
            )

            def partial_apply(command_arg, target, patch_path, _task):
                applied = subprocess.run(
                    ["git", "apply", "--index", "--3way", "--", str(patch_path)],
                    cwd=target, capture_output=True, text=True,
                )
                if applied.returncode:
                    raise AssertionError(applied.stderr)
                self.assertIn("A  src/Added.java", self._git(
                    "status", "--porcelain=v1", cwd=target))
                budget = current_settlement_budget(command_arg)
                self.assertIsNotNone(budget)
                clock["now"] = budget.capture_deadline + 1
                self.assertLess(clock["now"], budget.publication_deadline)
                raise TimeoutError("simulated timeout after capture deadline and index update")

            with (
                patch("modport.handlers.PromptCompressor.from_environment", return_value=_Compressor()),
                patch("modport.rework_tools.prepare_session", return_value=None),
                patch("modport.rework_tools.opencode_tool_config", return_value={}),
                patch("modport.opencode_agent.OpenCodeServer.start", side_effect=lambda **kwargs: (
                    setattr(server, "workspace", kwargs["cwd"]) or server)),
                patch("modport.development._apply", side_effect=partial_apply),
            ):
                with execution_budget(context, now=lambda: clock["now"]):
                    result = CodeCleanupHandler()(command)

            self.assertEqual("failed", result.status)
            self.assertEqual("cleanup_integrity", result.error_code)
            self.assertIn("simulated timeout after capture deadline", result.detail)
            self.assertEqual(before, self._git("rev-parse", "HEAD", cwd=worktree))
            self.assertEqual(original_source, (worktree / "src/Example.java").read_bytes())
            self.assertFalse((worktree / "src/Added.java").exists())
            self.assertEqual(report_bytes, reviewer_output.read_bytes())
            self.assertEqual(report_mode, reviewer_output.stat().st_mode & 0o777)
            self.assertEqual("?? reviewer-report.md", self._git(
                "status", "--porcelain=v1", cwd=worktree))
            refs = result.outputs["artifact_refs"]
            self.assertIn("code_cleanup_patch", refs)
            self.assertIn("code_cleanup_candidate", refs)
            candidate = json.loads((root / refs["code_cleanup_candidate"]["path"])
                                   .read_text(encoding="utf-8"))
            self.assertEqual("failed", candidate["status"])
            self.assertEqual(before, candidate["source_candidate"])
            self.assertEqual(before, candidate["integrated_candidate"])
            self.assertEqual("Applied the cleanup.", (root / refs["agent_last_message"]["path"])
                             .read_text(encoding="utf-8").strip())

    def test_code_cleanup_rejects_symlinked_execution_artifact_parent_before_writing(self):
        from modport.cleanup import CodeCleanupHandler

        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw) / "run"
            root.mkdir()
            worktree = root / "worktree"
            before = self._repository(worktree, {"src/Example.java": "class Example {}\n"})
            command = self._command(root, "code_cleanup")
            outside = Path(raw) / "outside"
            outside.mkdir()
            execution_parent = root / "artifacts" / "executions"
            execution_parent.mkdir(parents=True)
            (execution_parent / command.command_id).symlink_to(outside, target_is_directory=True)
            server = _CleanupServer(final_text="This should never be requested.")

            result = self._invoke(lambda: CodeCleanupHandler()(command), server)

            self.assertEqual("failed", result.status)
            self.assertEqual("cleanup_integrity", result.error_code)
            self.assertEqual([], list(outside.iterdir()))
            self.assertEqual([], server.turns)
            self.assertEqual(before, self._git("rev-parse", "HEAD", cwd=worktree))
            self.assertEqual("class Example {}\n",
                             (worktree / "src/Example.java").read_text(encoding="utf-8"))
            self.assertTrue(any("candidate record unavailable" in item
                                for item in result.outputs.get("business_diagnostics", [])))

    def test_registry_exposes_both_cleanup_handlers(self):
        from modport.handlers import build_registry

        registry = build_registry()
        self.assertIn("modport.research_cleanup", registry)
        self.assertIn("modport.code_cleanup", registry)


if __name__ == "__main__":
    unittest.main()
