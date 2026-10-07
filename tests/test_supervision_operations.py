"""Host integration for asynchronous five-assignment supervision."""
import unittest

from modport.contracts import OperationInput, OperationResult
from modport.models import Budget, MigrationRequest
from modport.operations import MigrationOperations
from modport.workflow import compile_migration_workflow


class SupervisionOperationsTests(unittest.TestCase):
    def setUp(self):
        request = MigrationRequest(
            "supervised", "https://example.invalid/mod.git", "1.20.1", "26.1.2",
            budget=Budget(max_agent_assignments=30),
        )
        self.header = {
            "request": request.to_dict(),
            "definition": compile_migration_workflow(request).to_dict(),
            "deadline_epoch": None, "run_dir": "/tmp/modport-supervision",
            "initial_refs": {}, "prior_findings": [],
            "registry_revision": "a" * 64, "rubric_sha256": "b" * 64,
        }
        # These assertions cover the historical automatic supervisor routing.
        self.header["definition"]["workflow_version"] = 15
        self.operations = MigrationOperations()
        self.app = self.operations._new_application()
        self.app["agent_assignments"] = 5
        self.snapshot = {"run_id": "supervised", "state": "running", "waits": {}, "tasks": {}}
        for index in range(1, 6):
            task_id = f"business-{index}"
            command = OperationInput(
                "supervised", task_id, "background", f"supervised:{task_id}:1",
                self.header["run_dir"], options={"agent_assignment": index},
            )
            self.snapshot["tasks"][task_id] = {"attempts": [{
                "state": "running",
                "command": {"execution_id": command.command_id, "payload": command.to_dict()},
            }]}

    def test_fifth_running_assignment_launches_nonblocking_supervisor_once(self):
        changes = self.operations._supervision_decision(self.snapshot, self.header, self.app)
        additions = [change for change in changes if change["kind"] == "add_task"]
        self.assertEqual(1, len(additions))
        command = OperationInput.from_dict(additions[0]["command"]["payload"])
        self.assertEqual("supervisor", command.stage_id)
        self.assertEqual(5, command.payload["supervision_window"])
        self.assertEqual("running", command.payload["supervision_packet"]["attempts"][-1]["state"])
        self.assertIsNone(command.payload["supervision_packet"]["attempts"][-1]["result"])
        self.assertEqual(5, self.app["agent_assignments"])
        self.assertIsNone(self.app["active_stage"])
        self.assertEqual([], self.operations._supervision_decision(self.snapshot, self.header, self.app))

    def _settled_supervisor(self, decision):
        self.operations._supervision_decision(self.snapshot, self.header, self.app)
        task_id = "supervisor.window.5"
        command = OperationInput(
            "supervised", task_id, "supervisor", "supervised:supervisor.window.5:1",
            self.header["run_dir"], options={"agent_assignment": 5},
            payload={"supervision_window": 5},
        )
        result = OperationResult("completed", command.run_id, command.task_id,
            command.stage_id, command.command_id, outputs={"supervisor_decision": decision})
        self.snapshot["tasks"][task_id] = {"attempts": [{
            "state": "succeeded",
            "command": {"execution_id": command.command_id, "payload": command.to_dict()},
            "result": {"value": result.to_dict()},
        }]}

    def test_replan_directive_retargets_next_agent_boundary(self):
        ids = [f"supervised:business-{index}:1" for index in range(1, 6)]
        decision = {"schema_version": 1, "decision": "replan", "reason": "Repeated blocker",
                    "evidence_execution_ids": ids, "process_improvements": [],
                    "intervention": {"stage": "migration_inventory", "profile": "planner",
                                     "prompt": "Classify the repeated failure first"}}
        self._settled_supervisor(decision)
        self.operations._supervision_decision(self.snapshot, self.header, self.app)
        # An earlier, unrelated scheduling boundary cannot consume or reroute
        # the directive around the workflow graph.
        unrelated = self.operations._schedule(self.snapshot, self.header, self.app, "contract_review")
        unrelated_command = OperationInput.from_dict(next(item for item in unrelated
            if item["kind"] == "add_task")["command"]["payload"])
        self.assertEqual("contract_review", unrelated_command.stage_id)
        self.assertIsNotNone(self.app["supervision"]["directive"])
        changes = self.operations._schedule(self.snapshot, self.header, self.app, "migration_inventory")
        command = OperationInput.from_dict(next(item for item in changes
            if item["kind"] == "add_task")["command"]["payload"])
        self.assertEqual("migration_inventory", command.stage_id)
        self.assertEqual("planner", command.payload["supervisor_intervention"]["profile"])
        self.assertEqual("gpt-5.6-luna", command.options["model"])
        self.assertEqual("high", command.options["reasoning_effort"])
        self.assertIsNone(self.app["supervision"]["directive"])

    def test_pause_is_immediate_host_stop_decision(self):
        ids = [f"supervised:business-{index}:1" for index in range(1, 6)]
        decision = {"schema_version": 1, "decision": "pause", "reason": "No progress",
                    "evidence_execution_ids": ids, "process_improvements": []}
        self._settled_supervisor(decision)
        self.operations._supervision_decision(self.snapshot, self.header, self.app)
        self.assertEqual("supervisor_pause", self.app["stop_reason"])
        self.assertEqual("cancelled", self.app["stop_state"])

    def test_older_completion_cannot_overwrite_newer_directive(self):
        ids = [f"supervised:business-{index}:1" for index in range(1, 6)]
        older = {"schema_version": 1, "decision": "pause", "reason": "Old snapshot",
                 "evidence_execution_ids": ids, "process_improvements": []}
        newer = {"schema_version": 1, "decision": "replan", "reason": "New evidence",
                 "evidence_execution_ids": ids, "process_improvements": [],
                 "intervention": {"stage": "migration_inventory", "profile": "planner",
                                  "prompt": "Use the newer diagnosis"}}
        for window, decision in ((5, older), (10, newer)):
            task_id = f"supervisor.window.{window}"
            command = OperationInput(
                "supervised", task_id, "supervisor", f"supervised:{task_id}:1",
                self.header["run_dir"], options={"agent_assignment": window},
                payload={"supervision_window": window},
            )
            result = OperationResult("completed", command.run_id, command.task_id,
                command.stage_id, command.command_id, outputs={"supervisor_decision": decision})
            self.snapshot["tasks"][task_id] = {"attempts": [{
                "state": "succeeded",
                "command": {"execution_id": command.command_id, "payload": command.to_dict()},
                "result": {"value": result.to_dict()},
            }]}

        self.operations._supervision_decision(self.snapshot, self.header, self.app)
        state = self.app["supervision"]
        self.assertEqual(10, state["highest_applicable_window"])
        self.assertEqual(10, state["directive"]["window_end"])
        self.assertIsNone(self.app["stop_reason"])
        self.assertEqual({5, 10}, {item["window_end"] for item in state["results"]})


if __name__ == "__main__":
    unittest.main()
