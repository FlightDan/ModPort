"""SDK v31 selection handoff; fixture JUnit receipts do not prove mod behavior."""
from contextlib import nullcontext, redirect_stdout
from copy import deepcopy
from dataclasses import dataclass
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
import unittest
from unittest.mock import patch

from fixtures_modport import FixtureHandler, registry
from modport.cli import main
from modport.contracts import OperationResult
from modport.evidence import atomic_json, file_digest, verified_path
from modport.handlers import CodexStageHandler, FreezeContractHandler, ReviewHandler, ValidateInputHandler
from modport.models import Budget, MigrationRequest
from modport.operations import MigrationOperations
from modport.prompts import STAGE_PROMPTS
from modport.prompt_compressor import ModelProfile
from modport.sdk_compat import require_compatible_storage


def _contract(source_commit):
    return {
        "schema_version": 1,
        "contract_id": "sdk-test-contract",
        "generator_id": "fixture-contract-author",
        "source_fingerprint": source_commit,
        "behaviors": [{
            "schema_version": 1,
            "id": "behavior.example",
            "entry_id": "behavior.example",
            "source_evidence": "src/main/java/Example.java:1-3",
            "preconditions": ["The example is initialized"],
            "action": ["Read the healthy and edge values"],
            "assertions": ["The healthy value is preserved", "The edge defect is recorded"],
            "side": "both",
            "test_mapping": ["test.keep", "test.defect"],
            "evidence_refs": [],
            "metadata": {},
            "assertion_contracts": [
                {"assertion_id": "behavior.healthy", "text": "The healthy value is preserved",
                 "source_anchor": {"path": "src/main/java/Example.java", "start_line": 2, "end_line": 2},
                 "test_ids": ["test.keep"]},
                {"assertion_id": "behavior.edge", "text": "The edge defect is recorded",
                 "source_anchor": {"path": "src/main/java/Example.java", "start_line": 3, "end_line": 3},
                 "test_ids": ["test.defect"]},
            ],
        }],
        "test_evidence": {
            "test.keep": {
                "path": ".modport/evidence/healthy.json", "evidence_kind": "runtime",
                "executor": "junit", "runtime_operations": ["invoke healthy assertion"],
                "test_source_files": [".modport/tests/ExampleTest.java"],
                "result_identity": {"kind": "junit_xml", "gradle_task": "testHealthy",
                                    "classname": "example.ExampleTest", "name": "healthy"},
            },
            "test.defect": {
                "path": ".modport/evidence/defect.json", "evidence_kind": "runtime",
                "executor": "junit", "runtime_operations": ["invoke edge assertion"],
                "test_source_files": [".modport/tests/ExampleTest.java"],
                "result_identity": {"kind": "junit_xml", "gradle_task": "testDefect",
                                    "classname": "example.ExampleTest", "name": "defect"},
            },
        },
        "baseline_evidence_files": [".modport/evidence/healthy.json", ".modport/evidence/defect.json"],
        "baseline_gradle_tasks": ["testHealthy", "testDefect"],
    }


def _matrix():
    return {
        "schema_version": 1,
        "exploration_notes": ["Located the original test project from its source layout."],
        "cases": [
            {"test_id": "test.keep", "behavior_id": "behavior.example",
             "entry_point": "example.ExampleTest.healthy", "action": "invoke healthy assertion",
             "conditions": ["normal input"], "assertion_ids": ["behavior.healthy"]},
            {"test_id": "test.defect", "behavior_id": "behavior.example",
             "entry_point": "example.ExampleTest.defect", "action": "invoke edge assertion",
             "conditions": ["edge input"], "assertion_ids": ["behavior.edge"]},
        ],
    }


def _assessment():
    return {
        "schema_version": 1,
        "decisions": [
            {"test_id": "test.keep", "decision": "keep", "reason": "Retain healthy behavior coverage."},
            {"test_id": "test.defect", "decision": "source_defect",
             "reason": "The original source fails this edge assertion.",
             "defect_assertion_ids": ["behavior.edge"],
             "evidence": ["Fresh original-source JUnit case receipt"]},
        ],
    }


@dataclass
class _RevisionBoundHandler:
    handler: object
    revision: str

    @property
    def __execution_kernel_revision__(self):
        return self.revision

    def __call__(self, command):
        return self.handler(command)


