"""Actual SDK same-Run generation recovery and crash-replay witnesses."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from dispatcher_sdk.orchestrator import Orchestrator

from fixtures_modport import Clock, registry
from modport import MigrationOperations, MigrationRequest
from modport.application_state_storage import hydrate_run_snapshot
from modport.contracts import OperationResult, json_copy
from modport.watchdog_recovery import recover
from modport.workflow import WORKFLOW_VERSION


class SupervisorWitness:
    __execution_kernel_revision__ = "watchdog-recovery-supervisor-witness-v1"

    def __init__(self):
        self.operations = []

    def __call__(self, operation):
        self.operations.append(operation)
        return OperationResult("completed", operation.run_id, operation.task_id,
                               operation.stage_id, operation.command_id)


class WatchdogRecoveryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "run"
        self.clock = Clock(1000)
        self.witness = SupervisorWitness()
        handlers = registry(fail_stage="source", fail_count=1)
        handlers["modport.supervisor"] = self.witness
        self.owner = MigrationOperations(handlers=handlers, isolation_mode="thread", clock=self.clock)
        self.owner.submit(MigrationRequest("mod", "https://example.invalid/mod.git", "1.20.1", "26.1.2"),
                          run_dir=self.root, run_id="watchdog")
        self.frozen_bytes = (self.root / "run.json").read_bytes()

    def seed(self, *, state="failed", assignments=7, authorize=True, stop=None):
        with self.owner.session(self.root, "watchdog") as (_, header, runtime, sdk):
            self.assertEqual(header["definition"]["workflow_version"], WORKFLOW_VERSION)
            app = self.owner._new_application()
            app["agent_assignments"] = assignments
            app["watchdog"] = {"authorized": authorize}
            if stop is not None:
                app["watchdog"]["stop_confirmed"] = stop
            operations = self.owner._schedule(sdk.get_run("watchdog"), header, app,
                                              "source", dependencies=[], activate=False)
            sdk.apply_operations("watchdog", command_id="seed-source", expected_revision=0,
                                 operations=operations, application_state=app)
            sdk.flush()
            returned = runtime.run_once()
            self.assertIsNotNone(returned)
            sdk.sync()
            self.owner._drain_result_delivery(sdk)
            observed = hydrate_run_snapshot(self.root, sdk.get_run("watchdog"))
            result = observed["tasks"]["source"]["attempts"][-1]["result"]["value"]
            self.assertEqual(result["status"], "failed")
            app["effective"]["source"] = result
            app["active_stage"] = "source"
            app["active_group"] = {"generation": 4, "retained_control": "original"}
            app["rounds"] = {"coder": 9}
            app["repair_generation"] = 3
            app["terminal_reason"] = "fixture_failure"
            sdk.apply_operations("watchdog", command_id="seed-terminal", expected_revision=observed["revision"],
                                 operations=[{"kind": "finish", "state": state}], application_state=app)
            return hydrate_run_snapshot(self.root, sdk.get_run("watchdog")), header

    def snapshot(self):
        with self.owner.session(self.root, "watchdog", allow_terminal_deployment=True) as (_, _, _, sdk):
            return hydrate_run_snapshot(self.root, sdk.get_run("watchdog"))

    def test_terminal_failure_reopens_same_id_supervisor_first_without_budget_reset(self):
        original, header = self.seed()
        result = recover(self.owner, self.root, "watchdog")
        self.assertEqual(result["status"], "reopened")
        observed = self.snapshot()
        self.assertEqual(observed["run_id"], original["run_id"])
        self.assertEqual(observed["generation"], 1)
        self.assertEqual(observed["input"], original["input"])
        self.assertEqual(observed["definition"], original["definition"])
        self.assertEqual((self.root / "run.json").read_bytes(), self.frozen_bytes)
        app = observed["application_state"]
        self.assertEqual(app["agent_assignments"], original["application_state"]["agent_assignments"] + 1)
        self.assertEqual(app["rounds"], original["application_state"]["rounds"])
        self.assertEqual(app["repair_generation"], 3)
        episode = app['watchdog']['episodes'][app['watchdog']['active']]
        self.assertNotIn('resume_controls', app['watchdog'])
        self.assertEqual(episode["resume_controls"], {
            "active_stage": "source", "active_group": {"generation": 4, "retained_control": "original"}})
        self.assertNotIn("recovery_deadline_epoch", app)
        self.assertNotIn("recovery_budget_override", app)
        new_tasks = set(observed["tasks"]) - set(original["tasks"])
        self.assertEqual(len(new_tasks), 1)
        task = observed["tasks"][new_tasks.pop()]
        operation = task["attempts"][-1]["command"]["payload"]
        self.assertEqual(operation["stage_id"], "supervisor")
        self.assertEqual(operation["options"]["deadline_epoch"], header["deadline_epoch"])
        self.assertEqual(operation["payload"]["watchdog_incident"]["incident_id"], "recovery.g1")
        with self.owner.session(self.root, "watchdog") as (_, _, runtime, sdk):
            sdk.flush()
            self.assertEqual(runtime.run_once().state, "succeeded")
            sdk.sync()
        self.assertEqual(len(self.witness.operations), 1)
        self.assertEqual(self.witness.operations[0].payload["watchdog_incident"]["target_execution_id"],
                         original["tasks"]["source"]["attempts"][-1]["command"]["execution_id"])

    def test_duplicate_call_does_not_create_generation_or_spend_another_assignment(self):
        self.seed()
        first = recover(self.owner, self.root, "watchdog")
        before = self.snapshot()
        second = recover(self.owner, self.root, "watchdog")
        self.assertEqual(second["status"], "reopened")
        self.assertEqual(first["recovery_id"], second["recovery_id"])
        self.assertEqual(before, self.snapshot())

    def test_sdk_prepared_recovery_replays_original_supervisor_dispatch(self):
        self.seed()
        with patch.object(Orchestrator, "advance_recovery", side_effect=RuntimeError("prepared crash")):
            with self.assertRaisesRegex(RuntimeError, "prepared crash"):
                recover(self.owner, self.root, "watchdog")
        result = recover(self.owner, self.root, "watchdog")
        self.assertEqual(result["status"], "reopened")
        self.assertEqual(result["generation"], 1)
        self.assertEqual(self.snapshot()["application_state"]["agent_assignments"], 8)

    def test_sdk_committed_recovery_activates_before_strict_session_open(self):
        self.seed()
        with patch.object(Orchestrator, "_activate_recovery", side_effect=RuntimeError("committed crash")):
            with self.assertRaisesRegex(RuntimeError, "committed crash"):
                recover(self.owner, self.root, "watchdog")
        result = recover(self.owner, self.root, "watchdog")
        self.assertEqual(result["status"], "reopened")
        self.assertEqual(result["generation"], 1)
        self.assertEqual(self.snapshot()["application_state"]["agent_assignments"], 8)

    def test_user_cancellation_cannot_reopen(self):
        original, _ = self.seed(state="cancelled")
        result = recover(self.owner, self.root, "watchdog")
        self.assertEqual((result["status"], result["reason"]), ("terminal", "user_cancelled"))
        self.assertEqual(self.snapshot(), original)

    def test_expired_original_deadline_cannot_reopen(self):
        original, header = self.seed()
        self.clock.now = header["deadline_epoch"] + 1
        result = recover(self.owner, self.root, "watchdog")
        self.assertEqual((result["status"], result["reason"]), ("terminal", "wall_clock_budget_exhausted"))
        self.assertEqual(self.snapshot(), original)

    def test_success_and_confirmed_watchdog_stop_do_not_resume(self):
        self.seed(state="succeeded")
        result = recover(self.owner, self.root, "watchdog")
        self.assertEqual((result["status"], result["reason"]), ("terminal", "run_succeeded"))

    def test_confirmed_unrecoverable_stop_remains_terminal(self):
        original, _ = self.seed(stop={"category": "unrecoverable", "reason": "verified cause", "incident_id": "prior"})
        result = recover(self.owner, self.root, "watchdog")
        self.assertEqual((result["status"], result["reason"]), ("terminal", "watchdog_stop_confirmed"))
        self.assertEqual(self.snapshot(), original)

    def test_assignment_exhaustion_cannot_reopen(self):
        header = self.owner._header(self.root, "watchdog")
        limit = header["request"]["budget"]["max_agent_assignments"]
        self.assertIsNotNone(limit)
        original, _ = self.seed(assignments=limit)
        result = recover(self.owner, self.root, "watchdog")
        self.assertEqual((result["status"], result["reason"]), ("terminal", "agent_assignment_budget_exhausted"))
        self.assertEqual(self.snapshot(), original)

    def test_dead_driver_mailbox_wakes_diagnosis_without_generation_or_assignment(self):
        from modport.watchdog_events import accept_notification
        before = self.snapshot()
        accept_notification(self.root, {"notification_id": "dead-driver", "run_id": "watchdog",
            "kind": "driver_lost", "reason": "Exact driver identity proved dead", "generation": 0})
        result = recover(self.owner, self.root, "watchdog")
        self.assertEqual((result["status"], result["reason"]), ("reopened", "watchdog_diagnostic_pending"))
        self.assertEqual(self.snapshot(), before)

    def test_nonterminal_run_without_incident_is_not_reopened(self):
        before = self.snapshot()
        result = recover(self.owner, self.root, "watchdog")
        self.assertEqual((result["status"], result["reason"]), ("blocked", "run_not_terminal"))
        self.assertEqual(self.snapshot(), before)


if __name__ == "__main__":
    unittest.main()
