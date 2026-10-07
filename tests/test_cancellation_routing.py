"""A fresh CLI Runtime must leave active cancellation to its SDK owner."""
from contextlib import nullcontext
from dataclasses import replace
from pathlib import Path
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from dispatcher_sdk.orchestrator import Orchestrator, OrchestratorHost
from fixtures_modport import FixtureHandler, registry
import modport
from modport import Budget, MigrationOperations, MigrationRequest
from modport.contracts import OperationInput
from modport.evidence import atomic_json, file_digest
from modport.application_state_storage import hydrate_run_snapshot
from modport import kernel_runtime


class BlockingHandler:
    __execution_kernel_revision__ = "modport-test-blocking-coder-v1"

    def __call__(self, operation):
        marker = Path(operation.run_dir) / "blocking-coder-started"
        marker.write_text("active", encoding="utf-8")
        while True:
            time.sleep(0.05)


class ReleasableHandler:
    __execution_kernel_revision__ = "modport-test-releasable-coder-v1"

    def __call__(self, operation):
        root = Path(operation.run_dir)
        (root / "releasable-coder-started").write_text("active", encoding="utf-8")
        release = root / "release-releasable-coder"
        while not release.exists():
            time.sleep(0.01)
        return FixtureHandler()(operation)


class BlockingVerifier:
    __execution_kernel_revision__ = "modport-test-blocking-rework-verifier-v1"

    def __call__(self, operation):
        (Path(operation.run_dir) / "blocking-verifier-started").write_text(
            "active", encoding="utf-8")
        while True:
            time.sleep(0.05)


class PartialBlockingContractDraft:
    __execution_kernel_revision__ = "modport-test-partial-contract-draft-v1"

    def __call__(self, operation):
        root = Path(operation.run_dir)
        output = root / "baseline" / ".modport" / "functional-contract.json"
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text('{"partial": true}', encoding="utf-8")
        (root / "blocking-contract-draft-started").write_text("active", encoding="utf-8")
        while True:
            time.sleep(0.05)


class BlockingReworkChild:
    __execution_kernel_revision__ = "modport-test-blocking-rework-child-v1"

    def __init__(self, marker):
        self.marker = marker

    def __call__(self, operation):
        root = Path(operation.run_dir)
        (root / self.marker).write_text("active", encoding="utf-8")
        while True:
            time.sleep(0.05)


class BlockingSupervisedGoal:
    __execution_kernel_revision__ = "modport-test-blocking-supervisor-v1"

    def __call__(self, operation):
        from modport.supervised_goals import prepare

        prepared = prepare(operation)
        target = operation.payload["supervised_goal_targets"][0]
        goal = (Path(operation.run_dir) / prepared.options["workspace"] / "goals"
                / f"{target['key']}.md")
        goal.write_text("Partial supervisor edit.\n", encoding="utf-8")
        (Path(operation.run_dir) / "blocking-supervisor-started").write_text(
            "active", encoding="utf-8")
        while True:
            time.sleep(0.05)


