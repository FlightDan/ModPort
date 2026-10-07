"""A v25 handoff keeps selected harness sources through explicit rework."""

import json
import importlib.metadata
import os
from pathlib import Path
import select
import subprocess
import sys
import unittest
from unittest.mock import patch

from fixtures_modport import FixtureHandler, registry
import modport
from modport.artifact_handoff import prepare_handoff
from modport.contracts import OperationResult
from modport.evidence import atomic_json, file_digest, verified_path
from modport.handlers import BaselineContractVerificationHandler, FreezeContractHandler, ValidateInputHandler
from modport.harness_snapshot import RestoreHarnessHandler
from modport.memory_admission import MemorySnapshot
from modport.models import Budget, MigrationRequest
from modport.operations import MigrationOperations
from modport.rework_tools import prepare_session
from modport.rubric import acceptance_rubric

import test_artifact_handoff as handoff_fixture


def runtime_environment():
    environment = {"PATH": os.defpath, "PYTHONDONTWRITEBYTECODE": "1"}
    if os.environ.get("MODPORT_F12_EXPECT_INSTALLED") != "1":
        environment["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")
    return environment


class RestoredContractVerifier(BaselineContractVerificationHandler):
    __execution_kernel_revision__ = "f12-v25-production-contract-verifier-v1"


class RestoredContractSource(ValidateInputHandler):
    __execution_kernel_revision__ = "f12-v25-production-source-v1"


class RestoredContractRestore(RestoreHarnessHandler):
    __execution_kernel_revision__ = "f12-v25-production-harness-restore-v1"


class RestoredContractFreeze(FreezeContractHandler):
    __execution_kernel_revision__ = "f12-v25-production-contract-freeze-v1"


class PreservingContractAuthor:
    __execution_kernel_revision__ = "f12-v25-preserving-author-v1"

    def __call__(self, command):
        path = Path(command.run_dir) / "baseline/.modport/functional-contract.json"
        if not path.is_file() or "reviewer_rework" not in command.payload:
            raise AssertionError("restored contract was not present for explicit rework")
        result = FixtureHandler()(command)
        return OperationResult(**{**result.to_dict(), "outputs": {
            **result.outputs,
            "preserved_contract_sha256": file_digest(path),
        }})


