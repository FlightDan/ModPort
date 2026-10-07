"""Explicit target continuation through current public SDK execution segments."""
from copy import deepcopy
from dataclasses import dataclass
import json
from pathlib import Path
import tempfile
import time
import unittest

from dispatcher_sdk.orchestrator import Orchestrator, OrchestratorHost, Operations

from modport.application_state_storage import hydrate_run_snapshot
from modport.continuation import continue_from_planner, prepare_application
from modport.contracts import OperationResult
from modport.evidence import atomic_json
from modport.kernel_runtime import open_runtime
from modport.models import Budget, MigrationRequest
from modport.operations import MigrationOperations
from modport.sdk_compat import inspect_runtime
from modport.workflow import WORKFLOW_VERSION, compile_migration_workflow


def target_contract():
    return {"behaviors": [{"id": "behavior", "assertion_contracts": [{
        "assertion_id": "assertion", "test_ids": ["case"]}]}],
        "test_evidence": {"case": {"path": "runtime.json", "evidence_kind": "runtime"}}}


@dataclass
class TargetContinuationWitness:
    target_passes: bool = True
    __execution_kernel_revision__ = "target-continuation-witness-v1"

    def __call__(self, command):
        root = Path(command.run_dir)
        directory = root / "artifacts" / "witness" / command.command_id
        directory.mkdir(parents=True, exist_ok=True)
        atomic_json(directory / "input.json", command.to_dict())
        status, error, outputs = "completed", None, {}
        if command.stage_id == "target_contract_freeze":
            path = directory / "lock.json"
            atomic_json(path, {"contract": target_contract(), "uncovered_assertion_ids": []})
            outputs = {"artifact_refs": {"functional_contract_lock": {
                "path": path.relative_to(root).as_posix(), "media_type": "application/json"}}}
        elif command.stage_id == "artifact_test_execute":
            outcome = "passed" if self.target_passes else "skipped"
            outputs = {"process_executed": True,
                "case_results": {"case": {"status": outcome, "test_outcome": outcome}},
                "assertion_results": {"assertion": {"status": outcome}},
                "evidence_records": {"case": {"path": "runtime.json", "evidence_kind": "runtime"}}}
        elif command.stage_id == "artifact_test_design":
            # Stop the bounded fixture after witnessing the actual repair input.
            if not command.command_id.startswith("original:"):
                status, error = "failed", "artifact_candidate_changed"
        elif command.stage_id == "artifact_test_report":
            path = directory / "report.json"
            atomic_json(path, {"acceptance_status": "unverified"})
            outputs = {"artifact_refs": {"artifact_verification_report": {
                "path": path.relative_to(root).as_posix(), "media_type": "application/json"}}}
            if command.command_id.startswith("original:"):
                status, error = "failed", "required_behavior_unverified"
        return OperationResult(status, command.run_id, command.task_id,
            command.stage_id, command.command_id, outputs=outputs, error_code=error)


