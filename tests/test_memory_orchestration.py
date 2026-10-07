from contextlib import contextmanager
from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
import uuid

from modport.contracts import OperationInput, OperationResult
from modport.memory_admission import MemoryPolicy, MemorySnapshot, MIB
from modport.operations import MigrationOperations
from modport.rework_tools import prepare_session
import test_goal_operations as goal_fixtures
import test_planning_operations as planning_fixtures


GIB = 1024 ** 3


class MemoryOrchestrationTests(unittest.TestCase):
    def test_host_admission_subtracts_reservation_for_actual_active_stage(self):
        fixture = planning_fixtures.PlanningPolicyTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        probe = MemorySnapshot(3 * 1024 * MIB, 8 * 1024 * MIB, "fixture")
        operations = MigrationOperations(
            memory_probe=lambda: probe, memory_policy=MemoryPolicy())

        for active_stage, state, expected_starts in (
                ("contract_draft", "pending_dispatch", 1), ("coder", "queued", 0)):
            with self.subTest(active_stage=active_stage, state=state):
                command = OperationInput(
                    "policy", "active", active_stage, "active:1",
                    fixture.header["run_dir"],
                )
                fixture.snapshot["tasks"] = {"active": {"attempts": [{
                    "state": state, "command": {
                        "execution_id": command.command_id,
                        "payload": command.to_dict(),
                    },
                }]}}
                app = fixture.operations._new_application()
                starts = operations._memory_capacity(
                    fixture.snapshot, fixture.header, app, "cross-stage",
                    requested_stage="coder")
                self.assertEqual(expected_starts, starts)

        command = OperationInput(
            "policy", "planned", "coder", "planned:1", fixture.header["run_dir"])
        fixture.snapshot["tasks"] = {"planned": {"attempts": [{
            "state": "planned", "command": {
                "execution_id": command.command_id, "payload": command.to_dict(),
            },
        }]}}
        app = fixture.operations._new_application()
        self.assertEqual(1, operations._memory_capacity(
            fixture.snapshot, fixture.header, app, "unplanned",
            requested_stage="coder"))

    def test_low_memory_waits_without_charge_then_admits_only_one_pending_goal(self):
        fixture = goal_fixtures.GoalOperationsTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        policy, header, app, snapshot, _ = fixture.setup_group()
        probe = [MemorySnapshot(GIB, 4 * GIB, "fixture")]
        policy.memory_probe = lambda: probe[0]
        self.assertEqual([], policy._group_decision(snapshot, header, app))
        self.assertEqual(0, app["agent_assignments"])
        self.assertTrue(all(value == "waiting_memory"
                            for value in app["active_group"]["task_states"].values()))
        previous = deepcopy(app)
        self.assertEqual([], policy._group_decision(snapshot, header, app))
        self.assertEqual(previous, app)  # No per-tick samples inflate audit state.
        probe[0] = MemorySnapshot(3 * GIB, 4 * GIB, "fixture")
        changes = policy._group_decision(snapshot, header, app)
        additions = [change for change in changes if change["kind"] == "add_task"]
        self.assertEqual(1, len(additions))
        addition = additions[0]
        snapshot["tasks"][addition["task_id"]] = {"attempts": [{
            "state": "pending", "command": addition["command"]}]}
        self.assertEqual([], policy._group_decision(snapshot, header, app))
        self.assertEqual(1, app["agent_assignments"])
        self.assertFalse(app["stop_reason"])
        fixture.settle(snapshot, changes, "coder_goal")
        later = policy._group_decision(snapshot, header, app)
        self.assertEqual(1, sum(change["kind"] == "dispatch" for change in later))

    def test_nested_rework_fails_promptly_uncharged_and_allows_new_request(self):
        fixture = planning_fixtures.PlanningPolicyTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        root = Path(fixture.header["run_dir"])
        (root / "worktree").mkdir()
        options = {"workflow_version": 16, "gate_policy": "downstream_toolcall"}
        source = OperationInput("policy", "coder.g1.a", "coder", "coder-1", str(root), options=options)
        result = OperationResult("completed", source.run_id, source.task_id,
                                 source.stage_id, source.command_id)
        caller = OperationInput("policy", "code_review", "code_review", "review-1", str(root),
            options=options, payload={"review_rework_targets": [{"target_agent": source.task_id}]},
            upstream_results={source.task_id: result.to_dict()})
        fixture.snapshot["tasks"] = {
            source.task_id: {"attempts": [{"state": "succeeded",
                "command": {"execution_id": source.command_id, "payload": source.to_dict()},
                "result": {"value": result.to_dict()}}]},
            caller.task_id: {"attempts": [{"state": "running",
                "command": {"execution_id": caller.command_id, "payload": caller.to_dict()}}]},
        }
        session = prepare_session(caller, root / "worktree", 60)
        self.assertIsNotNone(session)
        request_id = str(uuid.uuid4())
        request = Path(session).parent / "requests" / (request_id + ".json")
        request.parent.mkdir(exist_ok=True)
        request.write_text(json.dumps({"request_id": request_id, "run_id": "policy",
            "reviewer_execution_id": caller.command_id, "target_agent": source.task_id,
            "instructions": "Repair the consumer's failing case."}))
        fixture.operations.memory_probe = lambda: MemorySnapshot(GIB, 4 * GIB, "fixture")
        self.assertEqual([], fixture.operations._review_rework_decision(
            fixture.snapshot, fixture.header, fixture.app))
        records = fixture.app["review_rework"]["requests"]
        self.assertEqual(["failed"], [record["state"] for record in records.values()])
        self.assertIn("Insufficient memory", next(iter(records.values()))["error"])
        self.assertEqual(0, fixture.app["agent_assignments"])
        self.assertEqual({}, fixture.app["rounds"])
        fixture.operations.memory_probe = lambda: MemorySnapshot(16 * GIB, 16 * GIB, "fixture")
        new_id = str(uuid.uuid4())
        new_request = json.loads(request.read_text())
        new_request["request_id"] = new_id
        request.with_name(new_id + ".json").write_text(json.dumps(new_request))
        changes = fixture.operations._review_rework_decision(fixture.snapshot, fixture.header, fixture.app)
        self.assertEqual(["agent_rework"], [item.stage_id for item in fixture.scheduled(changes)])
        self.assertEqual(1, fixture.app["agent_assignments"])

    def test_nested_rework_reuses_waiting_coder_slot(self):
        fixture = planning_fixtures.PlanningPolicyTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        root = Path(fixture.header["run_dir"])
        (root / "worktree").mkdir()
        options = {"workflow_version": 16, "gate_policy": "downstream_toolcall"}
        source = OperationInput("policy", "coder.g1.a", "coder", "coder-1",
                                str(root), options=options)
        source_result = OperationResult("completed", source.run_id, source.task_id,
                                        source.stage_id, source.command_id)
        caller = OperationInput(
            "policy", "coder.g1.reviewer", "coder", "review-1", str(root),
            options=options,
            payload={"review_rework_targets": [{"target_agent": source.task_id}]},
            upstream_results={source.task_id: source_result.to_dict()},
        )
        fixture.snapshot["tasks"] = {
            source.task_id: {"attempts": [{"state": "succeeded",
                "command": {"execution_id": source.command_id,
                            "payload": source.to_dict()},
                "result": {"value": source_result.to_dict()}}]},
            caller.task_id: {"attempts": [{"state": "running",
                "command": {"execution_id": caller.command_id,
                            "payload": caller.to_dict()}}]},
        }
        session = prepare_session(caller, root / "worktree", 60)
        self.assertIsNotNone(session)
        request_id = str(uuid.uuid4())
        request = Path(session).parent / "requests" / (request_id + ".json")
        request.parent.mkdir(exist_ok=True)
        request.write_text(json.dumps({"request_id": request_id, "run_id": "policy",
            "reviewer_execution_id": caller.command_id, "target_agent": source.task_id,
            "instructions": "Repair the consumer's failing case."}))

        # The caller consumes the only configured parallel coder slot. Once it
        # is waiting in request_rework, the child may reuse that slot but must
        # still reserve memory for both live processes.
        fixture.header["request"]["max_parallel_coders"] = 1
        fixture.operations.memory_probe = lambda: MemorySnapshot(3 * GIB, 16 * GIB, "fixture")
        self.assertEqual([], fixture.operations._review_rework_decision(
            fixture.snapshot, fixture.header, fixture.app))
        self.assertEqual(0, fixture.app["agent_assignments"])
        self.assertIn("Insufficient memory", next(iter(
            fixture.app["review_rework"]["requests"].values()))["error"])

        next_id = str(uuid.uuid4())
        next_request = json.loads(request.read_text())
        next_request["request_id"] = next_id
        request.with_name(next_id + ".json").write_text(json.dumps(next_request))
        fixture.operations.memory_probe = lambda: MemorySnapshot(16 * GIB, 16 * GIB, "fixture")
        changes = fixture.operations._review_rework_decision(
            fixture.snapshot, fixture.header, fixture.app)
        self.assertEqual(["agent_rework"],
                         [item.stage_id for item in fixture.scheduled(changes)])
        self.assertEqual(1, fixture.app["agent_assignments"])
        record = fixture.app["review_rework"]["requests"][caller.command_id + '/' + next_id]
        self.assertEqual("running", record["state"])

    def test_native_recovery_does_not_enter_coder_when_memory_is_unavailable(self):
        fixture = planning_fixtures.PlanningPolicyTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        root = Path(fixture.header["run_dir"])
        operation = OperationInput("policy", "coder.g1.a", "coder", "coder-1", str(root))
        command = SimpleNamespace(execution_id=operation.command_id,
                                  to_dict=lambda: {"payload": operation.to_dict()})
        recovery = SimpleNamespace(execution=SimpleNamespace(command=command),
                                   effect=SimpleNamespace(effect_id="effect-1"))
        sdk = Mock()
        sdk.inspect_recoveries.return_value = [recovery]
        sdk.get_run.return_value = fixture.snapshot
        fixture.operations.memory_probe = lambda: MemorySnapshot(0, 4 * GIB, "fixture")

        @contextmanager
        def session(*args):
            yield root, fixture.header, Mock(), sdk

        # This policy fixture has no persisted Run; mock both header and session
        # boundaries while leaving storage and memory admission checks active.
        with patch.object(fixture.operations, "_header", return_value=fixture.header), \
                patch.object(fixture.operations, "session", session), \
                patch.object(fixture.operations, "tick"), \
                patch("modport.kernel_runtime.resume_interrupted_goal") as resume:
            with self.assertWarnsRegex(RuntimeWarning, "recovery deferred"):
                fixture.operations.recover(root, "policy", resume_native_goals=True)
        resume.assert_not_called()
        sdk.resolve_effect.assert_not_called()


if __name__ == "__main__":
    unittest.main()