class RestoredContractReviewer:
    __execution_kernel_revision__ = "f12-v25-restored-reviewer-v1"

    def __call__(self, command):
        root = Path(command.run_dir)
        session = prepare_session(command, root / "baseline", 90)
        if session is None:
            raise AssertionError("contract reviewer has no rework session")
        child = subprocess.Popen(
            [sys.executable, "-m", "modport.rework_mcp", "--session", str(session)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, bufsize=1,
            env=runtime_environment(),
        )

        def rpc(identity, method, params):
            child.stdin.write(json.dumps({"jsonrpc": "2.0", "id": identity,
                "method": method, "params": params}) + "\n")
            child.stdin.flush()
            if not select.select([child.stdout], [], [], 90)[0]:
                raise TimeoutError("restored-harness rework MCP did not respond")
            return json.loads(child.stdout.readline())

        try:
            rpc(1, "initialize", {"protocolVersion": "2024-11-05",
                "capabilities": {}, "clientInfo": {"name": "f12-probe", "version": "1"}})
            listing = rpc(2, "tools/call", {"name": "list_rework_targets", "arguments": {}})
            if "contract_restore" not in str(listing):
                raise AssertionError("completed restore was not offered to reviewer")
            reply = rpc(3, "tools/call", {"name": "request_rework", "arguments": {
                "target_agent": "contract_restore",
                "instructions": "Preserve the inherited contract and rerun verification.",
            }})
            result_path = root / "artifacts/f12-restored-reviewer-reply.json"
            atomic_json(result_path, reply)
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


class HandoffReworkConsumerTests(unittest.TestCase):
    def test_v25_handoff_rework_reaches_fresh_production_verifier(self):
        imported = Path(modport.__file__).resolve()
        if os.environ.get("MODPORT_F12_EXPECT_INSTALLED") == "1":
            self.assertTrue(imported.is_relative_to(Path(sys.prefix).resolve()), imported)
        else:
            self.assertEqual(Path(__file__).resolve().parents[1] / "src/modport/__init__.py",
                             imported)
        self.assertEqual("0.7.0.dev0", importlib.metadata.version("dispatcher-sdk"))
        fixture = handoff_fixture.ArtifactHandoffTests()
        fixture.setUp()
        keep_run = os.environ.get("MODPORT_F12_KEEP_RUN") == "1"
        if keep_run:
            fixture.temporary._finalizer.detach()
        else:
            self.addCleanup(fixture.tearDown)
        request = MigrationRequest("example", "https://example.invalid/mod.git",
            "1.20.1", "1.21.1", source_revision=fixture.source_commit,
            budget=Budget(max_seconds=240, max_agent_assignments=30))
        old_header = json.loads((fixture.root / "run.json").read_text())
        old_header["request"] = request.to_dict()
        atomic_json(fixture.root / "run.json", old_header)
        old_header_sha256 = file_digest(fixture.root / "run.json")
        rubric = acceptance_rubric()
        contract = {
            "schema_version": 1,
            "contract_id": "f12-inherited-contract",
            "generator_id": "characterization-agent",
            "source_fingerprint": fixture.source_commit,
            "rubric_id": rubric["rubric_id"],
            "rubric_version": rubric["rubric_version"],
            "rubric_sha256": rubric["rubric_sha256"],
            "baseline_gradle_tasks": ["test"],
            "baseline_evidence_files": [".modport/evidence/test-results.json"],
            "test_evidence": {"game_test.f12": {
                "path": ".modport/evidence/test-results.json",
                "evidence_kind": "runtime", "executor": "client_smoke",
                "runtime_operations": ["start client", "observe behavior"],
                "test_source_files": [".modport/tests/ProbeTest.java"],
            }},
            "behaviors": [{
                "id": "behavior-f12", "source_evidence": "build.gradle",
                "preconditions": ["client starts"],
                "action": ["exercise inherited harness"],
                "assertions": ["consumer sees inherited source"],
                "side": "client", "test_mapping": ["game_test.f12"],
            }],
        }
        selected = ["baseline/.modport/functional-contract.json",
                    "baseline/.modport/tests/ProbeTest.java"]
        for relative, raw in (
            (selected[0], (json.dumps(contract, sort_keys=True) + "\n").encode()),
            (selected[1], b"final class ProbeTest {}\n"),
        ):
            path = fixture.root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(raw)
        package = fixture.base / "handoff-f12"
        manifest = prepare_handoff(fixture.root, package, selected)
        custom = registry()
        custom["modport.source"] = RestoredContractSource()
        custom["modport.contract_restore"] = RestoredContractRestore()
        custom["modport.contract_verify"] = RestoredContractVerifier(require_client_evidence=True)
        custom["modport.contract_freeze"] = RestoredContractFreeze()
        custom["modport.contract_review"] = RestoredContractReviewer()
        custom["modport.contract_draft"] = PreservingContractAuthor()
        host = MigrationOperations(handlers=custom, isolation_mode="thread",
            memory_probe=lambda: MemorySnapshot(64 * 1024**3, 64 * 1024**3, "fixture"))
        root = fixture.base / "new-run"
        inspected = subprocess.run(
            [sys.executable, "-m", "modport.cli", "sdk-inspect", "--run-dir", str(root)],
            capture_output=True, text=True, check=True,
            env=runtime_environment(),
        )
        preflight = json.loads(inspected.stdout)
        self.assertTrue(preflight["complete"])
        self.assertEqual("0.7.0.dev0", preflight["module"]["source_version"])
        self.assertEqual("0.7.0.dev0", preflight["module"]["distribution_version"])
        self.assertEqual("verified", preflight["module"]["distribution_record"])
        self.assertTrue(all(item["status"] == "missing"
                            for item in preflight["storages"]))
        original_exec = __import__("modport.handlers", fromlist=["_exec"])._exec

        def no_project_execution(args, **kwargs):
            if args and args[0] == "bwrap":
                if "--dry-run" not in args:
                    raise AssertionError("full mod harness must not execute")
                log = kwargs["log"]
                log.parent.mkdir(parents=True, exist_ok=True)
                log.write_text("synthetic entry probe stopped before project execution\n")
                return subprocess.CompletedProcess(args, 1,
                    "synthetic entry probe stopped before project execution\n")
            return original_exec(args, **kwargs)

        from modport.workflow import WorkflowDefinition
        with (patch("modport.handlers._exec", side_effect=no_project_execution),
              patch("modport.handlers.urlopen", side_effect=AssertionError("no HTTP")),
              patch("modport.operations.compile_migration_workflow",
                    side_effect=lambda current: WorkflowDefinition(current.to_dict(), version=25)),
              patch("modport.opencode_agent.run_agent", side_effect=AssertionError("no model"))):
            run = host.submit(request, run_dir=root, run_id="f12-v25-consumer",
                artifact_handoff=package, inherit_harness=True)
            frozen = json.loads((root / "run.json").read_text())
            for field in ("source_version", "distribution_version", "source_sha256",
                          "module_path", "distribution_record", "version_agreement"):
                self.assertEqual(preflight["module"][field],
                                 frozen["sdk_identity"][field], field)
            self.assertEqual(25, run.snapshot["input"]["definition"]["workflow_version"])
            self.assertEqual("unverified", manifest["acceptance_status"])
            inherited = json.loads(verified_path(root,
                run.snapshot["input"]["initial_refs"]["inherited_harness"]).read_text())
            self.assertFalse(inherited["scheduler_history_imported"])
            finished = host.execute(run, poll_interval=0.25)
        snapshot = finished.snapshot
        self.assertIn("contract_restore", snapshot["tasks"])
        self.assertIn("contract_verify", snapshot["tasks"])
        for stage in ("source", "contract_restore", "contract_verify"):
            attempt = snapshot["tasks"][stage]["attempts"][0]
            self.assertEqual("succeeded", attempt["state"])
            self.assertEqual("completed" if stage != "contract_verify" else "failed",
                             attempt["result"]["value"]["status"])
        reply_path = root / "artifacts/f12-restored-reviewer-reply.json"
        self.assertTrue(reply_path.is_file(), {
            "run_state": snapshot["state"],
            "reviewer": snapshot["tasks"].get("contract_review"),
            "rework": snapshot["application_state"].get("review_rework"),
        })
        reply = json.loads(reply_path.read_text())
        self.assertNotIn("error", reply)
        # The bounded entry probe is intentionally stopped. The MCP call must
        # return that actual failed verification to its waiting reviewer.
        self.assertTrue(reply.get("result", {}).get("isError"), reply)
        self.assertIn("harness_entry_probe_failed", str(reply))
        self.assertEqual("succeeded", snapshot["tasks"]["contract_review"]["attempts"][0]["state"])
        followups = [task for name, task in snapshot["tasks"].items()
            if name.startswith("agent-rework.")
            and task["attempts"][0]["command"]["payload"]["stage_id"] == "contract_verify"]
        self.assertEqual(1, len(followups))
        authors = [task for name, task in snapshot["tasks"].items()
            if name.startswith("agent-rework.")
            and task["attempts"][0]["command"]["payload"]["stage_id"] == "contract_draft"]
        self.assertEqual(1, len(authors))
        author = authors[0]["attempts"][0]
        self.assertEqual("succeeded", author["state"])
        self.assertEqual("completed", author["result"]["value"]["status"])
        selected_contract = next(item for item in manifest["artifacts"]
            if item["source_path"] == selected[0])
        self.assertEqual(selected_contract["sha256"],
                         author["result"]["value"]["outputs"]["preserved_contract_sha256"])
        self.assertEqual(selected_contract["sha256"],
                         file_digest(root / selected[0]))
        verify = followups[0]["attempts"][0]
        self.assertEqual("succeeded", verify["state"])
        self.assertEqual(author["command"]["execution_id"], verify["command"]["causation_id"])
        self.assertIn(verify["command"]["execution_id"], str(reply))
        self.assertEqual("failed", verify["result"]["value"]["status"])
        self.assertEqual("harness_entry_probe_failed", verify["result"]["value"]["error_code"])
        self.assertFalse(verify["result"]["value"]["outputs"]["harness_executed"])
        self.assertTrue(verify["result"]["value"]["outputs"]["entry_probe_executed"])
        freezes = [task for name, task in snapshot["tasks"].items()
            if name.startswith("agent-rework.")
            and task["attempts"][0]["command"]["payload"]["stage_id"] == "contract_freeze"]
        self.assertEqual(1, len(freezes))
        freeze = freezes[0]["attempts"][0]
        self.assertEqual("succeeded", freeze["state"])
        self.assertEqual(verify["command"]["execution_id"], freeze["command"]["causation_id"])
        frozen = freeze["result"]["value"]
        self.assertEqual("completed", frozen["status"])
        self.assertEqual("unverified", frozen["outputs"]["acceptance_status"])
        binding = frozen["outputs"]["inherited_harness_binding"]
        self.assertEqual("matched", binding["status"])
        self.assertEqual(verify["command"]["execution_id"], binding["verifier_execution_id"])
        self.assertEqual(verify["result"]["value"]["outputs"]["verification_candidate_id"],
                         binding["freeze_candidate_id"])
        for attempt in (snapshot["tasks"]["contract_verify"]["attempts"][0], verify):
            identity = attempt["result"]["value"]["outputs"]["inherited_contract_identity"]
            self.assertEqual("preserved", identity["status"])
            self.assertEqual("f12-inherited-contract", identity["selected_contract_id"])
            self.assertEqual("f12-inherited-contract", identity["candidate_contract_id"])
            self.assertEqual(["behavior-f12"], identity["selected_behavior_ids"])
            self.assertEqual([], identity["missing_behavior_ids"])
            self.assertEqual(fixture.source_commit, identity["selected_source_commit"])
        for relative in selected:
            inherited_key = "inherited_harness:" + relative.removeprefix("baseline/")
            input_ref = verify["command"]["payload"]["artifact_refs"][inherited_key]
            selected_item = next(item for item in manifest["artifacts"]
                if item["source_path"] == relative)
            self.assertEqual(selected_item["sha256"], input_ref["sha256"])
            self.assertEqual(selected_item["sha256"], file_digest(verified_path(root, input_ref)))
        refs = verify["result"]["value"]["outputs"]["artifact_refs"]
        expected = next(item for item in manifest["artifacts"]
            if item["source_path"] == selected[1])
        matches = [ref for key, ref in refs.items()
            if key.startswith("baseline_executor_source:game_test.f12:")]
        self.assertEqual(1, len(matches))
        self.assertEqual(expected["sha256"], matches[0]["sha256"])
        self.assertEqual(expected["sha256"], file_digest(verified_path(root, matches[0])))
        self.assertEqual("unverified", snapshot["application_state"]["acceptance_status"])
        self.assertEqual(old_header_sha256, file_digest(fixture.root / "run.json"))
        self.assertNotIn("sdk_state_that_must_not_be_copied",
                         json.dumps(snapshot["input"]))

        evidence_path = os.environ.get("MODPORT_F12_EVIDENCE")
        if evidence_path:
            branch = Path(__file__).resolve().parents[1]
            head = subprocess.run(["git", "-C", str(branch), "rev-parse", "HEAD"],
                capture_output=True, text=True, check=True).stdout.strip()
            report = {
                "schema": "modport.f12-v25-handoff-rework-consumer/v1",
                "status": "passed_scoped_offline_sdk_consumer",
                "source_head": head,
                "workflow_version": 25,
                "sdk_version": importlib.metadata.version("dispatcher-sdk"),
                "sdk_source_sha256": preflight["module"]["source_sha256"],
                "sdk_frozen_identity_matches_preflight": True,
                "test_source_sha256": file_digest(Path(__file__)),
                "production_handler_source_sha256": file_digest(branch / "src/modport/handlers.py"),
                "run_dir": str(root),
                "run_id": snapshot["run_id"],
                "run_state": snapshot["state"],
                "acceptance_status": snapshot["application_state"]["acceptance_status"],
                "handoff_manifest_sha256": manifest["manifest_sha256"],
                "old_run_header_unchanged_sha256": old_header_sha256,
                "selected_sources": {item["source_path"]: item["sha256"]
                                     for item in manifest["artifacts"]},
                "inherited_scheduler_history": inherited["scheduler_history_imported"],
                "source_execution_id": snapshot["tasks"]["source"]["attempts"][0]["command"]["execution_id"],
                "restore_execution_id": snapshot["tasks"]["contract_restore"]["attempts"][0]["command"]["execution_id"],
                "initial_verify_execution_id": snapshot["tasks"]["contract_verify"]["attempts"][0]["command"]["execution_id"],
                "reviewer_execution_id": snapshot["tasks"]["contract_review"]["attempts"][0]["command"]["execution_id"],
                "author_execution_id": author["command"]["execution_id"],
                "followup_verify_execution_id": verify["command"]["execution_id"],
                "followup_verify_status": verify["result"]["value"]["status"],
                "followup_verify_error_code": verify["result"]["value"]["error_code"],
                "followup_input_selected_bytes_match": True,
                "author_preserved_contract_sha256": selected_contract["sha256"],
                "followup_executor_source_sha256": matches[0]["sha256"],
                "mcp_reply_returned_to_reviewer": True,
                "mcp_reply_is_error": reply["result"]["isError"],
                "mcp_reply_binds_followup_execution": True,
                "model_calls": 0,
                "real_project_execution": False,
                "limits": [
                    "The reviewer and rework author use deterministic fixture handlers; the production verifier and explicit stdio MCP/SDK route are real.",
                    "The sandboxed Gradle task-entry probe was substituted with a deterministic failure. No Minecraft build, test, or model call occurred.",
                    "The run uses MigrationOperations.submit, not the separate CLI process; F12 remains partial.",
                ],
            }
            target = Path(evidence_path)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")


if __name__ == "__main__":
    unittest.main()
