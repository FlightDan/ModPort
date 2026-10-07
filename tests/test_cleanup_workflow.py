"""Public-SDK routing tests for the v24 cleanup workers."""

from dataclasses import replace
import json
from pathlib import Path
import select
import subprocess
import sys
import tempfile
import threading
import time
import unittest

from modport import Budget, MigrationOperations, MigrationRequest
from modport.contracts import OperationInput, OperationResult
from modport.evidence import file_digest
from modport.handlers import ValidateInputHandler
from modport.kernel_runtime import operation_lock
from modport.memory_admission import MemorySnapshot
from modport.rework_tools import is_interactive_review, prepare_session, rework_targets, tool_arguments
from modport.workflow import compile_migration_workflow
from fixtures_modport import FixtureHandler, registry


def _git(root, *args):
    result = subprocess.run(["git", "-C", str(root), *args], check=True,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    return result.stdout.strip()


def _source_request(source, *, budget=None):
    return MigrationRequest(
        "cleanup-fixture", str(source), "1.20.1", "26.1.2",
        source_revision=_git(source, "rev-parse", "HEAD"),
        budget=budget or Budget(max_seconds=240, max_agent_assignments=100),
    )


def _make_source(parent):
    source = parent / "source"
    source.mkdir()
    subprocess.run(["git", "init", str(source)], check=True,
                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    subprocess.run(["git", "-C", str(source), "config", "user.name", "ModPort fixture"], check=True)
    subprocess.run(["git", "-C", str(source), "config", "user.email", "fixture@example.invalid"], check=True)
    (source / "Example.java").write_text("class Example {}\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(source), "add", "Example.java"], check=True)
    subprocess.run(["git", "-C", str(source), "commit", "-m", "source fixture"], check=True,
                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    return source


class ReportStage:
    __execution_kernel_revision__ = "cleanup-workflow-report-stage-v1"

    def __init__(self, stage_id, *, status="completed", barrier=None, observations=None):
        self.stage_id = stage_id
        self.status = status
        self.barrier = barrier
        self.observations = observations if observations is not None else {}

    def __call__(self, command):
        if command.stage_id != self.stage_id:
            raise AssertionError(f"expected {self.stage_id}, received {command.stage_id}")
        started = time.monotonic()
        integrated = command.upstream_results.get("development_integrate")
        self.observations.setdefault(command.task_id, {})["started"] = started
        self.observations[command.task_id]["integrated"] = integrated
        if self.barrier is not None:
            self.barrier.wait(timeout=20)
        root = Path(command.run_dir)
        path = root / "artifacts" / "cleanup-reports" / f"{command.task_id}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        body = f"# {self.stage_id}\n\nCleanup report for {command.task_id}.\n"
        path.write_text(body, encoding="utf-8")
        ref = {"path": path.relative_to(root).as_posix(), "sha256": file_digest(path),
               "media_type": "text/markdown"}
        aliases = ({self.stage_id: ref} if self.stage_id == "research_cleanup" else {
            "code_cleanup_report": ref,
            # CodexStageHandler preserves the raw report at this authenticated alias;
            # the wrapper's last_message path is what the reviewer tool returns.
            "agent_last_message": ref,
        })
        outputs = {"artifact_refs": aliases, "report": self.stage_id}
        if integrated is not None:
            outputs["integrated_candidate"] = integrated.get("outputs", {}).get("after_head")
        if self.stage_id == "code_cleanup":
            candidate_path = root / "artifacts" / "cleanup-reports" / f"{command.task_id}-candidate.json"
            candidate_path.write_text(json.dumps({"status": "integrated",
                "candidate": outputs.get("integrated_candidate")}), encoding="utf-8")
            patch_path = root / "artifacts" / "cleanup-reports" / f"{command.task_id}.patch"
            patch_path.write_text("fixture candidate patch\n", encoding="utf-8")
            outputs["artifact_refs"].update(
                code_cleanup_candidate={"path": candidate_path.relative_to(root).as_posix(),
                    "sha256": file_digest(candidate_path), "media_type": "application/json"},
                code_cleanup_patch={"path": patch_path.relative_to(root).as_posix(),
                    "sha256": file_digest(patch_path), "media_type": "application/octet-stream"},
            )
            outputs["last_message"] = path.relative_to(root).as_posix()
        self.observations[command.task_id]["finished"] = time.monotonic()
        return OperationResult(
            self.status, command.run_id, command.task_id, command.stage_id,
            command.command_id, outputs=outputs,
            detail="fixture cleanup report",
            error_code="fixture_failure" if self.status == "failed" else None,
        )


class OverlapPreparation:
    __execution_kernel_revision__ = "cleanup-workflow-overlap-preparation-v1"

    def __init__(self, barrier):
        self.barrier = barrier
        self.fixture = FixtureHandler()

    def __call__(self, command):
        if command.stage_id != "preparation":
            raise AssertionError("overlap fixture was not routed to preparation")
        self.barrier.wait(timeout=20)
        return self.fixture(command)


class MergedIntegrator:
    __execution_kernel_revision__ = "cleanup-workflow-integrator-v1"

    def __call__(self, command):
        result = FixtureHandler()(command)
        return replace(result, outputs={**result.outputs,
            "integration_status": "merged", "after_head": "b" * 40})


class ValidatedSource:
    __execution_kernel_revision__ = "cleanup-workflow-validated-source-v1"

    def __call__(self, command):
        return ValidateInputHandler()(command)


class RecordingBuild:
    __execution_kernel_revision__ = "cleanup-workflow-build-v1"

    def __init__(self):
        self.commands = []
        self.lock = threading.Lock()

    def __call__(self, command):
        with self.lock:
            self.commands.append(command)
        return FixtureHandler()(command)


class CleanupReworkReviewer:
    """Call the real MCP bridge and wait for cleanup plus fresh verification."""

    __execution_kernel_revision__ = "cleanup-workflow-reviewer-v1"

    def __call__(self, command):
        root = Path(command.run_dir)
        session = prepare_session(command, root / "worktree", 90)
        if session is None:
            raise AssertionError("v24 code review did not receive cleanup as a rework target")
        child = subprocess.Popen(
            [sys.executable, "-m", "modport.rework_mcp", "--session", str(session)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, bufsize=1,
        )

        def rpc(identity, method, params):
            child.stdin.write(json.dumps({"jsonrpc": "2.0", "id": identity,
                                          "method": method, "params": params}) + "\n")
            child.stdin.flush()
            if not select.select([child.stdout], [], [], 45)[0]:
                raise AssertionError("cleanup rework did not return to the review caller")
            return json.loads(child.stdout.readline())

        try:
            rpc(1, "initialize", {"protocolVersion": "2024-11-05", "capabilities": {},
                                  "clientInfo": {"name": "cleanup-workflow-test", "version": "1"}})
            tools = rpc(2, "tools/list", {})
            if "request_rework" not in str(tools):
                raise AssertionError("reviewer MCP session omitted request_rework")
            response = rpc(3, "tools/call", {"name": "request_rework", "arguments": {
                "target_agent": "code_cleanup",
                "instructions": "Revisit the integrated cleanup and report the result."}})
            result = response.get("result", {})
            if result.get("isError") or "Cleanup report for agent-rework" not in str(result):
                raise AssertionError("reviewer did not receive the cleanup report: " + str(result))
            (root / "cleanup-review-tool-result.json").write_text(
                json.dumps(result), encoding="utf-8")
        finally:
            child.stdin.close()
            try:
                child.wait(timeout=3)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait()
            child.stdout.close()
            child.stderr.close()
        return FixtureHandler()(command)


class CleanupWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.memory = lambda: MemorySnapshot(64 * 1024**3, 64 * 1024**3, "fixture")

    def _execute(self, request, root, handlers):
        operations = MigrationOperations(handlers=handlers, isolation_mode="thread",
                                         memory_probe=self.memory)
        run = operations.submit(request, run_dir=root, run_id="cleanup-v24")
        state = operations.execute(run, poll_interval=0.005).snapshot
        self.assertEqual(24, state["tasks"]["research_cleanup"]["attempts"][0]
                         ["command"]["payload"]["options"]["workflow_version"])
        return state

    def test_v24_public_sdk_parallel_report_merge_cleanup_review_and_verification(self):
        source = _make_source(self.base)
        request = _source_request(source)
        root = self.base / "run"
        barrier = threading.Barrier(2)
        observations = {}
        code_cleanup = ReportStage("code_cleanup", observations=observations)
        research_cleanup = ReportStage("research_cleanup", barrier=barrier)
        build = RecordingBuild()
        handlers = registry()
        handlers["modport.source"] = ValidatedSource()
        handlers["modport.preparation"] = OverlapPreparation(barrier)
        handlers["modport.research_cleanup"] = research_cleanup
        handlers["modport.development_integrate"] = MergedIntegrator()
        handlers["modport.code_cleanup"] = code_cleanup
        handlers["modport.target_build"] = build
        handlers["modport.code_review"] = CleanupReworkReviewer()

        state = self._execute(request, root, handlers)
        self.assertEqual("succeeded", state["state"])
        self.assertEqual("unverified", state["application_state"]["acceptance_status"])

        planner = state["tasks"]["migration_plan"]["attempts"][0]["command"]["payload"]
        research_ref = planner["artifact_refs"]["research_cleanup"]
        self.assertEqual("completed", planner["upstream_results"]["research_cleanup"]["status"])
        self.assertIn("Cleanup report for research_cleanup", (root / research_ref["path"]).read_text())

        integrate = state["tasks"]["development_integrate"]["attempts"][0]
        cleanup = state["tasks"]["code_cleanup"]["attempts"][0]
        target = state["tasks"]["target_build"]["attempts"][0]
        review = state["tasks"]["code_review"]["attempts"][0]
        self.assertEqual("merged", integrate["result"]["value"]["outputs"]["integration_status"])
        self.assertEqual(integrate["command"]["execution_id"], cleanup["command"]["causation_id"])
        self.assertEqual(integrate["command"]["execution_id"],
                         cleanup["command"]["payload"]["upstream_results"]["development_integrate"]["command_id"])
        self.assertEqual(cleanup["command"]["execution_id"], target["command"]["causation_id"])
        self.assertEqual("completed", target["command"]["payload"]["upstream_results"]["code_cleanup"]["status"])
        self.assertIn("code_cleanup_report", target["command"]["payload"]["artifact_refs"])
        review_command = OperationInput.from_dict(review["command"]["payload"])
        targets = review_command.payload["review_rework_targets"]
        self.assertIn("code_cleanup", {row["target_agent"] for row in targets})

        cleanup_command = OperationInput.from_dict(cleanup["command"]["payload"])
        self.assertFalse(is_interactive_review(cleanup_command))
        self.assertIsNone(prepare_session(cleanup_command, root / "worktree", 10))
        self.assertEqual([], tool_arguments(None, 10))
        self.assertEqual([], rework_targets(state, cleanup_command))
        self.assertEqual(root / ".locks" / "scopes" / "worktree",
                         operation_lock(root, cleanup_command))

        records = state["application_state"]["review_rework"]["requests"]
        self.assertEqual(1, len(records))
        record = next(iter(records.values()))
        self.assertEqual("completed", record["state"])
        self.assertEqual(["code_cleanup", "target_build"],
                         [row["stage"] for row in record["updates"]])
        verification_id = record["task_id"]
        verification = state["tasks"][verification_id]["attempts"][0]
        self.assertEqual("target_build", verification["command"]["payload"]["stage_id"])
        self.assertEqual(record["updates"][0]["result"]["command_id"],
                         verification["command"]["causation_id"])
        tool_result = json.loads((root / "cleanup-review-tool-result.json").read_text())
        self.assertIn("target_build", str(tool_result))

    def test_cleanup_failures_reach_consumers_without_retry_or_business_gate(self):
        request = MigrationRequest("cleanup-failure", "https://example.invalid/source.git",
            "1.20.1", "26.1.2", source_revision="a" * 40,
            budget=Budget(max_seconds=180, max_agent_assignments=100))
        root = self.base / "failed-run"
        handlers = registry()
        handlers["modport.research_cleanup"] = ReportStage("research_cleanup", status="failed")
        handlers["modport.code_cleanup"] = ReportStage("code_cleanup", status="failed")

        state = self._execute(request, root, handlers)
        self.assertEqual("succeeded", state["state"])
        self.assertEqual("unverified", state["application_state"]["acceptance_status"])
        research_attempt = state["tasks"]["research_cleanup"]["attempts"]
        code_attempt = state["tasks"]["code_cleanup"]["attempts"]
        self.assertEqual(1, len(research_attempt))
        self.assertEqual(1, len(code_attempt))
        self.assertEqual("failed", research_attempt[0]["result"]["value"]["status"])
        self.assertEqual("failed", code_attempt[0]["result"]["value"]["status"])

        planner = state["tasks"]["migration_plan"]["attempts"][0]["command"]["payload"]
        self.assertEqual("failed", planner["upstream_results"]["research_cleanup"]["status"])
        target = state["tasks"]["target_build"]["attempts"][0]["command"]["payload"]
        self.assertEqual("failed", target["upstream_results"]["code_cleanup"]["status"])
        self.assertNotIn("gate_handoff", state["tasks"])
        self.assertFalse(any(name.startswith("agent-rework.") for name in state["tasks"]))

    def test_repair_integrate_and_target_revision_continue_through_code_cleanup(self):
        request = MigrationRequest("cleanup-repair-route", "https://example.invalid/source.git",
            "1.20.1", "26.1.2", budget=Budget(max_agent_assignments=100))
        definition = compile_migration_workflow(request).to_dict()

        class RouteRecorder(MigrationOperations):
            def __init__(self):
                self.scheduled = []

            def _schedule(self, snapshot, header, app, stage, **kwargs):
                self.scheduled.append(stage)
                return []

        host = RouteRecorder()
        header = {"definition": definition, "request": request.to_dict(),
                  "run_dir": str(self.base / "route-run")}
        snapshot = {"run_id": "route-run", "tasks": {}}
        for predecessor in ("target_repair_integrate", "target_revise"):
            app = host._new_application()
            host._flowthrough_schedule_successor(snapshot, header, app, predecessor, "repair-result")
            self.assertEqual("code_cleanup", host.scheduled[-1])


if __name__ == "__main__":
    unittest.main()