class TargetContinuationTests(unittest.TestCase):
    def fixture(self, root):
        request = MigrationRequest("probe", "https://example.invalid/mod.git", "1.20.1", "1.21.1",
            workflow_mode="artifact_verification",
            budget=Budget(max_seconds=3600, max_agent_assignments=31))
        definition = compile_migration_workflow(request).to_dict()
        app = MigrationOperations._new_application()
        for stage in ("source", "environment", "behavior_freeze", "artifact_test_design"):
            outputs = {}
            if stage == "behavior_freeze":
                path = root / "artifacts" / "requirements.json"
                atomic_json(path, {"source_assumption": "user_confirmed_functional"})
                outputs = {"artifact_refs": {"behavior_requirements": {
                    "path": path.relative_to(root).as_posix(), "media_type": "application/json"}}}
            app["effective"][stage] = OperationResult("completed", "original", stage, stage,
                "original:" + stage + ":1", outputs=outputs).to_dict()
        for stage in ("target_contract_freeze", "artifact_test_execute", "artifact_test_report"):
            app["effective"][stage] = OperationResult("failed", "original", stage, stage,
                "original:" + stage + ":1", error_code="required_behavior_unverified").to_dict()
        app.update(agent_assignments=31, artifact_required_failure="agent_assignment_budget_exhausted",
            terminal_reason="agent_assignment_budget_exhausted", execution_status="finished",
            required_behavior_status="failed", required_behavior_assessments={"target": {
                "status": "failed", "gaps": ["case: required case did not pass"]}})
        header = {"format_version": 2, "run_id": "original", "run_dir": str(root),
            "request": request.to_dict(), "definition": definition,
            "initial_refs": {}, "prior_findings": [], "started_at": time.time(),
            "rubric_sha256": "host-supplied-provenance",
            "deadline_epoch": time.time() + 3600}
        return header, app

    def state(self, header, app):
        return {"run_id": "original", "input": header, "state": "failed", "tasks": {},
                "waits": {}, "application_state": app}

    def test_explicit_target_tail_keeps_failure_feedback_and_source_requirements(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            header, original = self.fixture(root)
            original["flowthrough_finish_pending"] = True
            report = root / "artifacts" / "artifact-verification-report.json"
            report.write_text("original failed report\n")
            original["artifact_verification_report"] = {
                "path": report.relative_to(root).as_posix(), "media_type": "application/json"}
            before = deepcopy(original)
            app = prepare_application(root, self.state(header, original),
                start_stage="target_contract_freeze", target_workflow_version=WORKFLOW_VERSION)
            self.assertEqual(before, original)
            self.assertEqual(31, app["agent_assignments"])
            self.assertTrue(app["flowthrough_resume"]["required_target_restart"])
            self.assertEqual("target_contract_freeze", app["flowthrough_resume"]["next_stage"])
            self.assertEqual({"source", "environment", "behavior_freeze", "artifact_test_design"},
                             set(app["effective"]))
            for field in ("artifact_required_failure", "artifact_required_repairs",
                          "required_behavior_status", "required_behavior_assessments",
                          "execution_status", "flowthrough_finish_pending"):
                self.assertNotIn(field, app)
            feedback = app["continuation_feedback"]
            self.assertEqual("required_behavior_unverified", feedback["gate_failure"]["error_code"])
            self.assertEqual("agent_assignment_budget_exhausted",
                feedback["target_restart"]["previous_settlement"]["artifact_required_failure"])
            self.assertEqual({"target_contract_freeze", "artifact_test_execute", "artifact_test_report"},
                             set(feedback["target_restart"]["previous_results"]))
            report.write_text("later target report\n")
            archived = feedback["target_restart"]["previous_settlement"]["artifact_verification_report"]
            self.assertEqual("original failed report\n", (root / archived["path"]).read_text())

    def test_explicit_target_restart_requires_earlier_completed_inputs(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for missing in ("source", "environment", "behavior_freeze", "artifact_test_design"):
                header, app = self.fixture(root)
                del app["effective"][missing]
                with self.assertRaisesRegex(ValueError, "requires completed " + missing):
                    prepare_application(root, self.state(header, app),
                        start_stage="target_contract_freeze", target_workflow_version=WORKFLOW_VERSION)

    def test_author_restart_cannot_rebaseline_a_product_violation(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for error in ('artifact_candidate_changed', 'artifact_binary_shadowed'):
                header, app = self.fixture(root)
                app['artifact_required_failure'] = error
                app['effective']['artifact_test_execute']['error_code'] = error
                with self.subTest(error=error), self.assertRaisesRegex(ValueError, 'cannot replace a product snapshot'):
                    prepare_application(root, self.state(header, app),
                        start_stage='artifact_test_design', target_workflow_version=WORKFLOW_VERSION)
                reopened = prepare_application(root, self.state(header, app),
                    start_stage='target_contract_freeze', target_workflow_version=WORKFLOW_VERSION)
                self.assertIn('artifact_test_design', reopened['effective'])
                self.assertEqual('target_contract_freeze', reopened['flowthrough_resume']['next_stage'])

    def settle(self, owner, root, sdk, host, header):
        bound = time.monotonic() + 15
        state = sdk.get_run(header["run_id"])
        while time.monotonic() < bound:
            host.wake(header["run_id"])
            sdk.sync()
            state = owner.tick(sdk, header)
            if state["state"] in {"succeeded", "failed", "cancelled"}:
                return hydrate_run_snapshot(root, state)
            time.sleep(0.01)
        self.fail("Bounded continuation witness did not settle: " + repr({
            key: task["attempts"][-1]["state"] for key, task in state["tasks"].items()})
            + "; host=" + repr(host.health()) + "; deliveries=" + repr([
                (row["state"], row["attempts"], row["last_error"])
                for row in sdk.delivery_messages(pending_only=True)]))

    def public_continuation(self, *, target_passes=True, start_stage="target_contract_freeze"):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        witness = TargetContinuationWitness(target_passes)
        handlers = {"modport." + stage: witness for stage in (
            "target_contract_freeze", "artifact_test_execute", "artifact_test_design", "artifact_test_report")}
        owner = MigrationOperations(handlers=handlers, isolation_mode="thread")
        header, app = self.fixture(root)
        with open_runtime(root, handlers=handlers, isolation_mode="thread",
                          memory_policy=owner.memory_policy, now=owner.clock) as runtime:
            header["registry_revision"] = runtime.registry_revision
            header["sdk_identity"] = inspect_runtime(root)["module"]
            atomic_json(root / "run.json", header)
            sdk = Orchestrator(root / "orchestrator.sqlite3", runtime.kernel, runtime=runtime)
            try:
                sdk.create_run("original", command_id="seed", input=header, definition=header["definition"])
                app["agent_assignments"] = 0
                schedules = owner._schedule({"run_id": "original", "tasks": {}}, header, app,
                    "artifact_test_design", dependencies=[])
                sdk.apply_operations("original", command_id="seed-author", expected_revision=0,
                    operations=schedules, application_state=app)
                with OrchestratorHost(sdk, worker_count=1) as host:
                    bound = time.monotonic() + 15
                    while time.monotonic() < bound:
                        host.wake("original")
                        sdk.sync()
                        prior = hydrate_run_snapshot(root, sdk.get_run("original"))
                        attempt = prior["tasks"]["artifact_test_design"]["attempts"][-1]
                        if attempt["state"] == "succeeded":
                            app["effective"]["artifact_test_design"] = attempt["result"]["value"]
                            break
                        time.sleep(0.01)
                    else:
                        self.fail("Original author witness did not settle")
                app.update(agent_assignments=31, active_stage=None)
                prior = sdk.get_run("original")
                sdk.apply_operations("original", command_id="settled-failure", expected_revision=prior["revision"],
                    operations=[Operations.finish("failed")], application_state=app)
            finally:
                sdk.close()
        before = owner.status(root, "original", detail=True).snapshot
        successor = continue_from_planner(owner, root, "original", next_run_id="continued",
            reason="Authorized target restart after host correction and fifty extra assignments",
            start_stage=start_stage, additional_agent_assignments=50)
        current = successor.snapshot["input"]
        self.assertEqual(WORKFLOW_VERSION, current["definition"]["workflow_version"])
        self.assertEqual(header["deadline_epoch"], current["deadline_epoch"])
        self.assertEqual(81, current["request"]["budget"]["max_agent_assignments"])
        self.assertEqual(81, current["definition"]["budget"]["max_agent_assignments"])
        self.assertEqual(31, current["continuation"]["agent_assignments_carried"])
        self.assertEqual(start_stage, current["continuation"]["start_stage"])
        self.assertEqual([start_stage], list(successor.snapshot["tasks"]))
        self.assertEqual(before, owner.status(root, "original", detail=True).snapshot)
        sources = current["continuation"]["support_refs"]["continuation:rework_sources"]
        document = json.loads((root / sources["path"]).read_text())
        self.assertTrue(any(row["execution_id"] == "original:artifact_test_design:1"
                            for row in document["sources"]))
        if start_stage == "target_contract_freeze":
            self.assertEqual(31, successor.snapshot["application_state"]["agent_assignments"])
            self.assertEqual(50, 81 - successor.snapshot["application_state"]["agent_assignments"])
        with open_runtime(root, handlers=handlers, isolation_mode="thread",
                          memory_policy=owner.memory_policy, now=owner.clock) as runtime:
            sdk = Orchestrator(root / "orchestrator.sqlite3", runtime.kernel, runtime=runtime)
            try:
                with OrchestratorHost(sdk, worker_count=1) as host:
                    final = self.settle(owner, root, sdk, host, current)
            finally:
                sdk.close()
        return root, final

    def test_public_sdk_freeze_restart_can_pass_only_fresh_required_target_cases(self):
        root, final = self.public_continuation()
        self.assertEqual("succeeded", final["state"])
        self.assertEqual("passed", final["application_state"]["required_behavior_status"])
        self.assertEqual({"target_contract_freeze", "artifact_test_execute", "artifact_test_report"},
                         set(final["tasks"]))
        frozen = json.loads(next(root.glob("artifacts/witness/continued:target_contract_freeze:1/input.json")).read_text())
        self.assertIn("behavior_freeze", frozen["upstream_results"])
        self.assertNotIn("artifact_test_report", frozen["upstream_results"])

    def test_public_sdk_skip_reaches_target_repair_and_cannot_succeed(self):
        root, final = self.public_continuation(target_passes=False)
        self.assertEqual("failed", final["state"])
        self.assertEqual(32, final["application_state"]["agent_assignments"])
        repair = json.loads(next(root.glob("artifacts/witness/continued:artifact_test_design:1/input.json")).read_text())
        self.assertEqual("target", repair["payload"]["required_behavior_repair"]["scope"])
        self.assertTrue(any("case" in gap for gap in repair["payload"]["required_behavior_repair"]["assessment"]["gaps"]))
        self.assertEqual("original:artifact_test_design:1",
                         repair["payload"]["reviewer_rework"]["source_execution_id"])
        self.assertFalse(any(stage.startswith("contract_") for stage in final["tasks"]))

    def test_public_sdk_explicit_author_restart_preserves_old_descriptor_and_assessment(self):
        root, final = self.public_continuation(start_stage="artifact_test_design")
        self.assertEqual("failed", final["state"])
        repair = json.loads(next(root.glob("artifacts/witness/continued:artifact_test_design:1/input.json")).read_text())
        self.assertEqual("original:artifact_test_design:1",
                         repair["payload"]["reviewer_rework"]["source_execution_id"])
        self.assertEqual(["case: required case did not pass"],
                         repair["payload"]["required_behavior_repair"]["assessment"]["gaps"])
        self.assertEqual(32, final["application_state"]["agent_assignments"])
