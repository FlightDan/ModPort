"""A recovery wait must not strand cancellation of another live assignment."""
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from modport import Budget, MigrationRequest
from modport.memory_admission import MemorySnapshot
from modport.operations import MigrationOperations, MigrationRun
from fixtures_modport import Clock, registry


def waiting_state(review_state, *, stop_reason="wall_clock_budget_exhausted"):
    return {
        "state": "running",
        "waits": {"recovery:verify": {"state": "open"}},
        "application_state": {"stop_reason": stop_reason},
        "tasks": {
            "review": {"attempts": [{"state": review_state}]},
            "verify": {"attempts": [{"state": "recovery_required"}]},
        },
    }


class WaitingStopDeliveryTests(unittest.TestCase):
    def test_recovery_wait_keeps_host_until_running_peer_settles(self):
        ops = object.__new__(MigrationOperations)
        ops.handlers = {}
        ops.isolation_mode = "thread"
        root = Path("/tmp/modport-waiting-peer-test")
        run = MigrationRun("run", root, {})
        header = {"request": {"max_parallel_coders": 1}}
        runtime = SimpleNamespace(reap=MagicMock())
        sdk = SimpleNamespace(sync=MagicMock(), get_run=MagicMock(return_value={}))
        driver = SimpleNamespace(check_health=MagicMock())
        live = waiting_state("running", stop_reason=None)
        settled = waiting_state("succeeded", stop_reason=None)
        host = MagicMock()
        host.__enter__.return_value = host
        host.health.return_value = SimpleNamespace(state="running")
        with (patch.object(ops, "session", return_value=nullcontext((root, header, runtime, sdk))),
              patch.object(ops, "_audit_action"),
              patch.object(ops, "tick", side_effect=[live, live, settled]),
              patch.object(ops, "_audited_observation"),
              patch.object(ops, "_export_audit"),
              patch("modport.operations.OrchestratorHost", return_value=host),
              patch("modport.operations.hydrate_run_snapshot", return_value=settled)):
            result = ops._execute_owned(run, poll_interval=0, driver=driver)
        host.wake.assert_called_once_with("run")
        self.assertEqual("succeeded", result.snapshot["tasks"]["review"]["attempts"][0]["state"])

    def test_public_sdk_deadline_cancels_peer_while_uncertain_effect_stays_parked(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "run"
            clock = Clock()
            ops = MigrationOperations(
                handlers=registry(raise_stage="source"), isolation_mode="thread",
                clock=clock, memory_probe=lambda: MemorySnapshot(
                    64 * 1024**3, 64 * 1024**3, "fixture"))
            request = MigrationRequest("mod", "https://example.invalid/mod.git",
                "1.20.1", "26.1.2", source_revision="a" * 40,
                budget=Budget(max_seconds=1, max_agent_assignments=4,
                              execution_max_attempts=1))
            run = ops.submit(request, run_dir=root, run_id="waiting-stop")
            with ops.session(root, run.run_id) as (_, header, runtime, sdk):
                ops.tick(sdk, header)
                sdk.flush()
                runtime.run_once()
                sdk.sync()
                state = ops.tick(sdk, header)
                self.assertEqual("recovery_required",
                    state["tasks"]["source"]["attempts"][-1]["state"])
                app = state["application_state"]
                scheduled = ops._schedule(state, header, app, "contract_review",
                                          activate=False, dependencies=[])
                sdk.apply_operations(run.run_id, command_id="add-peer-before-deadline",
                    expected_revision=state["revision"], operations=scheduled,
                    application_state=app)
                sdk.flush()
                self.assertIsNotNone(runtime.kernel.claim(
                    "stranded-review-worker", registry_revision=runtime.registry_revision))
                sdk.sync()
                before = sdk.get_run(run.run_id)
                self.assertEqual("leased",
                    before["tasks"]["contract_review"]["attempts"][-1]["state"])
                review_execution = before["tasks"]["contract_review"]["attempts"][-1]["command"]["execution_id"]
                clock.now = header["deadline_epoch"] + 1
            result = ops.resume(root, run.run_id)
            self.assertEqual("wall_clock_budget_exhausted",
                result.snapshot["application_state"]["stop_reason"])
            self.assertEqual("recovery_required",
                result.snapshot["tasks"]["source"]["attempts"][-1]["state"])
            self.assertEqual("cancelled",
                result.snapshot["tasks"]["contract_review"]["attempts"][-1]["state"])
            self.assertEqual(1, len(result.snapshot["tasks"]["contract_review"]["attempts"]))
            self.assertFalse((root / "artifacts/executions" / review_execution / "receipt.json").exists())

    def test_expired_run_drives_live_review_cancel_before_leaving_recovery_wait(self):
        ops = object.__new__(MigrationOperations)
        ops.handlers = {}
        ops.isolation_mode = "thread"
        root = Path("/tmp/modport-waiting-stop-test")
        run = MigrationRun("run", root, {})
        header = {"request": {"max_parallel_coders": 1}}
        runtime = SimpleNamespace(reap=MagicMock())
        sdk = SimpleNamespace(sync=MagicMock(), get_run=MagicMock(return_value={}))
        driver = SimpleNamespace(check_health=MagicMock())
        live = waiting_state("running")
        settled = waiting_state("cancelled")
        host = MagicMock()
        host.__enter__.return_value = host
        host.health.return_value = SimpleNamespace(state="running")
        with (patch.object(ops, "session", return_value=nullcontext((root, header, runtime, sdk))),
              patch.object(ops, "_audit_action"),
              patch.object(ops, "tick", side_effect=[live, live, settled]),
              patch.object(ops, "_audited_observation"),
              patch.object(ops, "_export_audit"),
              patch("modport.operations.OrchestratorHost", return_value=host),
              patch("modport.operations.hydrate_run_snapshot", return_value=settled)):
            result = ops._execute_owned(run, poll_interval=0, driver=driver)
        host.wake.assert_called_once_with("run")
        self.assertEqual("cancelled", result.snapshot["tasks"]["review"]["attempts"][0]["state"])
        self.assertEqual("recovery_required", result.snapshot["tasks"]["verify"]["attempts"][0]["state"])

    def test_recovery_wait_without_live_stop_work_returns_without_host(self):
        ops = object.__new__(MigrationOperations)
        ops.handlers = {}
        ops.isolation_mode = "thread"
        root = Path("/tmp/modport-waiting-stop-test")
        run = MigrationRun("run", root, {})
        header = {"request": {"max_parallel_coders": 1}}
        runtime = SimpleNamespace(reap=MagicMock())
        sdk = SimpleNamespace(sync=MagicMock())
        state = waiting_state("cancelled")
        with (patch.object(ops, "session", return_value=nullcontext((root, header, runtime, sdk))),
              patch.object(ops, "_audit_action"),
              patch.object(ops, "tick", return_value=state),
              patch.object(ops, "_audited_observation"),
              patch.object(ops, "_export_audit"),
              patch("modport.operations.OrchestratorHost") as host):
            result = ops._execute_owned(run, poll_interval=0,
                                        driver=SimpleNamespace(check_health=MagicMock()))
        host.assert_not_called()
        self.assertIs(result.snapshot, state)


if __name__ == "__main__":
    unittest.main()