class CancellationRoutingTests(unittest.TestCase):
    @staticmethod
    def create_run(temporary, handlers=None):
        root = Path(temporary) / "run"
        operations = MigrationOperations(
            handlers=registry() if handlers is None else handlers,
            isolation_mode="process",
        )
        run = operations.submit(
            MigrationRequest(
                "cancel-route", "https://example.invalid/mod.git", "1.20.1", "26.1.2",
                source_revision="a" * 40, budget=Budget(max_seconds=None),
            ),
            run_dir=root,
            run_id="cancel-route",
        )
        return root, operations, run

    def test_cli_cancel_reaps_coder_and_records_incomplete_failed_receipt(self):
        with tempfile.TemporaryDirectory() as temporary:
            handlers = registry()
            handlers["modport.coder"] = BlockingHandler()
            root, operations, run = self.create_run(temporary, handlers)
            (root / "workspaces" / "coder").mkdir(parents=True)

            with operations.session(root, run.run_id) as (_, header, runtime, sdk):
                command_id = "active-coder"
                operation = OperationInput(
                    run.run_id, "coder", "coder", command_id, str(root),
                    payload={"synthetic": "incomplete-coder-effect"},
                    options={"workspace": "workspaces/coder"},
                )
                command = runtime.command(
                    "modport.coder",
                    execution_id=command_id,
                    idempotency_key=command_id,
                    correlation_id=run.run_id,
                    timeout_seconds=60,
                    payload=operation.to_dict(),
                )
                sdk.apply_operations(
                    run.run_id,
                    command_id="start-active-coder",
                    expected_revision=sdk.get_run(run.run_id)["revision"],
                    operations=[
                        {"kind": "add_task", "task_id": "coder", "command": command.to_dict()},
                        {"kind": "dispatch", "task_id": "coder"},
                    ],
                )
                host = OrchestratorHost(sdk).start()
                try:
                    marker = root / "blocking-coder-started"
                    deadline = time.monotonic() + 10
                    while not marker.exists() and time.monotonic() < deadline:
                        time.sleep(0.02)
                    self.assertTrue(marker.exists(), "owner Runtime never entered the coder handler")

                    cancelled = operations.cancel(root, run.run_id)
                    self.assertEqual(run.run_id, cancelled.run_id)

                    report = sdk.inspect_cancellation(run.run_id, task_id="coder")
                    self.assertEqual(1, len(report.executions))
                    evidence = report.executions[0]
                    self.assertEqual(command_id, evidence.execution_id)
                    self.assertEqual("confirmed", evidence.command_delivered.status)
                    self.assertEqual("confirmed", evidence.local_process_tree_reaped.status)
                    # SDK confirms the tracked effect's recovery decision;
                    # the receipt still leaves workspace/external side effects unknown.
                    self.assertEqual("confirmed", evidence.external_outcome.status)
                    self.assertEqual("recorded_effects_only",
                                     evidence.external_outcome.details["coverage"])
                    self.assertEqual("cancelled", evidence.execution_state)
                    self.assertEqual("cancelled", cancelled.status)
                    receipt = json.loads((root / "artifacts" / "executions"
                                          / command_id / "receipt.json").read_text())
                    response = receipt["response"]
                    self.assertEqual("failed", response["status"])
                    self.assertEqual("coder_interrupted", response["error_code"])
                    self.assertFalse(response["outputs"]["artifacts_complete"])
                    self.assertEqual("unknown_unaccepted", response["outputs"]["workspace_changes"])
                    self.assertEqual("unknown", response["outputs"]["external_outcome"])
                    with self.assertRaisesRegex(ValueError, "forbidden after user cancellation"):
                        operations.recover(root, run.run_id, resume_native_goals=True)
                finally:
                    host.stop(timeout=5)

            self.assertTrue(
                (root / ".modport" / "execution-cancellation.sqlite3").is_file()
            )

    def test_recover_settles_reaped_contract_draft_as_unaccepted(self):
        with tempfile.TemporaryDirectory() as temporary:
            handlers = registry()
            handlers["modport.contract_draft"] = PartialBlockingContractDraft()
            root, operations, run = self.create_run(temporary, handlers)

            with operations.session(root, run.run_id) as (_, _, runtime, sdk):
                command_id = "active-contract-draft"
                operation = OperationInput(
                    run.run_id, "contract_draft", "contract_draft", command_id,
                    str(root), payload={"synthetic": "partial-contract"},
                )
                command = runtime.command(
                    "modport.contract_draft", execution_id=command_id,
                    idempotency_key=command_id, correlation_id=run.run_id,
                    timeout_seconds=60, payload=operation.to_dict(),
                )
                sdk.apply_operations(
                    run.run_id, command_id="start-active-contract-draft",
                    expected_revision=sdk.get_run(run.run_id)["revision"],
                    operations=[
                        {"kind": "add_task", "task_id": "contract_draft",
                         "command": command.to_dict()},
                        {"kind": "dispatch", "task_id": "contract_draft"},
                    ],
                )
                host = OrchestratorHost(sdk).start()
                try:
                    marker = root / "blocking-contract-draft-started"
                    deadline = time.monotonic() + 10
                    while not marker.exists() and time.monotonic() < deadline:
                        time.sleep(0.02)
                    self.assertTrue(marker.exists(), "owner Runtime never entered the draft handler")
                    operations.cancel(root, run.run_id)
                finally:
                    host.stop(timeout=5)

            recovered = operations.recover(root, run.run_id)
            self.assertEqual("cancelled", recovered.status)
            receipt_path = (root / "artifacts" / "executions" / command_id
                            / "receipt.json")
            receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
            response = receipt["response"]
            self.assertEqual("failed", response["status"])
            self.assertEqual("contract_draft_interrupted", response["error_code"])
            self.assertFalse(response["outputs"]["artifacts_complete"])
            self.assertEqual("unknown", response["outputs"]["external_outcome"])
            self.assertEqual("unverified", response["outputs"]["acceptance_status"])
            self.assertTrue(response["outputs"]["partial_outputs_unaccepted"])
            self.assertTrue((root / "baseline" / ".modport"
                             / "functional-contract.json").is_file())
            with operations.session(root, run.run_id) as (_, _, _, sdk):
                self.assertEqual([], sdk.inspect_recoveries(run.run_id))

    def test_cancelled_review_request_waits_for_child_then_fails_unaccepted(self):
        operations = MigrationOperations()
        app = {
            "user_cancelled": True,
            "stop_reason": "user_cancelled",
            "review_rework": {"requests": {"reviewer/request": {
                "state": "running", "reviewer_execution_id": "reviewer-execution",
                "request_id": "request", "task_id": "agent-rework.request",
                "updates": [],
            }}},
        }
        snapshot = {"tasks": {
            "contract_review": {"attempts": [{"state": "cancelled",
                "command": {"execution_id": "reviewer-execution"}}]},
            "agent-rework.request": {"attempts": [{"state": "recovery_required",
                "command": {"execution_id": "child-execution"}}]},
        }}
        operations._settle_user_cancelled_review_requests(snapshot, app)
        record = app["review_rework"]["requests"]["reviewer/request"]
        self.assertEqual("running", record["state"])

        snapshot["tasks"]["agent-rework.request"]["attempts"][-1]["state"] = "failed"
        operations._settle_user_cancelled_review_requests(snapshot, app)
        self.assertEqual("failed", record["state"])
        self.assertEqual("failed", record["cancellation_settlement"]["child_execution_state"])
        self.assertFalse(record["cancellation_settlement"]["accepted"])

    def test_user_cancel_settles_review_children_before_parent_review(self):
        from modport.rework_tools import session_directory

        for stage, suffix in (("agent_rework", ""), ("contract_verify", ".verify")):
            with self.subTest(stage=stage), tempfile.TemporaryDirectory() as temporary:
                reviewer_id = "cancelled-reviewer"
                request_id = f"request-{stage}"
                task_id = f"agent-rework.{request_id}{suffix}"
                child_id = f"cancel-run:{task_id}:1"
                marker = f"blocking-{stage}-started"
                handlers = registry()
                handlers["modport.contract_review"] = BlockingHandler()
                handlers[f"modport.{stage}"] = BlockingReworkChild(marker)
                root, operations, run = self.create_run(temporary, handlers)

                session = session_directory(root, reviewer_id)
                request_path = session / "requests" / f"{request_id}.json"
                atomic_json(request_path, {
                    "run_id": run.run_id,
                    "reviewer_execution_id": reviewer_id,
                    "request_id": request_id,
                    "task_id": task_id,
                })

                with operations.session(root, run.run_id) as (_, _, runtime, sdk):
                    reviewer = OperationInput(
                        run.run_id, "contract_review", "contract_review", reviewer_id,
                        str(root), payload={"review_rework_targets": [
                            {"target_agent": "agent_rework"}]},
                        options={"workflow_version": 26},
                    )
                    reviewer_command = runtime.command(
                        "modport.contract_review", execution_id=reviewer_id,
                        idempotency_key=reviewer_id, correlation_id=run.run_id,
                        timeout_seconds=60, payload=reviewer.to_dict(),
                    )
                    child = OperationInput(
                        run.run_id, task_id, stage, child_id, str(root),
                        payload={"reviewer_rework": {"request_id": request_id},
                                 "reviewer_execution_id": reviewer_id,
                                 "reviewer_workspace": "worktree"},
                        options={"workflow_version": 26},
                    )
                    child_command = runtime.command(
                        f"modport.{stage}", execution_id=child_id,
                        idempotency_key=child_id, correlation_id=run.run_id,
                        timeout_seconds=60, payload=child.to_dict(),
                    )
                    app = operations._new_application()
                    app["review_rework"] = {"requests": {f"{reviewer_id}/{request_id}": {
                        "state": "running", "request_id": request_id,
                        "reviewer_execution_id": reviewer_id, "task_id": task_id,
                        "target_stage": stage, "followup_stage": stage,
                        "target_agent": stage, "reviewer_stage": "contract_review",
                        "sequence": 1, "updates": [], "downstream_toolcall": True,
                    }}, "latest_targets": {}, "sequence": 1}
                    sdk.apply_operations(
                        run.run_id, command_id=f"start-cancelled-{stage}-rework",
                        expected_revision=sdk.get_run(run.run_id)["revision"],
                        operations=[
                            {"kind": "add_task", "task_id": "contract_review",
                             "command": reviewer_command.to_dict()},
                            {"kind": "dispatch", "task_id": "contract_review"},
                            {"kind": "add_task", "task_id": task_id,
                             "command": child_command.to_dict()},
                            {"kind": "dispatch", "task_id": task_id},
                        ], application_state=app,
                    )
                    host = OrchestratorHost(sdk).start()
                    try:
                        deadline = time.monotonic() + 10
                        while (not (root / marker).exists()
                               or not (root / "blocking-coder-started").exists()) and time.monotonic() < deadline:
                            time.sleep(0.02)
                        self.assertTrue((root / marker).exists(), "rework child handler was not entered")
                        self.assertTrue((root / "blocking-coder-started").exists(),
                                        "reviewer handler was not entered")
                        cancelled = operations.cancel(root, run.run_id)
                        self.assertEqual("waiting", cancelled.status)
                        self.assertTrue(cancelled.snapshot["application_state"]["user_cancelled"])
                    finally:
                        host.stop(timeout=5)

                recovered = operations.recover(root, run.run_id)
                self.assertEqual("cancelled", recovered.status)
                snapshot = recovered.snapshot
                for execution_id, expected_code in (
                        (child_id, f"{stage}_interrupted"),
                        (reviewer_id, "contract_review_interrupted")):
                    receipt = json.loads((root / "artifacts" / "executions"
                        / execution_id / "receipt.json").read_text(encoding="utf-8"))
                    response = receipt["response"]
                    self.assertEqual("failed", response["status"])
                    self.assertEqual(expected_code, response["error_code"])
                    self.assertEqual("unknown", response["outputs"]["external_outcome"])
                    self.assertEqual("unverified", response["outputs"]["acceptance_status"])
                record = snapshot["application_state"]["review_rework"]["requests"][
                    f"{reviewer_id}/{request_id}"]
                self.assertEqual("failed", record["state"])
                self.assertFalse(record["cancellation_settlement"]["accepted"])

    def test_cancelled_supervisor_edit_is_published_as_non_applicable(self):
        from modport.evidence import verified_path
        from modport.supervised_goals import target_key

        with tempfile.TemporaryDirectory() as temporary:
            handlers = registry()
            handlers["modport.supervisor"] = BlockingSupervisedGoal()
            root, operations, run = self.create_run(temporary, handlers)
            plan_path = root / "artifacts" / "supervisor-plan.json"
            atomic_json(plan_path, {"tasks": []})
            plan_ref = {"path": plan_path.relative_to(root).as_posix(),
                        "sha256": file_digest(plan_path),
                        "media_type": "application/json"}
            task = {"id": "supervisor-target", "objective": "Preserve behavior."}
            target = {"key": target_key(task, plan_ref), "task": task,
                      "plan_ref": plan_ref, "source_execution_id": None,
                      "source_workspace": "worktree"}
            (root / "worktree").mkdir()

            with operations.session(root, run.run_id) as (_, _, runtime, sdk):
                operation = OperationInput(
                    run.run_id, "supervisor.window.10", "supervisor",
                    "active-supervisor", str(root),
                    payload={"supervised_goal_targets": [target]},
                    options={"workflow_version": 26},
                )
                command = runtime.command(
                    "modport.supervisor", execution_id=operation.command_id,
                    idempotency_key=operation.command_id,
                    correlation_id=run.run_id, timeout_seconds=60,
                    payload=operation.to_dict(),
                )
                sdk.apply_operations(
                    run.run_id, command_id="start-active-supervisor",
                    expected_revision=sdk.get_run(run.run_id)["revision"],
                    operations=[
                        {"kind": "add_task", "task_id": operation.task_id,
                         "command": command.to_dict()},
                        {"kind": "dispatch", "task_id": operation.task_id},
                    ],
                )
                host = OrchestratorHost(sdk).start()
                try:
                    marker = root / "blocking-supervisor-started"
                    deadline = time.monotonic() + 10
                    while not marker.exists() and time.monotonic() < deadline:
                        time.sleep(0.02)
                    self.assertTrue(marker.exists(), "supervisor handler was not entered")
                    cancelled = operations.cancel(root, run.run_id)
                    self.assertEqual("waiting", cancelled.status)
                finally:
                    host.stop(timeout=5)

            recovered = operations.recover(root, run.run_id)
            self.assertEqual("cancelled", recovered.status)
            receipt = json.loads((root / "artifacts" / "executions"
                / operation.command_id / "receipt.json").read_text(encoding="utf-8"))
            response = receipt["response"]
            self.assertEqual("failed", response["status"])
            self.assertEqual("supervisor_interrupted", response["error_code"])
            self.assertEqual("unknown", response["outputs"]["external_outcome"])
            self.assertEqual("unverified", response["outputs"]["acceptance_status"])
            revisions = response["outputs"]["supervised_goal_revisions"]
            self.assertEqual(1, len(revisions))
            revision = json.loads(verified_path(root, revisions[0]["revision_ref"])
                                  .read_text(encoding="utf-8"))
            self.assertFalse(revision["applicable"])

    def test_closed_rework_verifier_recovery_needs_cancellation_proof(self):
        for stage, source_stage, reviewer_stage in (
                ("contract_verify", "contract_draft", "contract_review"),
                ("target_build", "coder", "code_review")):
            with self.subTest(stage=stage):
                self._check_closed_rework_verifier_recovery(
                    stage, source_stage, reviewer_stage)

    def _check_closed_rework_verifier_recovery(self, stage, source_stage,
                                                reviewer_stage):
        retain = os.environ.get("MODPORT_RETAIN_CANCEL_PROBE_ROOT")
        if retain:
            retained_root = Path(retain).resolve()
            retained_root.mkdir(parents=True, exist_ok=True)
            temporary_context = nullcontext(tempfile.mkdtemp(
                prefix=f"{stage}-", dir=retained_root))
        else:
            temporary_context = tempfile.TemporaryDirectory()
        with temporary_context as temporary:
            handlers = registry()
            handlers[f"modport.{stage}"] = BlockingVerifier()
            root, operations, run = self.create_run(temporary, handlers)
            frozen_header_sha256 = file_digest(root / "run.json")
            reviewer_id = "reviewer-execution"
            session = root / "artifacts" / "rework-tools" / reviewer_id
            atomic_json(session / "session.json", {
                "run_id": run.run_id, "reviewer_execution_id": reviewer_id,
                "deadline_epoch": time.time() + 60, "drain_pending_on_eof": True,
                "targets": [{"target_agent": source_stage,
                             "description": "Rework author"}],
            })
            environment = os.environ.copy()
            environment["PYTHONPATH"] = str(Path(modport.__file__).resolve().parent.parent)
            mcp = subprocess.Popen(
                [sys.executable, "-m", "modport.rework_mcp", "--session",
                 str(session / "session.json")], stdin=subprocess.PIPE,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                encoding="utf-8", env=environment,
            )
            try:
                assert mcp.stdin is not None
                mcp.stdin.write(json.dumps({"jsonrpc": "2.0", "id": 41,
                    "method": "tools/call", "params": {"name": "request_rework",
                        "arguments": {"target_agent": source_stage,
                                      "instructions": "Verify the revision"}}}) + "\n")
                mcp.stdin.flush()
                deadline = time.monotonic() + 5
                request_path = None
                while time.monotonic() < deadline:
                    candidates = list((session / "requests").glob("*.json"))
                    if candidates:
                        request_path = candidates[0]
                        break
                    time.sleep(0.02)
                self.assertIsNotNone(request_path, "MCP did not publish the rework request")
                request = json.loads(request_path.read_text(encoding="utf-8"))
                request_id = request["request_id"]
                task_id = f"agent-rework.{request_id}.verify"
                command_id = f"{run.run_id}:{task_id}:1"
                marker = session / "requests" / f"{request_id}.cancel.json"

                with operations.session(root, run.run_id) as (_, header, runtime, sdk):
                    reviewer_operation = OperationInput(
                        run.run_id, reviewer_stage, reviewer_stage,
                        reviewer_id, str(root), options={"workflow_version": 25},
                    )
                    reviewer_command = runtime.command(
                        f"modport.{reviewer_stage}", execution_id=reviewer_id,
                        idempotency_key=reviewer_id, correlation_id=run.run_id,
                        timeout_seconds=60, payload=reviewer_operation.to_dict(),
                    )
                    operation = OperationInput(
                        run.run_id, task_id, stage, command_id, str(root),
                        payload={"reviewer_rework": {"request_id": request_id,
                            "reviewer_execution_id": reviewer_id}},
                        options={"workflow_version": 25},
                    )
                    command = runtime.command(
                        f"modport.{stage}", execution_id=command_id,
                        idempotency_key=command_id, correlation_id=run.run_id,
                        timeout_seconds=60, payload=operation.to_dict(),
                    )
                    app = operations._new_application()
                    app["review_rework"] = {"requests": {f"{reviewer_id}/{request_id}": {
                        "state": "running", "request_id": request_id,
                        "reviewer_execution_id": reviewer_id, "task_id": task_id,
                        "target_stage": source_stage, "followup_stage": stage,
                        "target_agent": source_stage, "reviewer_stage": reviewer_stage,
                        "sequence": 1, "updates": [], "downstream_toolcall": True,
                        "instructions": "Verify the revision", "text": "",
                    }}, "latest_targets": {}, "sequence": 1}
                    sdk.apply_operations(
                        run.run_id, command_id="start-rework-verifier",
                        expected_revision=sdk.get_run(run.run_id)["revision"],
                        operations=[
                            {"kind": "add_task", "task_id": reviewer_stage,
                             "command": reviewer_command.to_dict()},
                            {"kind": "dispatch", "task_id": reviewer_stage},
                            {"kind": "add_task", "task_id": task_id, "command": command.to_dict()},
                            {"kind": "dispatch", "task_id": task_id},
                        ], application_state=app,
                    )
                    host = OrchestratorHost(sdk).start()
                    try:
                        started = root / "blocking-verifier-started"
                        deadline = time.monotonic() + 10
                        while not started.exists() and time.monotonic() < deadline:
                            time.sleep(0.02)
                        self.assertTrue(started.exists(), "verifier handler was not entered")
                        mcp.stdin.write(json.dumps({"jsonrpc": "2.0",
                            "method": "notifications/cancelled", "params": {
                                "requestId": 41, "reason": "reviewer tool call closed"}}) + "\n")
                        mcp.stdin.flush()
                        deadline = time.monotonic() + 5
                        while not marker.is_file() and time.monotonic() < deadline:
                            time.sleep(0.02)
                        self.assertTrue(marker.is_file(), "MCP did not publish cancellation")
                        sdk.sync()
                        operations.tick(sdk, header)
                        updated = hydrate_run_snapshot(root, sdk.get_run(run.run_id))[
                            "application_state"]
                        self.assertIn(command_id, updated["cancel_sent"])
                        deadline = time.monotonic() + 10
                        while time.monotonic() < deadline:
                            sdk.sync()
                            report = sdk.inspect_cancellation(run.run_id, execution_id=command_id)
                            entry = report.executions[0]
                            if (entry.execution_state == "recovery_required"
                                    and entry.request_committed.status == "confirmed"
                                    and entry.command_delivered.status == "confirmed"
                                    and entry.execution_authority_revoked.status == "confirmed"
                                    and entry.local_process_tree_reaped.status == "confirmed"
                                    and entry.cleanup.status == "confirmed"):
                                break
                            time.sleep(0.02)
                        self.assertEqual("recovery_required", entry.execution_state)
                        self.assertEqual("confirmed", entry.request_committed.status)
                        self.assertEqual("confirmed", entry.command_delivered.status)
                        self.assertEqual("confirmed", entry.execution_authority_revoked.status)
                        self.assertEqual("confirmed", entry.local_process_tree_reaped.status)
                        self.assertEqual("confirmed", entry.cleanup.status)
                        self.assertEqual("unknown", entry.external_outcome.status)
                        self.assertFalse(entry.issues)
                        self.assertFalse(entry.effects_truncated)
                        self.assertFalse(entry.receipts_truncated)
                        self.assertTrue(any(row["effect_id"] == f"modport:{command_id}"
                                            and row["state"] == "indeterminate"
                                            for row in entry.effects))
                    finally:
                        host.stop(timeout=5)

                marker_bytes = marker.read_bytes()
                marker.unlink()
                with self.assertRaisesRegex(ValueError, "no complete stage receipt"):
                    operations.recover(root, run.run_id)
                marker.write_bytes(marker_bytes)
                inspect_cancellation = Orchestrator.inspect_cancellation

                def unconfirmed_cleanup(sdk, *args, **kwargs):
                    report = inspect_cancellation(sdk, *args, **kwargs)
                    return replace(report, executions=tuple(replace(
                        entry, cleanup=replace(entry.cleanup, status="pending"))
                        for entry in report.executions))

                with patch.object(Orchestrator, "inspect_cancellation",
                                  unconfirmed_cleanup):
                    with self.assertRaisesRegex(ValueError, "no complete stage receipt"):
                        operations.recover(root, run.run_id)
                self.assertFalse((root / "artifacts" / "executions"
                                  / command_id / "receipt.json").exists())
                write_json = kernel_runtime.atomic_json

                def crash_after_note(path, value):
                    if Path(path).name == "receipt.json":
                        raise RuntimeError("crash after cancellation note")
                    return write_json(path, value)

                with patch("modport.kernel_runtime.atomic_json", side_effect=crash_after_note):
                    with self.assertRaisesRegex(RuntimeError, "crash after cancellation note"):
                        operations.recover(root, run.run_id)
                note = root / "artifacts" / "executions" / command_id / "interrupted-rework-verification.json"
                self.assertTrue(note.is_file())
                first_receipts = json.loads(note.read_text())["cancellation_receipt_ids"]
                with operations.session(root, run.run_id) as (_, _, _, sdk):
                    sdk.apply_operations(
                        run.run_id, command_id="repeat-reviewer-close",
                        expected_revision=sdk.get_run(run.run_id)["revision"],
                        operations=[{"kind": "cancel", "task_id": task_id,
                                     "reason": "reviewer tool call closed"}],
                    )
                    sdk.flush()
                    deadline = time.monotonic() + 5
                    while time.monotonic() < deadline:
                        report = sdk.inspect_cancellation(run.run_id, execution_id=command_id)
                        if len(report.executions[0].receipt_ids) > len(first_receipts):
                            break
                        time.sleep(0.02)
                    self.assertGreater(len(report.executions[0].receipt_ids),
                                       len(first_receipts))
                recovered = operations.recover(root, run.run_id)
                self.assertEqual(first_receipts,
                    json.loads(note.read_text())["cancellation_receipt_ids"])
                receipt = json.loads((root / "artifacts" / "executions"
                                      / command_id / "receipt.json").read_text())
                response = receipt["response"]
                self.assertEqual("failed", response["status"])
                self.assertEqual("rework_verification_interrupted", response["error_code"])
                self.assertFalse(response["outputs"]["artifacts_complete"])
                self.assertEqual("unknown", response["outputs"]["external_outcome"])
                self.assertEqual("unverified", response["outputs"]["acceptance_status"])
                attempt = recovered.snapshot["tasks"][task_id]["attempts"]
                self.assertEqual(1, len(attempt))
                self.assertEqual("cancelled", attempt[0]["state"])
                self.assertEqual(1, len(operations.recover(root, run.run_id).snapshot
                                        ["tasks"][task_id]["attempts"]))
                self.assertEqual(frozen_header_sha256, file_digest(root / "run.json"))
                if retain:
                    atomic_json(root / "validation-summary.json", {
                        "schema": "modport.f08-rework-verifier-cancel/v1",
                        "stage": stage, "run_id": run.run_id,
                        "run_dir": str(root), "task_id": task_id,
                        "execution_id": command_id,
                        "package_source": modport.__file__,
                        "frozen_header_sha256": frozen_header_sha256,
                        "cancellation": {
                            "execution_state_before_recovery": entry.execution_state,
                            "effect_state_before_recovery": "indeterminate",
                            "request": entry.request_committed.status,
                            "delivery": entry.command_delivered.status,
                            "authority_revoked": entry.execution_authority_revoked.status,
                            "process_reaped": entry.local_process_tree_reaped.status,
                            "cleanup": entry.cleanup.status,
                            "external_outcome": entry.external_outcome.status,
                            "first_receipt_ids": first_receipts,
                            "receipt_count_after_duplicate_cancel": len(
                                report.executions[0].receipt_ids),
                        },
                        "result": {"attempt_state": attempt[0]["state"],
                                   "attempt_count": len(attempt),
                                   "receipt_status": response["status"],
                                   "receipt_error_code": response["error_code"],
                                   "artifacts_complete": response["outputs"]["artifacts_complete"],
                                   "external_outcome": response["outputs"]["external_outcome"],
                                   "acceptance_status": response["outputs"]["acceptance_status"]},
                        "negative_controls": ["missing_close_marker",
                                              "unconfirmed_sdk_cleanup"],
                        "note_only_crash_recovered": True,
                    })
            finally:
                if mcp.stdin is not None and not mcp.stdin.closed:
                    mcp.stdin.close()
                try:
                    mcp.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    mcp.kill()
                    mcp.wait(timeout=3)
                if mcp.stdout is not None:
                    mcp.stdout.close()
                if mcp.stderr is not None:
                    mcp.stderr.close()

    def test_cancel_retries_until_its_stop_decision_is_durable(self):
        with tempfile.TemporaryDirectory() as temporary:
            root, operations, run = self.create_run(temporary)
            with operations.session(root, run.run_id) as (_, _, runtime, sdk):
                command_id = "queued-coder"
                operation = OperationInput(
                    run.run_id, "coder", "coder", command_id, str(root),
                    payload={"synthetic": "never-started"},
                )
                command = runtime.command(
                    "modport.coder",
                    execution_id=command_id,
                    idempotency_key=command_id,
                    correlation_id=run.run_id,
                    timeout_seconds=60,
                    payload=operation.to_dict(),
                )
                sdk.apply_operations(
                    run.run_id,
                    command_id="queue-coder",
                    expected_revision=sdk.get_run(run.run_id)["revision"],
                    operations=[
                        {"kind": "add_task", "task_id": "coder", "command": command.to_dict()},
                        {"kind": "dispatch", "task_id": "coder"},
                    ],
                )
                sdk.flush()
                self.assertEqual("queued", sdk.get_run(run.run_id)["tasks"]["coder"]["attempts"][0]["state"])

            real_tick = operations.tick
            tick_calls = 0

            def conflict_once(sdk, header, *, stop_reason=None, stop_state="cancelled"):
                nonlocal tick_calls
                tick_calls += 1
                if tick_calls == 1:
                    # Model another controller winning the first revision CAS.
                    return sdk.get_run(run.run_id)
                return real_tick(sdk, header, stop_reason=stop_reason, stop_state=stop_state)

            with patch.object(operations, "tick", side_effect=conflict_once):
                with patch("modport.operations.CANCEL_SETTLE_TIMEOUT_SECONDS", 0.05):
                    with self.assertRaisesRegex(TimeoutError, "did not confirm cancellation cleanup"):
                        operations.cancel(root, run.run_id)

            self.assertGreaterEqual(tick_calls, 2)
            with operations.session(root, run.run_id) as (_, _, runtime, sdk):
                snapshot = sdk.get_run(run.run_id)
                self.assertTrue(snapshot["application_state"]["user_cancelled"])
                self.assertEqual("queued", snapshot["tasks"]["coder"]["attempts"][0]["state"])
                pending_cancels = [message for message in sdk.delivery_messages(
                    execution_ids=["queued-coder"], pending_only=True)
                    if message["kind"] == "cancel"]
                self.assertEqual(1, len(pending_cancels))
                self.assertEqual("pending", pending_cancels[0]["state"])

            with self.assertRaisesRegex(ValueError, "forbidden after user cancellation"):
                operations.recover(root, run.run_id, resume_native_goals=True)

    def test_cancel_routes_preexisting_recovery_required_attempt_to_owner_cleanup(self):
        with tempfile.TemporaryDirectory() as temporary:
            handlers = registry()
            handlers["modport.coder"] = BlockingHandler()
            root, operations, run = self.create_run(temporary, handlers)
            with operations.session(root, run.run_id) as (_, _, runtime, sdk):
                command_id = "recovery-coder"
                operation = OperationInput(
                    run.run_id, "coder", "coder", command_id, str(root),
                    payload={"synthetic": "incomplete-coder-effect"},
                )
                command = runtime.command(
                    "modport.coder", execution_id=command_id,
                    idempotency_key=command_id, correlation_id=run.run_id,
                    timeout_seconds=60, payload=operation.to_dict(),
                )
                sdk.apply_operations(
                    run.run_id, command_id="start-recovery-coder",
                    expected_revision=sdk.get_run(run.run_id)["revision"],
                    operations=[
                        {"kind": "add_task", "task_id": "coder", "command": command.to_dict()},
                        {"kind": "dispatch", "task_id": "coder"},
                    ],
                )
                host = OrchestratorHost(sdk).start()
                try:
                    marker = root / "blocking-coder-started"
                    deadline = time.monotonic() + 10
                    while not marker.exists() and time.monotonic() < deadline:
                        time.sleep(0.02)
                    self.assertTrue(marker.exists(), "owner Runtime never entered the coder handler")

                    active = runtime.kernel.get(command_id)
                    self.assertIsNotNone(active.lease)
                    runtime.kernel.require_effect_recovery(
                        active.lease, "modport:" + command_id)
                    sdk.sync()
                    self.assertEqual(
                        "recovery_required",
                        sdk.get_run(run.run_id)["tasks"]["coder"]["attempts"][0]["state"],
                    )

                    operations.cancel(root, run.run_id)

                    report = sdk.inspect_cancellation(run.run_id, execution_id=command_id)
                    evidence = report.executions[0]
                    self.assertEqual("confirmed", evidence.command_delivered.status)
                    self.assertEqual("confirmed", evidence.local_process_tree_reaped.status)
                    self.assertEqual("unknown", evidence.external_outcome.status)
                    self.assertEqual("recovery_required", evidence.execution_state)
                    with self.assertRaisesRegex(ValueError, "forbidden after user cancellation"):
                        operations.recover(root, run.run_id, resume_native_goals=True)
                finally:
                    host.stop(timeout=5)

    def test_owner_completion_wins_cancel_without_waiting_for_missing_cancel_receipt(self):
        with tempfile.TemporaryDirectory() as temporary:
            handlers = registry()
            handlers["modport.coder"] = ReleasableHandler()
            root, operations, run = self.create_run(temporary, handlers)
            with operations.session(root, run.run_id) as (_, _, runtime, sdk):
                command_id = "finish-during-cancel"
                operation = OperationInput(
                    run.run_id, "coder", "coder", command_id, str(root),
                    payload={"synthetic": "completed-before-cancel-delivery"},
                )
                command = runtime.command(
                    "modport.coder", execution_id=command_id,
                    idempotency_key=command_id, correlation_id=run.run_id,
                    timeout_seconds=60, payload=operation.to_dict(),
                )
                sdk.apply_operations(
                    run.run_id, command_id="start-finish-during-cancel",
                    expected_revision=sdk.get_run(run.run_id)["revision"],
                    operations=[
                        {"kind": "add_task", "task_id": "coder", "command": command.to_dict()},
                        {"kind": "dispatch", "task_id": "coder"},
                    ],
                )
                sdk.flush()
                allow_cancel_delivery = threading.Event()
                flush = sdk.flush

                def gated_flush(*args, **kwargs):
                    if not allow_cancel_delivery.is_set():
                        return 0
                    return flush(*args, **kwargs)

                with patch.object(sdk, "flush", side_effect=gated_flush):
                    host = OrchestratorHost(sdk).start()
                    cancel_result = {}
                    try:
                        marker = root / "releasable-coder-started"
                        deadline = time.monotonic() + 10
                        while not marker.exists() and time.monotonic() < deadline:
                            time.sleep(0.02)
                        self.assertTrue(marker.exists(), "owner Runtime never entered the coder handler")
                        thread = threading.Thread(
                            target=lambda: self._capture_cancel_result(
                                cancel_result, operations, root, run.run_id),
                            daemon=True,
                        )
                        thread.start()
                        deadline = time.monotonic() + 10
                        pending = []
                        while time.monotonic() < deadline:
                            pending = [message for message in sdk.delivery_messages(
                                execution_ids=[command_id], pending_only=True)
                                if message["kind"] == "cancel"]
                            if pending:
                                break
                            time.sleep(0.02)
                        self.assertTrue(pending, "cancel intent was not recorded")
                        (root / "release-releasable-coder").write_text("release", encoding="utf-8")
                        thread.join(timeout=5)
                        self.assertFalse(thread.is_alive(), "cancel waited for a receipt after owner completion")
                        self.assertNotIn("error", cancel_result)
                        self.assertEqual("cancelled", sdk.get_run(run.run_id)["state"])
                    finally:
                        (root / "release-releasable-coder").touch()
                        allow_cancel_delivery.set()
                        host.stop(timeout=5)

    @staticmethod
    def _capture_cancel_result(result, operations, root, run_id):
        try:
            result["run"] = operations.cancel(root, run_id)
        except BaseException as error:
            result["error"] = error


if __name__ == "__main__":
    unittest.main()