@dataclass
class _FailedBaselineReceipt:
    """Publish a deterministic, explicitly non-runtime receipt for routing tests."""

    __execution_kernel_revision__ = "test-matrix-sdk-fixture-receipt-v1"

    def __call__(self, command):
        root = Path(command.run_dir)
        source = json.loads((root / "artifacts/source.json").read_text())
        workspace = root / "baseline"
        from modport.opencode_shell_mcp import _workspace_candidate_identity
        candidate = _workspace_candidate_identity(workspace)
        execution = root / "artifacts/executions" / command.command_id
        execution.mkdir(parents=True, exist_ok=True)
        xml = execution / "baseline-results.xml"
        xml.write_text(
            '<testsuite><testcase classname="example.ExampleTest" name="healthy"/>'
            '<testcase classname="example.ExampleTest" name="defect">'
            '<failure>original edge result is wrong</failure></testcase></testsuite>',
            encoding="utf-8",
        )
        xml_ref = {"path": xml.relative_to(root).as_posix(), "sha256": file_digest(xml),
                   "media_type": "application/xml"}
        nonce = "fixture-baseline-nonce"
        declarations = json.loads((workspace / ".modport/functional-contract.json").read_text())["test_evidence"]
        case_results = {}
        assertion_results = {}
        for test_id, outcome, assertion_id in (
            ("test.keep", "passed", "behavior.healthy"),
            ("test.defect", "failed", "behavior.edge"),
        ):
            identity = declarations[test_id]["result_identity"]
            case_results[test_id] = {
                "test_id": test_id, "status": outcome, "test_outcome": outcome,
                "category": "mod_behavior", "source_commit": source["source_commit"],
                "candidate_id": candidate["candidate_id"], "candidate_unchanged": True,
                "execution_nonce": nonce, "result_identity": identity,
                "xml_artifact_ref": deepcopy(xml_ref),
                "case_identity": identity["classname"] + "#" + identity["name"],
            }
            assertion_results[assertion_id] = {
                "status": outcome, "test_ids": [test_id],
                "test_results": {test_id: {
                    "test_outcome": outcome, "result_identity": identity,
                    "candidate_id": candidate["candidate_id"], "case_identity": case_results[test_id]["case_identity"],
                    "execution_nonce": nonce, "xml_artifact_ref": deepcopy(xml_ref),
                }},
            }
        report = {
            "source_commit": source["source_commit"], "execution_nonce": nonce,
            "candidate_before": candidate, "candidate_after": candidate,
            "candidate_unchanged": True, "exit_code": 1,
            "verification_binding": "fixture_receipt_only",
            "case_results": case_results, "assertion_results": assertion_results,
        }
        report_path = execution / "baseline-contract-tests.json"
        atomic_json(report_path, report)
        return OperationResult(
            "failed", command.run_id, command.task_id, command.stage_id, command.command_id,
            outputs={"artifact_refs": {"baseline_contract_tests_candidate": {
                "path": report_path.relative_to(root).as_posix(),
                "sha256": file_digest(report_path), "media_type": "application/json"}}},
            error_code="fixture_original_case_failed", detail="fixture original-source case failed",
        )


@dataclass
class _CaptureMigrationPlan:
    __execution_kernel_revision__ = "test-matrix-sdk-planner-consumer-v1"

    def __call__(self, command):
        root = Path(command.run_dir)
        ref = command.artifact_refs.get("functional_contract_lock")
        if not isinstance(ref, dict):
            raise AssertionError("migration_plan did not receive functional_contract_lock")
        lock_path = verified_path(root, ref)
        lock = json.loads(lock_path.read_text(encoding="utf-8"))
        path = root / "artifacts/migration-plan-selection-input.json"
        atomic_json(path, {
            "lock_ref": ref,
            "selected_test_ids": lock.get("test_selection", {}).get("selected_test_ids"),
            "selected_test_evidence": sorted(lock.get("contract", {}).get("test_evidence", {})),
            "baseline_gradle_tasks": lock.get("contract", {}).get("baseline_gradle_tasks"),
            "source_defects": lock.get("source_defects"),
        })
        result = FixtureHandler()(command)
        return OperationResult(**{**result.to_dict(), "outputs": {
            **result.outputs, "artifact_refs": {**result.outputs.get("artifact_refs", {}),
                "observed_migration_plan_selection": {
                    "path": path.relative_to(root).as_posix(), "sha256": file_digest(path),
                    "media_type": "application/json"}},
        }})


