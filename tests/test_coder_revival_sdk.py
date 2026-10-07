"""Current v25 revival route through the real SDK, with deterministic handlers.

The fixture exercises ModPort policy and actual Orchestrator/Kernel tasks,
commands, and attempts. The planner and coder handlers are synthetic; this
does not validate OpenCode model diagnosis or a real migration workspace.
"""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from fixtures_modport import Clock, registry
from modport.application_state_storage import pack_application_state
from modport.contracts import OperationResult
from modport.evidence import file_digest
from modport.memory_admission import MemorySnapshot
from modport.models import Budget, MigrationRequest
from modport.operations import MigrationOperations
from modport.revival_planning import CoderRevivalPlannerHandler
from modport.workflow import CODER_REVIVAL_STAGE, WORKFLOW_VERSION


GIB = 1024 ** 3


class SyntheticCoder:
    """Fail B once, then complete both tasks without invoking an agent."""

    __execution_kernel_revision__ = "coder-revival-sdk-fixture-v1"

    def __init__(self):
        self.calls = []

    def __call__(self, operation):
        task = operation.payload["development_task"]["id"]
        self.calls.append((task, operation.attempt))
        failed = task == "b" and operation.attempt == 1
        return OperationResult(
            "failed" if failed else "completed",
            operation.run_id,
            operation.task_id,
            operation.stage_id,
            operation.command_id,
            detail="synthetic dependency fixture failure" if failed else "synthetic completion",
            error_code="fixture_dependency_failure" if failed else None,
        )


class SyntheticRevivalPlanner:
    """Return a fixed B-resume/A-wait, then A-resume decision sequence."""

    __execution_kernel_revision__ = "coder-revival-planner-fixture-v1"

    def __init__(self):
        self.requests = []
        self.commands = []
        self.fail = False
        self.malformed = False

    def __call__(self, operation):
        request = deepcopy(operation.payload["revival_request"])
        self.requests.append(request)
        self.commands.append(operation)
        if self.fail:
            return OperationResult(
                "failed", operation.run_id, operation.task_id, operation.stage_id,
                operation.command_id, error_code="planner_fixture_unavailable",
                detail="synthetic planner returned no decision")
        if self.malformed and not request["prior_decisions"]:
            return OperationResult(
                "failed", operation.run_id, operation.task_id, operation.stage_id,
                operation.command_id, error_code="revival_decision_invalid",
                detail="synthetic malformed planner decision")
        required = request["required_tasks"]
        if required == ["b"]:
            decisions = [
                {"task_id": "b", "action": "resume",
                 "instruction": "Correct B using its retained failure evidence and rerun the affected API check.",
                 "wait_for": [], "reuse_partial": True},
                {"task_id": "a", "action": "wait",
                 "instruction": "Use B's settled result before selecting A's next action.",
                 "wait_for": ["b"]},
            ]
        elif required == ["a"]:
            decisions = [
                {"task_id": "a", "action": "resume",
                 "instruction": "Update A against B's new result and rerun the dependent API check.",
                 "wait_for": [], "reuse_partial": True},
            ]
        else:
            raise AssertionError(f"unexpected required tasks: {required!r}")
        return OperationResult(
            "completed", operation.run_id, operation.task_id, operation.stage_id,
            operation.command_id,
            outputs={"revival_decision": {
                "reason": "fixture decision follows the controlled dependency result",
                "decisions": decisions,
            }},
            detail="synthetic planner response; no model call",
        )


class ProductionRevivalPlannerFixture:
    __execution_kernel_revision__ = "production-revival-planner-offline-fixture-v1"

    def __call__(self, operation):
        return CoderRevivalPlannerHandler()(operation)


class CoderRevivalSDKTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "run"
        self.clock = Clock()
        self.coder = SyntheticCoder()
        self.planner = SyntheticRevivalPlanner()
        handlers = registry()
        handlers["modport.coder"] = self.coder
        handlers["modport.coder_revival_plan"] = self.planner
        self.operations = MigrationOperations(
            handlers=handlers,
            isolation_mode="thread",
            clock=self.clock,
            memory_probe=lambda: MemorySnapshot(64 * GIB, 64 * GIB, "fixture"),
        )
        request = MigrationRequest(
            "revival-fixture", "https://example.invalid/mod.git", "1.20.1", "26.1.2",
            source_revision="a" * 40,
            budget=Budget(max_seconds=None, max_agent_assignments=6,
                          max_rework_rounds=0, execution_max_attempts=1),
            max_parallel_coders=2,
        )
        self.run = self.operations.submit(request, run_dir=self.root, run_id="revival-sdk")

    def _seed_old_coder_attempts(self, root, header, runtime, sdk, *, include_independent=False):
        snapshot = sdk.get_run(self.run.run_id)
        app = self.operations._new_application()
        tasks = [
            {"id": "b", "objective": "Repair the shared API", "owned_paths": ["src/shared/"],
             "dependencies": [], "acceptance": [], "complexity": "simple",
             "model": "gpt-6-luna", "reasoning_effort": "max"},
            {"id": "a", "objective": "Update the dependent caller", "owned_paths": ["src/caller/"],
             "dependencies": ["b"], "acceptance": [], "complexity": "simple",
             "model": "gpt-6-luna", "reasoning_effort": "max"},
        ]
        if include_independent:
            tasks.append({"id": "c", "objective": "Complete unrelated work",
                          "owned_paths": ["src/independent/"], "dependencies": [],
                          "acceptance": [], "complexity": "simple",
                          "model": "gpt-6-luna", "reasoning_effort": "max"})
        self.operations._start_development_group(header, app, "implementation", {
            "development_tasks": tasks,
            "development_base": "f" * 40,
            "goal_scope": "migration",
        })
        group = app["active_group"]
        group["goal_scheduled"] = ["b", "a"]
        group["scheduled"] = ["b", "a"]
        operations = []
        for task in tasks:
            name = task["id"]
            if name == "c" and include_independent:
                continue
            goal_id = f"goal.g1.{name}"
            goal_path = root / "artifacts" / "goals" / f"{name}.json"
            goal_path.parent.mkdir(parents=True, exist_ok=True)
            goal_path.write_text(f'{{"task":"{name}"}}\n', encoding="utf-8")
            goal_ref = {
                "path": goal_path.relative_to(root).as_posix(),
                "sha256": file_digest(goal_path),
                "media_type": "application/json",
            }
            group["results"][goal_id] = {
                "outputs": {"artifact_refs": {"coder_goal": goal_ref}}
            }
            task_id = f"coder.g1.{name}"
            group["members"].append(task_id)
            operations.extend(self.operations._schedule(
                snapshot, header, app, "coder", task_id=task_id, activate=False,
                dependencies=[], artifact_overrides={"coder_goal": goal_ref},
                payload={"goal_scope": "migration", "goal_generation": 1,
                         "development_generation": 1, "development_task": task,
                         "development_base": group["base"], "planning_context": {},
                         "dependency_patches": []},
                extra_options={"workspace": f"workspaces/development/g1/{name}",
                               "model": task["model"],
                               "reasoning_effort": task["reasoning_effort"]},
            ))
        seeded = sdk.apply_operations(
            self.run.run_id, command_id="seed-coder-attempts",
            expected_revision=snapshot["revision"], expected_generation=snapshot["generation"],
            operations=operations, application_state=pack_application_state(root, app),
        )
        self.assertEqual({"coder.g1.b", "coder.g1.a"}, set(seeded["tasks"]))
        return seeded

    @staticmethod
    def _run_queued(runtime, sdk, expected):
        sdk.flush()
        completed = 0
        for _ in range(expected + 2):
            result = runtime.run_once()
            sdk.sync()
            if result is None:
                break
            completed += 1
        if completed != expected:
            raise AssertionError(f"expected {expected} fixture executions, observed {completed}")

    def _submit_deadline_run(self, suffix):
        self.root = Path(self.temp.name) / f"deadline-{suffix}-run"
        self.run = self.operations.submit(
            MigrationRequest(
                f"revival-deadline-{suffix}", "https://example.invalid/mod.git",
                "1.20.1", "26.1.2", source_revision="a" * 40,
                budget=Budget(max_seconds=60, max_agent_assignments=6,
                              max_rework_rounds=0, execution_max_attempts=1),
                max_parallel_coders=2,
            ), run_dir=self.root, run_id=f"revival-sdk-deadline-{suffix}")

    def test_group_routing_plans_once_then_resumes_a_after_b_settles(self):
        with self.operations.session(self.root, self.run.run_id) as (_, header, runtime, sdk):
            self.assertEqual(WORKFLOW_VERSION, header["definition"]["workflow_version"])
            self.assertEqual("planner_requests", header["definition"]["revival_policy"]["mode"])
            planner_stage = next(row for row in header["definition"]["stages"]
                                 if row["stage_id"] == CODER_REVIVAL_STAGE)
            self.assertTrue(planner_stage["agent"])
            self.assertEqual("modport.coder_revival_plan", planner_stage["handler_id"])

            state = self._seed_old_coder_attempts(self.root, header, runtime, sdk)
            self.assertEqual(2, state["application_state"]["agent_assignments"])
            self._run_queued(runtime, sdk, 2)
            state = sdk.get_run(self.run.run_id)
            self.assertEqual("succeeded", state["tasks"]["coder.g1.a"]["attempts"][-1]["state"])
            self.assertEqual("succeeded", state["tasks"]["coder.g1.b"]["attempts"][-1]["state"])

            # The actual host tick observes B's failed OperationResult and A's
            # stale success, then persists and dispatches the first SDK planner.
            state = self.operations.tick(sdk, header)
            first_request_id = state["application_state"]["active_group"]["revival"]["pending"]
            self.assertTrue(first_request_id.startswith("revival.g1.1."))
            first_task = state["tasks"][first_request_id]
            self.assertEqual("coder_revival_plan", first_task["attempts"][-1]["command"]["payload"]["stage_id"])
            self.assertEqual(1, len(first_task["attempts"]))
            self.assertEqual(3, state["application_state"]["agent_assignments"])

            # Replaying policy while that same planner is pending is idempotent:
            # there is no second planner command or assignment charge.
            repeated = self.operations.tick(sdk, header)
            self.assertEqual(3, repeated["application_state"]["agent_assignments"])
            self.assertEqual(1, len(repeated["tasks"][first_request_id]["attempts"]))
            self.assertEqual(first_request_id,
                             repeated["application_state"]["active_group"]["revival"]["pending"])

            self._run_queued(runtime, sdk, 1)
            state = self.operations.tick(sdk, header)
            b_task = state["tasks"]["coder.g1.b"]
            self.assertEqual(2, len(b_task["attempts"]))
            self.assertEqual("pending_dispatch", b_task["attempts"][-1]["state"])
            self.assertEqual("revival-sdk:coder.g1.b:2",
                             b_task["attempts"][-1]["command"]["execution_id"])
            self.assertEqual("coder", b_task["attempts"][-1]["command"]["payload"]["stage_id"])
            self.assertEqual(4, state["application_state"]["agent_assignments"])

            # B's new SDK execution changes the observed dependency identity.
            # The host creates a second planner task, rather than waking A from
            # the old answer or from completion status alone.
            self._run_queued(runtime, sdk, 1)
            state = self.operations.tick(sdk, header)
            self.assertEqual("succeeded", state["tasks"]["coder.g1.b"]["attempts"][-1]["state"])
            second_request_id = state["application_state"]["active_group"]["revival"]["pending"]
            self.assertNotEqual(first_request_id, second_request_id)
            self.assertTrue(second_request_id.startswith("revival.g1.2."))
            self.assertEqual(5, state["application_state"]["agent_assignments"])

            self._run_queued(runtime, sdk, 1)
            state = self.operations.tick(sdk, header)
            a_task = state["tasks"]["coder.g1.a"]
            self.assertEqual(2, len(a_task["attempts"]))
            self.assertEqual("pending_dispatch", a_task["attempts"][-1]["state"])
            self.assertEqual("coder", a_task["attempts"][-1]["command"]["payload"]["stage_id"])
            self.assertEqual("gpt-6-luna", a_task["attempts"][-1]["command"]["payload"]["options"]["model"])
            self.assertEqual(6, state["application_state"]["agent_assignments"])

            self._run_queued(runtime, sdk, 1)
            state = self.operations.tick(sdk, header)
            a_task = state["tasks"]["coder.g1.a"]
            self.assertEqual("succeeded", a_task["attempts"][-1]["state"])
            self.assertEqual(6, state["application_state"]["agent_assignments"])
            self.assertEqual(["b"], self.planner.requests[0]["required_tasks"])
            self.assertEqual(["a"], self.planner.requests[1]["required_tasks"])
            self.assertNotEqual(
                self.planner.requests[0]["trigger_execution_ids"],
                self.planner.requests[1]["trigger_execution_ids"],
            )
            self.assertEqual("gpt-6-sol", self.planner.commands[0].options["model"])
            self.assertEqual("high", self.planner.commands[0].options["reasoning_effort"])
            self.assertEqual(6, header["request"]["budget"]["max_agent_assignments"])
            self.assertEqual(sorted([("b", 1), ("a", 1), ("b", 2), ("a", 2)]),
                             sorted(self.coder.calls))
            self.assertEqual(2, len(self.planner.requests))

    def test_production_planner_archives_decisions_before_dependent_resume(self):
        handlers = registry()
        handlers["modport.coder"] = self.coder
        handlers["modport.coder_revival_plan"] = ProductionRevivalPlannerFixture()
        self.operations = MigrationOperations(
            handlers=handlers, isolation_mode="thread", clock=self.clock,
            memory_probe=lambda: MemorySnapshot(64 * GIB, 64 * GIB, "fixture"),
        )
        self.root = Path(self.temp.name) / "production-planner-run"
        self.run = self.operations.submit(
            MigrationRequest(
                "production-revival-fixture", "https://example.invalid/mod.git",
                "1.20.1", "26.1.2", source_revision="a" * 40,
                budget=Budget(max_seconds=None, max_agent_assignments=6,
                              max_rework_rounds=0, execution_max_attempts=1),
                max_parallel_coders=2,
            ), run_dir=self.root, run_id="revival-production-planner-sdk",
        )
        planner_requests = []

        def synthetic_dialogue(stage_handler, operation):
            request = operation.payload["revival_request"]
            planner_requests.append(deepcopy(request))
            self.assertTrue(stage_handler.read_only)
            self.assertIn("settled", stage_handler.prompt)
            self.assertIn("execution_evidence", stage_handler.prompt)
            self.assertIn("coder_revival_request", operation.artifact_refs)
            for task_id in request["required_tasks"]:
                self.assertIn(task_id, request["execution_evidence"])
            if request["required_tasks"] == ["b"]:
                decisions = [
                    {"task_id": "b", "action": "resume", "wait_for": [],
                     "instruction": "Inspect B's retained failure evidence and correct the shared API."},
                    {"task_id": "a", "action": "wait", "wait_for": ["b"],
                     "instruction": "Wait for B's new settled result."},
                ]
            elif request["required_tasks"] == ["a"]:
                decisions = [
                    {"task_id": "a", "action": "resume", "wait_for": [],
                     "instruction": "Use B's new result to update the dependent caller."},
                ]
            else:
                raise AssertionError(request["required_tasks"])
            return OperationResult(
                "completed", operation.run_id, operation.task_id,
                operation.stage_id, operation.command_id,
                outputs={
                    "raw_report": json.dumps({
                        "decisions": decisions, "reason": "Synthetic fixture decision from settled SDK evidence",
                    }),
                    "agent_dialogue": {"transport": "opencode", "turns": 2},
                },
            )

        with patch("modport.handlers.CodexStageHandler.__call__", new=synthetic_dialogue):
            with self.operations.session(self.root, self.run.run_id) as (_, header, runtime, sdk):
                self._seed_old_coder_attempts(self.root, header, runtime, sdk)
                self._run_queued(runtime, sdk, 2)
                state = self.operations.tick(sdk, header)
                first_id = state["application_state"]["active_group"]["revival"]["pending"]
                first_execution_id = state["tasks"][first_id]["attempts"][-1]["command"]["execution_id"]
                self._run_queued(runtime, sdk, 1)
                state = self.operations.tick(sdk, header)
                self.assertEqual(2, len(state["tasks"]["coder.g1.b"]["attempts"]))
                self.assertEqual("pending_dispatch", state["tasks"]["coder.g1.b"]["attempts"][-1]["state"])
                self._run_queued(runtime, sdk, 1)
                state = self.operations.tick(sdk, header)
                second_id = state["application_state"]["active_group"]["revival"]["pending"]
                second_execution_id = state["tasks"][second_id]["attempts"][-1]["command"]["execution_id"]
                self.assertNotEqual(first_id, second_id)
                self._run_queued(runtime, sdk, 1)
                state = self.operations.tick(sdk, header)
                a_attempt = state["tasks"]["coder.g1.a"]["attempts"][-1]
                self.assertEqual("pending_dispatch", a_attempt["state"])
                self.assertEqual(2, len(state["tasks"]["coder.g1.a"]["attempts"]))
                self.assertEqual(second_execution_id, a_attempt["command"]["causation_id"])
                self.assertEqual([["b"], ["a"]],
                                 [request["required_tasks"] for request in planner_requests])
                for request_id, execution_id in (
                    (first_id, first_execution_id), (second_id, second_execution_id),
                ):
                    artifact = (self.root / "artifacts" / "executions" /
                                execution_id / "coder-revival-decision.json")
                    decision = json.loads(artifact.read_text(encoding="utf-8"))
                    self.assertEqual(request_id, decision["request_id"])
                    self.assertEqual(execution_id, decision["execution_id"])

    def test_planner_without_decision_stops_instead_of_redispatching(self):
        self.planner.fail = True
        self.root = Path(self.temp.name) / "failed-planner-run"
        self.run = self.operations.submit(
            MigrationRequest(
                "revival-planner-failure", "https://example.invalid/mod.git",
                "1.20.1", "26.1.2", source_revision="a" * 40,
                budget=Budget(max_seconds=None, max_agent_assignments=6,
                              max_rework_rounds=0, execution_max_attempts=1),
                max_parallel_coders=2,
            ), run_dir=self.root, run_id="revival-sdk-failed-planner")
        with self.operations.session(self.root, self.run.run_id) as (_, header, runtime, sdk):
            self._seed_old_coder_attempts(self.root, header, runtime, sdk)
            self._run_queued(runtime, sdk, 2)
            state = self.operations.tick(sdk, header)
            planner_id = state["application_state"]["active_group"]["revival"]["pending"]
            self._run_queued(runtime, sdk, 1)
            state = self.operations.tick(sdk, header)
            self.assertEqual("failed", state["state"])
            self.assertEqual("coder_revival_planner_unavailable",
                             state["application_state"]["terminal_reason"])
            self.assertEqual(3, state["application_state"]["agent_assignments"])
            self.assertEqual([planner_id], [task_id for task_id in state["tasks"]
                                             if task_id.startswith("revival.")])
            record = state["application_state"]["failed_development_group"]["revival"]["requests"][planner_id]
            self.assertEqual("unavailable", record["status"])
            self.assertEqual("planner_fixture_unavailable", record["result"]["error_code"])

    def test_missing_planner_handler_dead_attempt_does_not_loop(self):
        handlers = registry()
        handlers["modport.coder"] = self.coder
        handlers.pop("modport.coder_revival_plan", None)
        self.operations = MigrationOperations(
            handlers=handlers, isolation_mode="thread", clock=self.clock,
            memory_probe=lambda: MemorySnapshot(64 * GIB, 64 * GIB, "fixture"))
        self.root = Path(self.temp.name) / "missing-planner-run"
        self.run = self.operations.submit(
            MigrationRequest(
                "missing-revival-planner", "https://example.invalid/mod.git",
                "1.20.1", "26.1.2", source_revision="a" * 40,
                budget=Budget(max_seconds=None, max_agent_assignments=6,
                              max_rework_rounds=0, execution_max_attempts=1),
                max_parallel_coders=2),
            run_dir=self.root, run_id="revival-sdk-missing-planner")
        with self.operations.session(self.root, self.run.run_id) as (_, header, runtime, sdk):
            self._seed_old_coder_attempts(self.root, header, runtime, sdk)
            self._run_queued(runtime, sdk, 2)
            state = self.operations.tick(sdk, header)
            planner_id = state["application_state"]["active_group"]["revival"]["pending"]
            self._run_queued(runtime, sdk, 1)
            state = self.operations.tick(sdk, header)
            self.assertEqual("failed", state["state"])
            self.assertEqual("dead", state["tasks"][planner_id]["attempts"][-1]["state"])
            self.assertEqual(3, state["application_state"]["agent_assignments"])
            self.assertEqual([planner_id], [task_id for task_id in state["tasks"]
                                             if task_id.startswith("revival.")])
            record = state["application_state"]["failed_development_group"]["revival"]["requests"][planner_id]
            self.assertEqual("unavailable", record["status"])
            self.assertEqual("dead", record["execution"]["state"])
            self.assertEqual("handler_unavailable", record["execution"]["error"]["code"])
            self.assertEqual("execution_dead", record["result"]["error_code"])

    def test_unavailable_planner_allows_unrelated_coder_to_finish(self):
        self.planner.fail = True
        self.root = Path(self.temp.name) / "independent-after-planner-failure"
        self.run = self.operations.submit(
            MigrationRequest(
                "independent-after-planner-failure", "https://example.invalid/mod.git",
                "1.20.1", "26.1.2", source_revision="a" * 40,
                budget=Budget(max_seconds=None, max_agent_assignments=6,
                              max_rework_rounds=0, execution_max_attempts=1),
                max_parallel_coders=3),
            run_dir=self.root, run_id="revival-sdk-independent")
        with self.operations.session(self.root, self.run.run_id) as (_, header, runtime, sdk):
            self._seed_old_coder_attempts(
                self.root, header, runtime, sdk, include_independent=True)
            self._run_queued(runtime, sdk, 2)
            state = self.operations.tick(sdk, header)
            planner_id = state["application_state"]["active_group"]["revival"]["pending"]
            self.assertIn("goal.g1.c", state["tasks"])
            self._run_queued(runtime, sdk, 2)
            state = self.operations.tick(sdk, header)
            self.assertEqual("running", state["state"])
            self.assertIn("coder.g1.c", state["tasks"])
            self._run_queued(runtime, sdk, 1)
            state = self.operations.tick(sdk, header)
            self.assertEqual("failed", state["state"])
            self.assertEqual("completed", state["application_state"]["failed_development_group"]
                             ["results"]["coder.g1.c"]["status"])
            self.assertEqual([planner_id], [task_id for task_id in state["tasks"]
                                             if task_id.startswith("revival.")])

    def test_malformed_planner_report_still_gets_feedback(self):
        self.planner.malformed = True
        self.root = Path(self.temp.name) / "malformed-planner-run"
        self.run = self.operations.submit(
            MigrationRequest(
                "malformed-revival-planner", "https://example.invalid/mod.git",
                "1.20.1", "26.1.2", source_revision="a" * 40,
                budget=Budget(max_seconds=None, max_agent_assignments=6,
                              max_rework_rounds=0, execution_max_attempts=1),
                max_parallel_coders=2),
            run_dir=self.root, run_id="revival-sdk-malformed-planner")
        with self.operations.session(self.root, self.run.run_id) as (_, header, runtime, sdk):
            self._seed_old_coder_attempts(self.root, header, runtime, sdk)
            self._run_queued(runtime, sdk, 2)
            state = self.operations.tick(sdk, header)
            first_id = state["application_state"]["active_group"]["revival"]["pending"]
            self._run_queued(runtime, sdk, 1)
            state = self.operations.tick(sdk, header)
            self.assertEqual("running", state["state"])
            revival = state["application_state"]["active_group"]["revival"]
            self.assertEqual("rejected", revival["requests"][first_id]["status"])
            self.assertIn("malformed", revival["requests"][first_id]["feedback"])
            self.assertNotEqual(first_id, revival["pending"])
            self.assertEqual(4, state["application_state"]["agent_assignments"])

    def test_user_cancel_after_failed_dependency_does_not_dispatch_revival(self):
        with self.operations.session(self.root, self.run.run_id) as (_, header, runtime, sdk):
            self._seed_old_coder_attempts(self.root, header, runtime, sdk)
            self._run_queued(runtime, sdk, 2)
            before = sdk.get_run(self.run.run_id)
            self.assertEqual(2, before["application_state"]["agent_assignments"])
            self.assertFalse(any(task_id.startswith("revival.") for task_id in before["tasks"]))
            # cancel() enters this same host stop route. Keep one session
            # because this fixture's call-tracking handler changes its own
            # synthetic deployment fingerprint after execution.
            cancelled = self.operations.tick(sdk, header, stop_reason="user_cancelled")
            self.assertEqual("cancelled", cancelled["state"])
            self.assertEqual("user_cancelled", cancelled["application_state"]["stop_reason"])
            self.assertFalse(any(task_id.startswith("revival.") for task_id in cancelled["tasks"]))
            self.assertEqual(2, cancelled["application_state"]["agent_assignments"])

    def test_exhausted_shared_assignment_budget_does_not_dispatch_planner(self):
        self.root = Path(self.temp.name) / "budget-exhausted-run"
        request = MigrationRequest(
            "revival-budget-fixture", "https://example.invalid/mod.git", "1.20.1", "26.1.2",
            source_revision="a" * 40,
            budget=Budget(max_seconds=None, max_agent_assignments=2,
                          max_rework_rounds=0, execution_max_attempts=1),
            max_parallel_coders=2,
        )
        self.run = self.operations.submit(request, run_dir=self.root, run_id="revival-budget-sdk")
        with self.operations.session(self.root, self.run.run_id) as (_, header, runtime, sdk):
            self._seed_old_coder_attempts(self.root, header, runtime, sdk)
            self._run_queued(runtime, sdk, 2)
            stopped = self.operations.tick(sdk, header)
            self.assertEqual("agent_assignment_budget_exhausted",
                             stopped["application_state"]["stop_reason"])
            self.assertFalse(any(task_id.startswith("revival.") for task_id in stopped["tasks"]))
            # Host records the stop before the following SDK settlement tick.
            stopped = self.operations.tick(sdk, header)
            self.assertEqual("failed", stopped["state"])
            self.assertEqual("agent_assignment_budget_exhausted",
                             stopped["application_state"]["stop_reason"])
            self.assertEqual(2, stopped["application_state"]["agent_assignments"])
            self.assertFalse(any(task_id.startswith("revival.") for task_id in stopped["tasks"]))

    def test_original_run_deadline_cancels_pending_revival_without_resuming_coders(self):
        self._submit_deadline_run("pending")
        with self.operations.session(self.root, self.run.run_id) as (_, header, runtime, sdk):
            frozen_header_sha = file_digest(self.root / "run.json")
            self.assertEqual(160, header["deadline_epoch"])
            self._seed_old_coder_attempts(self.root, header, runtime, sdk)
            self._run_queued(runtime, sdk, 2)
            state = self.operations.tick(sdk, header)
            planner_id = state["application_state"]["active_group"]["revival"]["pending"]
            self.assertEqual("pending_dispatch", state["tasks"][planner_id]["attempts"][-1]["state"])
            self.assertEqual(3, state["application_state"]["agent_assignments"])

            self.clock.now = 161
            for _ in range(3):
                state = self.operations.tick(sdk, header)
                sdk.flush()
                sdk.sync()
                if state["state"] == "failed":
                    break
            self.assertEqual("failed", state["state"],
                             {"stop_reason": state["application_state"].get("stop_reason"),
                              "planner_state": state["tasks"][planner_id]["attempts"][-1]["state"],
                              "cancel_sent": state["application_state"].get("cancel_sent")})
            self.assertEqual("wall_clock_budget_exhausted",
                             state["application_state"]["terminal_reason"])
            self.assertEqual("cancelled", state["tasks"][planner_id]["attempts"][-1]["state"])
            self.assertEqual(1, len(state["tasks"]["coder.g1.b"]["attempts"]))
            self.assertEqual(1, len(state["tasks"]["coder.g1.a"]["attempts"]))
            self.assertEqual([planner_id], [task_id for task_id in state["tasks"]
                                            if task_id.startswith("revival.")])
            self.assertEqual(3, state["application_state"]["agent_assignments"])
            self.assertEqual([], self.planner.requests)
            self.assertEqual(frozen_header_sha, file_digest(self.root / "run.json"))
            self.assertEqual(160, header["deadline_epoch"])

    def test_original_run_deadline_blocks_settled_planner_decision(self):
        self._submit_deadline_run("settled")
        with self.operations.session(self.root, self.run.run_id) as (_, header, runtime, sdk):
            frozen_header_sha = file_digest(self.root / "run.json")
            self._seed_old_coder_attempts(self.root, header, runtime, sdk)
            self._run_queued(runtime, sdk, 2)
            state = self.operations.tick(sdk, header)
            planner_id = state["application_state"]["active_group"]["revival"]["pending"]
            self._run_queued(runtime, sdk, 1)
            before = sdk.get_run(self.run.run_id)
            self.assertEqual("succeeded", before["tasks"][planner_id]["attempts"][-1]["state"])
            self.assertEqual(1, len(self.planner.requests))
            self.assertEqual(3, before["application_state"]["agent_assignments"])

            self.clock.now = 161
            for _ in range(3):
                state = self.operations.tick(sdk, header)
                sdk.flush()
                sdk.sync()
                if state["state"] == "failed":
                    break
            self.assertEqual("failed", state["state"])
            self.assertEqual("wall_clock_budget_exhausted",
                             state["application_state"]["terminal_reason"])
            self.assertEqual("succeeded", state["tasks"][planner_id]["attempts"][-1]["state"])
            self.assertEqual(1, len(state["tasks"]["coder.g1.b"]["attempts"]))
            self.assertEqual(1, len(state["tasks"]["coder.g1.a"]["attempts"]))
            self.assertEqual([planner_id], [task_id for task_id in state["tasks"]
                                            if task_id.startswith("revival.")])
            self.assertEqual(3, state["application_state"]["agent_assignments"])
            self.assertEqual(frozen_header_sha, file_digest(self.root / "run.json"))
            self.assertEqual(160, header["deadline_epoch"])

    def test_deadline_crossing_inside_tick_cannot_dispatch_revival(self):
        self._submit_deadline_run("inside-tick")
        with self.operations.session(self.root, self.run.run_id) as (_, header, runtime, sdk):
            frozen_header_sha = file_digest(self.root / "run.json")
            self._seed_old_coder_attempts(self.root, header, runtime, sdk)
            self._run_queued(runtime, sdk, 2)
            state = self.operations.tick(sdk, header)
            planner_id = state["application_state"]["active_group"]["revival"]["pending"]
            self._run_queued(runtime, sdk, 1)
            self.clock.now = 159
            original_schedule = self.operations._schedule
            crossed = []

            def cross_at_revival_coder(snapshot, frozen, app, stage, **kwargs):
                if stage == "coder" and kwargs.get("task_id") == "coder.g1.b":
                    crossed.append(True)
                    self.clock.now = 161
                return original_schedule(snapshot, frozen, app, stage, **kwargs)

            with patch.object(self.operations, "_schedule", side_effect=cross_at_revival_coder):
                state = self.operations.tick(sdk, header)
            self.assertEqual([True], crossed)
            self.assertEqual("failed", state["state"])
            self.assertEqual("wall_clock_budget_exhausted",
                             state["application_state"]["terminal_reason"])
            self.assertEqual(1, len(state["tasks"]["coder.g1.b"]["attempts"]))
            self.assertEqual(1, len(state["tasks"]["coder.g1.a"]["attempts"]))
            self.assertEqual([planner_id], [task_id for task_id in state["tasks"]
                                            if task_id.startswith("revival.")])
            self.assertEqual(3, state["application_state"]["agent_assignments"])
            self.assertEqual(frozen_header_sha, file_digest(self.root / "run.json"))
            self.assertEqual(160, header["deadline_epoch"])

    def test_deadline_crossing_before_sdk_commit_discards_revival_dispatch(self):
        self._submit_deadline_run("precommit")
        with self.operations.session(self.root, self.run.run_id) as (_, header, runtime, sdk):
            frozen_header_sha = file_digest(self.root / "run.json")
            self._seed_old_coder_attempts(self.root, header, runtime, sdk)
            self._run_queued(runtime, sdk, 2)
            state = self.operations.tick(sdk, header)
            planner_id = state["application_state"]["active_group"]["revival"]["pending"]
            self._run_queued(runtime, sdk, 1)
            self.clock.now = 159
            original_decision = self.operations._decision
            crossed = []

            def cross_after_policy(snapshot, frozen, **kwargs):
                operations, app = original_decision(snapshot, frozen, **kwargs)
                if (not crossed and any(row["kind"] == "new_attempt"
                                        and row["task_id"] == "coder.g1.b"
                                        for row in operations)):
                    crossed.append(True)
                    self.clock.now = 161
                return operations, app

            with patch.object(self.operations, "_decision", side_effect=cross_after_policy):
                state = self.operations.tick(sdk, header)
            self.assertEqual([True], crossed)
            self.assertEqual("failed", state["state"])
            self.assertEqual(1, len(state["tasks"]["coder.g1.b"]["attempts"]))
            self.assertEqual(1, len(state["tasks"]["coder.g1.a"]["attempts"]))
            self.assertEqual([planner_id], [task_id for task_id in state["tasks"]
                                            if task_id.startswith("revival.")])
            self.assertEqual(3, state["application_state"]["agent_assignments"])
            self.assertEqual("wall_clock_budget_exhausted",
                             state["application_state"]["terminal_reason"])
            self.assertEqual(frozen_header_sha, file_digest(self.root / "run.json"))
            self.assertEqual(160, header["deadline_epoch"])

    def test_deadline_crossing_during_progress_recording_discards_revival_dispatch(self):
        self._submit_deadline_run("progress")
        with self.operations.session(self.root, self.run.run_id) as (_, header, runtime, sdk):
            frozen_header_sha = file_digest(self.root / "run.json")
            self._seed_old_coder_attempts(self.root, header, runtime, sdk)
            self._run_queued(runtime, sdk, 2)
            state = self.operations.tick(sdk, header)
            planner_id = state["application_state"]["active_group"]["revival"]["pending"]
            self._run_queued(runtime, sdk, 1)
            self.clock.now = 159
            from modport.operations import record_command_progress
            crossed = []

            def cross_after_planned(root, command, phase):
                result = record_command_progress(root, command, phase)
                if (phase == "planned" and command.get("execution_id", "").endswith(":coder.g1.b:2")):
                    crossed.append(True)
                    self.clock.now = 161
                return result

            with patch("modport.operations.record_command_progress", side_effect=cross_after_planned):
                state = self.operations.tick(sdk, header)
            self.assertEqual([True], crossed)
            self.assertEqual("failed", state["state"])
            self.assertEqual("wall_clock_budget_exhausted",
                             state["application_state"]["terminal_reason"])
            self.assertEqual(1, len(state["tasks"]["coder.g1.b"]["attempts"]))
            self.assertEqual(1, len(state["tasks"]["coder.g1.a"]["attempts"]))
            self.assertEqual([planner_id], [task_id for task_id in state["tasks"]
                                            if task_id.startswith("revival.")])
            self.assertEqual(3, state["application_state"]["agent_assignments"])
            self.assertEqual(frozen_header_sha, file_digest(self.root / "run.json"))
            self.assertEqual(160, header["deadline_epoch"])


if __name__ == "__main__":
    unittest.main()
