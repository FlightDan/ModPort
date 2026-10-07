"""Configuration-only continuation through public SDK and process dispatch."""
from dataclasses import dataclass
import json
from pathlib import Path
import tempfile
import time
import unittest

from dispatcher_sdk.orchestrator import Orchestrator, OrchestratorHost, Operations
from modport.application_state_storage import hydrate_run_snapshot
from modport.continuation import continue_from_planner
from modport.contracts import OperationResult
from modport.handlers import _agent_model_policy
from modport.kernel_runtime import open_runtime
from modport.model_policy import load_model_config, save_model_config, update_model_config
from modport.models import Budget, MigrationRequest
from modport.operations import MigrationOperations


@dataclass
class ConfigurationWitness:
    __execution_kernel_revision__ = "independent-model-witness-v1"

    def __call__(self, command):
        model, effort = _agent_model_policy(command)
        witness = {"model": model, "reasoning_effort": effort,
                   "options_model": command.options["model"],
                   "workflow_version": command.options["workflow_version"]}
        (Path(command.run_dir) / (command.stage_id + "-model.json")).write_text(json.dumps(witness))
        return OperationResult("failed" if command.stage_id == "source" else "completed",
            command.run_id, command.task_id, command.stage_id, command.command_id,
            outputs=witness, error_code="probe_source_failure" if command.stage_id == "source" else None)


class ModelConfigSDKTests(unittest.TestCase):
    def test_model_change_keeps_workflow_deadline_usage_and_reaches_process_worker(self):
        self.check_configuration_continuation()

    def test_model_only_change_carries_already_authorized_recovery_budget(self):
        self.check_configuration_continuation(recovery_limit=12)

    def check_configuration_continuation(self, recovery_limit=None):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "run"
            handlers = {"modport.source": ConfigurationWitness(),
                        "modport.contract_draft": ConfigurationWitness()}
            owner = MigrationOperations(handlers=handlers, isolation_mode="process")
            original = load_model_config()
            original = update_model_config(original, "coder", "gpt-6-luna", "max")
            parent = owner.submit(MigrationRequest("probe", "https://example.invalid/mod.git",
                "1.20.1", "26.1.2", budget=Budget(max_seconds=3600, max_agent_assignments=11)),
                run_dir=root, run_id="models-original", model_policy=original)

            def settle_task(run_id, task_id):
                with open_runtime(root, handlers=handlers, isolation_mode="process",
                                  memory_policy=owner.memory_policy, now=owner.clock) as runtime:
                    sdk = Orchestrator(root / "orchestrator.sqlite3", runtime.kernel, runtime=runtime)
                    host = OrchestratorHost(sdk, worker_count=1)
                    try:
                        state = sdk.get_run(run_id)
                        if task_id not in state["tasks"]:
                            state = owner.tick(sdk, state["input"])
                        if state["tasks"][task_id]["attempts"][-1]["state"] == "planned":
                            sdk.apply_operations(run_id, command_id="dispatch-model-probe:" + task_id,
                                expected_revision=state["revision"], expected_generation=state["generation"],
                                operations=[Operations.dispatch(task_id)])
                        host.start()
                        deadline = time.monotonic() + 15
                        while time.monotonic() < deadline:
                            host.wake(run_id)
                            sdk.sync()
                            state = hydrate_run_snapshot(root, sdk.get_run(run_id))
                            task = state["tasks"].get(task_id)
                            if task and task["attempts"][-1]["state"] in {"succeeded", "failed"}:
                                return state
                            time.sleep(0.02)
                        self.fail("Process worker did not settle: " + repr({key: value["attempts"][-1]["state"]
                            for key, value in state["tasks"].items()}))
                    finally:
                        host.stop()
                        sdk.close()

            parent_state = settle_task(parent.run_id, "source")
            with open_runtime(root, handlers=handlers, isolation_mode="process",
                              memory_policy=owner.memory_policy, now=owner.clock) as runtime:
                sdk = Orchestrator(root / "orchestrator.sqlite3", runtime.kernel, runtime=runtime)
                try:
                    app = parent_state["application_state"]
                    app["agent_assignments"] = 7
                    app["user_cancelled"] = True
                    app["stop_reason"] = "user_cancelled"
                    if recovery_limit is not None:
                        app["recovery_budget_override"] = {"max_agent_assignments": recovery_limit}
                    sdk.apply_operations(parent.run_id, command_id="finish-probe-source",
                        expected_revision=parent_state["revision"],
                        expected_generation=parent_state["generation"],
                        operations=[Operations.finish("cancelled")], application_state=app)
                finally:
                    sdk.close()

            selected = load_model_config()
            config_path = Path(temporary) / "models.json"
            save_model_config(config_path, selected)
            successor = continue_from_planner(owner, root, parent.run_id,
                next_run_id="models-selected", reason="User-selected independent model settings",
                start_stage="contract_draft", additional_agent_assignments=20,
                model_policy=load_model_config(config_path))
            self.assertEqual(33, successor.snapshot["input"]["definition"]["workflow_version"])
            self.assertEqual(parent.snapshot["input"]["deadline_epoch"],
                             successor.snapshot["input"]["deadline_epoch"])
            self.assertEqual((recovery_limit or 11) + 20,
                             successor.snapshot["input"]["request"]["budget"]["max_agent_assignments"])
            self.assertEqual(7, successor.snapshot["input"]["continuation"]["agent_assignments_carried"])
            self.assertEqual(8, successor.snapshot["application_state"]["agent_assignments"])
            self.assertNotIn("workflow_upgrade", successor.snapshot["input"])
            self.assertIsNot(True, successor.snapshot["application_state"].get("user_cancelled"))
            self.assertTrue(owner.status(root, parent.run_id, detail=True).snapshot["application_state"]["user_cancelled"])
            archived = json.loads((root / "artifacts/continuations/models-original/run.json").read_text())
            self.assertEqual(original, archived["model_policy"])
            save_model_config(config_path, update_model_config(selected, "coder", "gpt-6-luna", "max"))
            state = settle_task(successor.run_id, "contract_draft")
            self.assertEqual(selected, state["input"]["model_policy"])
            witness = json.loads((root / "contract_draft-model.json").read_text())
            self.assertEqual({"model": "gpt-6.1-sol", "reasoning_effort": "high",
                              "options_model": "gpt-6.1-sol", "workflow_version": 33}, witness)