class TestMatrixSDKHandoff(unittest.TestCase):
    def _source_repository(self, root):
        source = root / "source"
        (source / "src/main/java").mkdir(parents=True)
        (source / "src/main/java/Example.java").write_text(
            "class Example {\n  int healthy() { return 1; }\n  int edge() { return -1; }\n}\n",
            encoding="utf-8",
        )
        (source / ".gitignore").write_text(
            ".modport/contract-review.json\n.modport/test-assessment.json\n", encoding="utf-8")
        def git(*args):
            subprocess.run(["git", "-C", str(source), *args], check=True,
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        git("init", "-q")
        git("add", "src/main/java/Example.java", ".gitignore")
        git("-c", "user.name=Test", "-c", "user.email=test@example.invalid",
            "commit", "-qm", "fixture source")
        return source

    def test_failed_source_receipt_is_sealed_frozen_and_delivered_to_planner(self):
        probe_dir = os.environ.get('MODPORT_MATRIX_SDK_PROBE_DIR')
        with (nullcontext(probe_dir) if probe_dir else tempfile.TemporaryDirectory()) as temporary:
            root = Path(temporary)
            root.mkdir(parents=True, exist_ok=True)
            atomic_json(root / 'probe-process.json', {'pid': os.getpid(), 'started_at': time.time(),
                'deadline_seconds': 100, 'run_id': 'matrix-sdk-v31', 'run_dir': str(root / 'run')})
            source = self._source_repository(root)
            run_dir = root / "run"

            # The supported CLI inspection and API preflight are read-only and run before submit opens SDK writers.
            stdout = io.StringIO()
            with redirect_stdout(stdout):
                self.assertEqual(main(["sdk-inspect", "--run-dir", str(run_dir)]), 0)
            inspected = json.loads(stdout.getvalue())
            atomic_json(root / 'sdk-inspection.json', inspected)
            self.assertEqual(inspected["module"]["source_version"], "0.7.0.dev0")
            self.assertTrue(inspected["complete"])
            self.assertTrue(all(item["status"] == "missing" and item["exists"] is False
                                for item in inspected["storages"]))
            storage_preflight = require_compatible_storage(run_dir)
            self.assertEqual(storage_preflight["module"]["source_version"], "0.7.0.dev0")

            handlers = registry()
            handlers.update({
                "modport.source": _RevisionBoundHandler(ValidateInputHandler(), "v31-test-source-v1"),
                "modport.contract_draft": _RevisionBoundHandler(CodexStageHandler(
                    STAGE_PROMPTS["contract_draft"], baseline=True,
                    required_paths=(".modport/functional-contract.json",)), "v31-test-contract-draft-v1"),
                "modport.contract_verify": _FailedBaselineReceipt(),
                "modport.contract_review": _RevisionBoundHandler(ReviewHandler(baseline=True), "v31-test-review-v1"),
                "modport.contract_freeze": _RevisionBoundHandler(FreezeContractHandler(), "v31-test-freeze-v1"),
                "modport.migration_plan": _CaptureMigrationPlan(),
            })
            operations = MigrationOperations(handlers=handlers, isolation_mode="thread")
            request = MigrationRequest(
                "matrix-sdk", str(source), "1.20.1", "26.1.2",
                validation_scope="compile_package",
                budget=Budget(max_seconds=1800, max_agent_assignments=100,
                              max_rework_rounds=0, execution_max_attempts=1),
            )
            run = operations.submit(request, run_dir=run_dir, run_id="matrix-sdk-v31")

            def model_transport(*, cwd, log, command_id, run_root, **_kwargs):
                worktree = Path(cwd)
                modport = worktree / ".modport"
                modport.mkdir(parents=True, exist_ok=True)
                if "contract_draft" in command_id:
                    source_record = json.loads((Path(run_root) / "artifacts/source.json").read_text())
                    atomic_json(modport / "functional-contract.json", _contract(source_record["source_commit"]))
                    atomic_json(modport / "test-matrix.json", _matrix())
                    test_source = modport / "tests/ExampleTest.java"
                    test_source.parent.mkdir(parents=True, exist_ok=True)
                    test_source.write_text("class ExampleTest { void healthy() {} void defect() {} }\n",
                                          encoding="utf-8")
                    wire_contract = _contract(source_record['source_commit'])
                    wire_contract['test_evidence'] = [
                        {'test_id': test_id, 'declaration': declaration}
                        for test_id, declaration in wire_contract['test_evidence'].items()]
                    report = json.dumps(wire_contract)
                elif "contract_review" in command_id:
                    contract = json.loads((modport / "functional-contract.json").read_text())
                    review = {
                        "verdict": "approved", "findings": [], "report": "Fixture source review.",
                        "assertion_reviews": [
                            {"assertion_id": assertion["assertion_id"],
                             "source_anchor": {key: assertion["source_anchor"][key]
                                               for key in ("path", "start_line", "end_line")},
                             "status": "supported", "reasoning": "The cited source range matches this assertion."}
                            for behavior in contract["behaviors"]
                            for assertion in behavior["assertion_contracts"]
                        ],
                    }
                    atomic_json(modport / "contract-review.json", review)
                    atomic_json(modport / "test-assessment.json", _assessment())
                    report = json.dumps(review)
                else:
                    raise AssertionError("unexpected model transport command: " + command_id)
                log = Path(log)
                log.parent.mkdir(parents=True, exist_ok=True)
                log.write_text("deterministic model transport fixture\n", encoding="utf-8")
                stdout = json.dumps({'type': 'item.completed', 'item': {
                    'type': 'agent_message', 'text': report}}) + '\n'
                completed = subprocess.CompletedProcess(["model-transport-fixture"], 0, stdout, "")
                completed.dialogue_metadata = {'status': 'completed', 'dialogue_phase': 'execute',
                                               'planning_turns': 1, 'turns': 1}
                return completed

            class ConsumerObserved(Exception):
                pass

            tick = operations.tick
            deadline = time.monotonic() + 100
            observed = {}
            last_signature = None

            def observe_tick(sdk, header, **kwargs):
                nonlocal last_signature
                state = tick(sdk, header, **kwargs)
                observed['state'] = state
                rows = {name: {'state': task['attempts'][-1]['state'],
                              'result': task['attempts'][-1].get('result')}
                        for name, task in state['tasks'].items()}
                signature = [state['state'], [(name, row['state']) for name, row in rows.items()]]
                if signature != last_signature:
                    atomic_json(root / 'probe-status.json', {'observed_at': time.time(),
                        'run_id': run.run_id, 'workflow_version': 31, 'state': state['state'],
                        'tasks': rows})
                    print('SDK_PROBE_STATUS ' + json.dumps(signature), flush=True)
                    last_signature = signature
                if any(attempt['state'] == 'succeeded'
                       for attempt in state['tasks'].get('migration_plan', {}).get('attempts', [])):
                    raise ConsumerObserved()
                if time.monotonic() >= deadline:
                    raise TimeoutError('SDK selection consumer was not reached within 100 seconds')
                return state

            try:
                with patch("modport.opencode_agent.run_agent", side_effect=model_transport), \
                        patch('modport.prompt_compressor.OpenCodeSummaryBackend.model_profile',
                            side_effect=lambda model: ModelProfile(model, 1_000_000,
                                'deterministic probe model catalog', variants=('high', 'max'))), \
                        patch.object(operations, 'tick', side_effect=observe_tick):
                    try:
                        result = operations.execute(run, poll_interval=0.05)
                    except ConsumerObserved:
                        pass
                    else:
                        observed['state'] = result.snapshot
            finally:
                settled = operations.cancel(run.run_dir, run.run_id)
                atomic_json(root / 'probe-settlement.json', {'run_id': run.run_id,
                    'state': settled.snapshot['state'], 'reason': 'bounded handoff probe ended'})

            state = observed['state']
            tasks = state["tasks"]
            self.assertTrue("migration_plan" in tasks, "run did not reach migration_plan; stages=" +
                            ','.join(tasks))
            self.assertTrue(any(attempt["state"] == "succeeded"
                                for attempt in tasks["migration_plan"]["attempts"]))
            self.assertNotIn("contract_repair_plan", tasks)
            self.assertNotIn("contract_revise", tasks)
            self.assertEqual(state["definition"]["workflow_version"], 31)
            self.assertEqual(state["definition"]["validation_policy"]["scope"], "compile_package")

            planner_ref = next(
                attempt["result"]["value"]["outputs"]["artifact_refs"]["observed_migration_plan_selection"]
                for attempt in tasks["migration_plan"]["attempts"]
                if attempt["state"] == "succeeded"
                and "observed_migration_plan_selection" in attempt["result"]["value"]["outputs"].get("artifact_refs", {})
            )
            planner_input = json.loads(verified_path(run_dir, planner_ref).read_text(encoding="utf-8"))
            self.assertEqual(planner_input["selected_test_ids"], ["test.keep"])
            self.assertEqual(planner_input["selected_test_evidence"], ["test.keep"])
            self.assertEqual(planner_input["baseline_gradle_tasks"], ["testHealthy"])
            self.assertEqual(planner_input["source_defects"][0]["test_id"], "test.defect")
            self.assertEqual(planner_input["source_defects"][0]["case_evidence"]["test_outcome"], "failed")
            atomic_json(root / 'probe-result.json', {'passed': True, 'workflow_version': 31,
                'run_id': run.run_id, 'planner_input': planner_input,
                'substitutions': ['model transport and catalog', 'original baseline receipt', 'unrelated stage handlers'],
                'scope': 'SDK routing and production review/freeze; not gameplay or real-model acceptance'})


if __name__ == "__main__":
    unittest.main()
