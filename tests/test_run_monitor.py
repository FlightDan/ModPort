import json
import os
import fcntl
from pathlib import Path
import threading
import tempfile
import time
import unittest
from unittest.mock import patch

from modport.run_monitor import (
    MAX_EVENTS, RunMonitor, _execution_progress_projection, process_alive,
    project_status, read_run_availability, start_monitor,
)
from modport.execution_progress import write_execution_progress
from modport.runner import DriverLease


class Availability:
    def __init__(self, **changes):
        self.rows = {
            "run_id": "run", "run_revision": 1, "run_state": "running",
            "task_count": 2, "active_leases": 0, "queued_ready": 0,
            "expired_leases": 0, "future_retries": 0, "pending_commands": 0,
            "pending_result_sync": 0, "pending_result_delivery": 0,
            "open_waits": 0, "recovery_required": 0, "unknown_effects": 0,
            "missing_executions": 0, "summaries": [], "summaries_truncated": False,
            "complete": True, "snapshot_consistency": "non_atomic",
            "reason_codes": (),
        }
        self.rows.update(changes)

    def to_dict(self):
        return self.rows


class RunMonitorTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.output = self.root / "monitor"

    def test_execution_wait_and_driver_exit_are_distinct(self):
        for report, alive, expected in (
            (Availability(active_leases=1), True, "executing"),
            (Availability(active_leases=1), False, "executing"),
            (Availability(recovery_required=1), False, "recovery_wait"),
            (Availability(open_waits=1), False, "application_wait"),
            (Availability(), False, "driver_exited"),
            (Availability(run_state="succeeded"), False, "terminal"),
        ):
            with self.subTest(expected=expected):
                status = project_status(report, driver_alive=alive, coder_seen=False, now=3)
                self.assertEqual(expected, status["phase"])
                self.assertEqual(alive, status["driver_alive"])

    def test_sdk_runner_and_acceptance_are_independent_projections(self):
        status = project_status(
            Availability(active_leases=1), driver_alive=False,
            coder_seen=False, now=3,
            progress={"verification": {
                "authenticated_test_count": 4, "successful_runs": 1,
                "failed_runs": 0, "evidence_sha256": "a" * 64,
            }},
        )
        self.assertEqual("running", status["execution"]["state"])
        self.assertEqual("executing", status["execution"]["phase"])
        self.assertEqual("execution_interrupted", status["overall_status"])
        self.assertEqual("interrupted", status["runner_health"]["status"])
        self.assertTrue(status["runner_health"]["worker_activity_observed"])
        self.assertEqual("unverified", status["acceptance"]["status"])
        self.assertEqual(4, status["acceptance"]["authenticated_test_count"])

        terminal = project_status(
            Availability(run_state="succeeded"), driver_alive=False,
            coder_seen=False, now=4,
        )
        self.assertEqual("succeeded", terminal["execution"]["state"])
        self.assertEqual("stopped", terminal["runner_health"]["status"])
        self.assertEqual("unverified", terminal["acceptance"]["status"])

    def test_live_process_with_stale_heartbeat_is_not_healthy(self):
        status = project_status(
            Availability(active_leases=1), driver_alive=True,
            coder_seen=False, now=100,
            driver_snapshot={"status": "running", "timestamp": 60},
        )
        self.assertEqual("executing", status["execution"]["phase"])
        self.assertEqual("unresponsive", status["runner_health"]["status"])
        self.assertEqual("driver_heartbeat_stale", status["runner_health"]["reason"])
        self.assertEqual("execution_interrupted", status["overall_status"])

    def test_recent_heartbeat_keeps_invisible_driver_uncertain(self):
        status = project_status(
            Availability(), driver_alive=False, coder_seen=False, now=100,
            driver_snapshot={"status": "running", "timestamp": 95},
        )
        self.assertEqual("running", status["execution"]["state"])
        self.assertEqual("uncertain", status["runner_health"]["status"])
        self.assertEqual("recent_heartbeat_process_not_visible",
                         status["runner_health"]["reason"])
        self.assertEqual("idle", status["phase"])
        self.assertEqual("idle", status["overall_status"])
        stale = project_status(
            Availability(), driver_alive=False, coder_seen=False, now=140,
            driver_snapshot={"status": "running", "timestamp": 95},
        )
        self.assertEqual("interrupted", stale["runner_health"]["status"])
        self.assertEqual("execution_interrupted", stale["overall_status"])

    def test_other_pid_namespace_uses_heartbeat_without_claiming_driver_exit(self):
        snapshot = {"status": "running", "timestamp": 95,
                    "pid_namespace": "pid:[another-namespace]"}
        with patch("modport.run_monitor.pid_namespace", return_value="pid:[observer]"):
            recent = project_status(
                Availability(), driver_alive=True, coder_seen=False, now=100,
                driver_snapshot=snapshot)
            stale = project_status(
                Availability(), driver_alive=False, coder_seen=False, now=140,
                driver_snapshot=snapshot)
        self.assertFalse(recent["driver_alive"])
        self.assertEqual("uncertain", recent["runner_health"]["status"])
        self.assertEqual("driver_pid_namespace_unverified", recent["runner_health"]["reason"])
        self.assertEqual("idle", recent["overall_status"])
        self.assertEqual("unresponsive", stale["runner_health"]["status"])
        self.assertEqual("driver_heartbeat_stale_pid_namespace_unverified",
                         stale["runner_health"]["reason"])

    def test_present_unreadable_namespace_never_authenticates_local_pid(self):
        with patch("modport.run_monitor.pid_namespace", return_value="pid:[observer]"):
            status = project_status(
                Availability(), driver_alive=True, coder_seen=False, now=100,
                driver_snapshot={"status": "running", "timestamp": 95,
                                 "pid_namespace": None})
        self.assertFalse(status["driver_alive"])
        self.assertEqual("uncertain", status["runner_health"]["status"])

    def test_lightweight_polling_is_frequent_for_coder_and_waits(self):
        active = Availability(active_leases=1, summaries=[
            {"task_id": "coder.g1.task", "state": "running", "execution_id": "x"}])
        status = project_status(active, driver_alive=True, coder_seen=False, now=3)
        self.assertTrue(status["coder_seen"])
        self.assertEqual(10, status["next_poll_seconds"])
        wait = project_status(Availability(recovery_required=1), driver_alive=False,
                              coder_seen=status["coder_seen"], now=4)
        self.assertEqual(10, wait["next_poll_seconds"])
        self.assertTrue(wait["coder_seen"])

    def test_execution_stage_telemetry_applies_only_to_v22_runs(self):
        (self.root / "run.json").write_text(json.dumps({
            "definition": {"workflow_version": 21},
        }))
        report = Availability(active_leases=1, summaries=[{
            "task_id": "coder-1", "execution_id": "run:coder-1:1", "state": "running",
        }])
        old_run = RunMonitor(self.root, "run", self.output / "v21",
                             inspect=lambda *_: report, clock=lambda: 100)
        with patch("modport.run_monitor.process_alive", return_value=True):
            self.assertEqual([], old_run.poll()["execution_progress"])

        (self.root / "run.json").write_text(json.dumps({
            "definition": {"workflow_version": 22},
        }))
        new_run = RunMonitor(self.root, "run", self.output / "v22",
                             inspect=lambda *_: report, clock=lambda: 100)
        with patch("modport.run_monitor.process_alive", return_value=True):
            progress = new_run.poll()["execution_progress"]
        self.assertEqual("startup_unconfirmed", progress[0]["phase"])
        self.assertEqual("unconfirmed", progress[0]["state"])

    def test_sdk_availability_without_kernel_attempt_keeps_valid_fence_progress(self):
        execution_id = "run:coder:1"
        identity = {"command_id": execution_id, "run_id": "run", "task_id": "coder",
                    "stage_id": "coder", "attempt": 1}
        self.assertTrue(write_execution_progress(
            self.root, identity, "model_started", kernel_attempt=1,
            fence=7, now=100))
        row = {"execution_id": execution_id, "task_id": "coder",
               "application_attempt": 0, "state": "running", "fence": 7}
        current = _execution_progress_projection(self.root, {"summaries": [row]}, 105)
        self.assertEqual("model_started", current[0]["phase"])
        self.assertEqual("current", current[0]["state"])
        mismatch = _execution_progress_projection(
            self.root, {"summaries": [{**row, "fence": 8}]}, 105)
        self.assertEqual("progress_fence_mismatch", mismatch[0]["phase"])
        known_attempt_mismatch = _execution_progress_projection(
            self.root, {"summaries": [{**row, "attempt": 2}]}, 105)
        self.assertEqual("progress_fence_mismatch", known_attempt_mismatch[0]["phase"])

    def test_recent_shell_receipt_distinguishes_tool_activity_from_stale_marker(self):
        execution_id = "run:coder:1"
        identity = {"command_id": execution_id, "run_id": "run", "task_id": "coder",
                    "stage_id": "coder", "attempt": 1}
        self.assertTrue(write_execution_progress(
            self.root, identity, "handler_entered", kernel_attempt=1,
            fence=7, now=100))
        row = {"execution_id": execution_id, "task_id": "coder",
               "application_attempt": 0, "state": "running", "fence": 7}
        directory = (self.root / "artifacts" / "executions" / execution_id / "opencode-shell")
        directory.mkdir(parents=True)
        (directory / "session.json").write_text(json.dumps({"command_id": execution_id}))
        receipt = directory / ("a" * 32 + ".json")
        receipt.write_text("{}")
        os.utime(receipt, (190, 190))
        os.utime(directory, (190, 190))

        recent = _execution_progress_projection(self.root, {"summaries": [row]}, 200)[0]
        self.assertEqual("handler_entered", recent["phase"])
        self.assertEqual("tool_activity_observed", recent["state"])
        self.assertEqual(190, recent["last_shell_receipt_at"])
        stale = _execution_progress_projection(self.root, {"summaries": [row]}, 251)[0]
        self.assertEqual("no_observed_progress", stale["state"])
        (directory / "session.json").write_text(json.dumps({"command_id": "another-task"}))
        mismatched = _execution_progress_projection(self.root, {"summaries": [row]}, 200)[0]
        self.assertEqual("no_observed_progress", mismatched["state"])

    def test_latest_receipt_pointer_avoids_bounded_scan_overflow(self):
        execution_id = "run:coder:1"
        identity = {"command_id": execution_id, "run_id": "run", "task_id": "coder",
                    "stage_id": "coder", "attempt": 1}
        self.assertTrue(write_execution_progress(
            self.root, identity, "handler_entered", kernel_attempt=1,
            fence=7, now=100))
        row = {"execution_id": execution_id, "task_id": "coder",
               "application_attempt": 0, "state": "running", "fence": 7}
        directory = self.root / "artifacts" / "executions" / execution_id / "opencode-shell"
        directory.mkdir(parents=True)
        (directory / "session.json").write_text(json.dumps({"command_id": execution_id}))
        for index in range(260):
            receipt = directory / (f"{index:032x}.json")
            receipt.write_text("{}")
            os.utime(receipt, (100, 100))
        latest = directory / ("f" * 32 + ".json")
        latest.write_text("{}")
        os.utime(latest, (190, 190))
        (directory / "latest-receipt.json").write_text(json.dumps({
            "schema_version": 1, "command_id": execution_id,
            "receipt": latest.name,
        }))
        os.utime(directory, (190, 190))

        with patch("modport.run_monitor.os.scandir", side_effect=AssertionError("scan not bounded")):
            current = _execution_progress_projection(self.root, {"summaries": [row]}, 200)[0]
        self.assertEqual("tool_activity_observed", current["state"])
        self.assertEqual(190, current["last_shell_receipt_at"])

    def test_stale_execution_stage_requests_one_bounded_diagnostic(self):
        monitor = RunMonitor(self.root, "run", self.output,
                             inspect=lambda *_: Availability(active_leases=1),
                             clock=lambda: 31)
        monitor._last_diagnostic_at = 1
        report = Availability(active_leases=1)
        startup = [{"phase": "startup_unconfirmed", "state": "unconfirmed"}]
        reason = monitor._diagnostic_reason(
            report, alive=True, driver_snapshot=None, now=31, force=False,
            execution_progress=startup)
        self.assertEqual("execution_startup_unconfirmed", reason)
        monitor._last_diagnostic_reason = reason
        self.assertIsNone(monitor._diagnostic_reason(
            report, alive=True, driver_snapshot=None, now=40, force=False,
            execution_progress=startup))

        stale = [{"phase": "waiting_memory", "state": "no_observed_progress"}]
        self.assertEqual("execution_no_observed_progress", monitor._diagnostic_reason(
            report, alive=True, driver_snapshot=None, now=61, force=False,
            execution_progress=stale))

    def test_progress_diagnostics_are_not_repeated_on_ordinary_polls(self):
        calls = []
        monitor = RunMonitor(
            self.root, "run", self.output,
            inspect=lambda *_: Availability(active_leases=1),
            clock=iter((1, 2)).__next__,
        )
        with patch("modport.run_monitor.process_alive", return_value=True), \
             patch("modport.run_monitor.collect_progress_evidence",
                   side_effect=lambda root: calls.append(root) or {"public": {}}):
            monitor.poll()
            monitor.poll()
        self.assertEqual(1, len(calls))

    def test_driver_anomaly_triggers_one_diagnostic_then_quiet_polls(self):
        calls = []
        monitor = RunMonitor(
            self.root, "run", self.output,
            inspect=lambda *_: Availability(active_leases=1),
            clock=iter((1, 2, 3)).__next__,
        )
        alive = iter((True, False, False))
        with patch("modport.run_monitor.process_alive", side_effect=lambda *_: next(alive)), \
             patch("modport.run_monitor.collect_progress_evidence",
                   side_effect=lambda root: calls.append(root) or {"public": {}}):
            monitor.poll()
            monitor.poll()
            monitor.poll()
        self.assertEqual(2, len(calls))

    def test_diagnostic_reason_preserves_sdk_wait_when_driver_is_unhealthy(self):
        states = iter((Availability(active_leases=1), Availability(recovery_required=1)))
        alive = iter((True, False))
        monitor = RunMonitor(
            self.root, "run", self.output,
            inspect=lambda *_: next(states),
            clock=iter((1, 2)).__next__,
        )
        with patch("modport.run_monitor.process_alive", side_effect=lambda *_: next(alive)), \
             patch("modport.run_monitor.collect_progress_evidence",
                   return_value={"public": {}}):
            monitor.poll()
            monitor._diagnostic_thread.join(1)
            status = monitor.poll()
        self.assertIn("runner_interrupted", status["diagnostic_request"])
        self.assertIn("sdk_recovery_or_wait", status["diagnostic_request"])

    def test_blocking_diagnostic_does_not_delay_lightweight_status(self):
        started = threading.Event()
        release = threading.Event()

        def blocked_scan(root):
            started.set()
            release.wait(2)
            return {"public": {}}

        monitor = RunMonitor(
            self.root, "run", self.output,
            inspect=lambda *_: Availability(active_leases=1),
            clock=lambda: 1,
        )
        try:
            with patch("modport.run_monitor.process_alive", return_value=True), \
                 patch("modport.run_monitor.collect_progress_evidence", side_effect=blocked_scan):
                begin = time.monotonic()
                status = monitor.poll()
                elapsed = time.monotonic() - begin
                self.assertTrue(started.wait(1))
                self.assertLess(elapsed, 1)
                self.assertTrue(status["diagnostic_pending"])
                release.set()
                monitor._diagnostic_thread.join(1)
                status = monitor.poll()
                self.assertFalse(status["diagnostic_pending"])
        finally:
            release.set()

    def test_scheduled_diagnostics_are_opt_in(self):
        calls = []
        monitor = RunMonitor(
            self.root, "run", self.output,
            inspect=lambda *_: Availability(active_leases=1),
            clock=iter((1, 2, 11)).__next__,
            diagnostic_interval_seconds=10,
        )
        with patch("modport.run_monitor.process_alive", return_value=True), \
             patch("modport.run_monitor.collect_progress_evidence",
                   side_effect=lambda root: calls.append(root) or {"public": {}}):
            monitor.poll()
            monitor.poll()
            monitor.poll()
        self.assertEqual(2, len(calls))

    def test_restart_restores_last_diagnostic_projection_without_rescanning(self):
        self.output.mkdir()
        (self.output / "monitor-status.json").write_text(json.dumps({
            "run_id": "run", "coder_seen": True,
            "diagnostic_at": 100, "diagnostic_reason": "initial_baseline",
            "progress_evidence": {"verification": {"tests": 3}},
            "progress_delta": {"signature": "delta"},
            "budget_extension": {"eligible": False, "reason": "initial_budget_not_exhausted"},
        }))
        monitor = RunMonitor(
            self.root, "run", self.output,
            inspect=lambda *_: Availability(active_leases=1),
            clock=lambda: 101,
        )
        monitor._restore()
        with patch("modport.run_monitor.process_alive", return_value=True), \
             patch("modport.run_monitor.collect_progress_evidence",
                   side_effect=AssertionError("diagnostic should not run")):
            status = monitor.poll()
        self.assertEqual({"verification": {"tests": 3}}, status["progress_evidence"])
        self.assertEqual({"signature": "delta"}, status["progress_delta"])

    def test_deceased_process_and_recycled_pid_are_not_marked_alive(self):
        self.assertFalse(process_alive(None, None))
        with patch("modport.run_monitor.process_birth", return_value="boot:5"):
            self.assertTrue(process_alive(42, "boot:5"))
            self.assertFalse(process_alive(42, "boot:4"))
            self.assertFalse(process_alive(42, None))

    def test_monitor_continues_after_driver_exit_and_stops_on_sdk_terminal(self):
        states = iter((Availability(active_leases=1, run_revision=1),
                       Availability(open_waits=1, recovery_required=1, run_revision=2),
                       Availability(run_state="failed", run_revision=3)))
        monitor = RunMonitor(self.root, "run", self.output,
                             inspect=lambda root, run_id: next(states),
                             clock=iter((1, 2, 3)).__next__)
        intervals = []
        with patch("modport.run_monitor.process_alive", return_value=False):
            completed = monitor.run(sleep=intervals.append)
        self.assertEqual("failed", completed["run_state"])
        self.assertEqual([10, 10], intervals)
        self.assertEqual("terminal", json.loads((self.output / "monitor-status.json").read_text())["phase"])
        events = json.loads((self.output / "monitor-events.json").read_text())
        self.assertEqual(["executing", "recovery_wait", "terminal"],
                         [event["phase"] for event in events])

    def test_driver_loss_interrupts_long_coder_observation_interval(self):
        states = iter((Availability(active_leases=1), Availability(run_state='failed')))
        alive = {'value': True}
        monitor = RunMonitor(self.root, 'run', self.output, driver_pid=42,
            driver_birth='fixture', coder_seen=True, inspect=lambda *_: next(states), clock=lambda: 100)
        sleeps = []
        def sleep(seconds):
            sleeps.append(seconds)
            alive['value'] = False
        with patch('modport.run_monitor.process_alive', side_effect=lambda *_: alive['value']):
            result = monitor.run(sleep=sleep)
        self.assertEqual('failed', result['run_state'])
        self.assertEqual([10], sleeps)

    def test_events_and_samples_remain_bounded(self):
        monitor = RunMonitor(self.root, "run", self.output,
                             inspect=lambda root, run_id: Availability(
                                 run_revision=monitor.n,
                                 summaries=[{"task_id": str(index), "state": "running",
                                             "execution_id": str(index)} for index in range(300)]),
                             clock=lambda: monitor.n)
        self.output.mkdir()
        for index in range(MAX_EVENTS + 15):
            monitor.n = index
            status = monitor.poll()
        self.assertEqual(100, len(status["active_samples"]))
        events = json.loads((self.output / "monitor-events.json").read_text())
        self.assertEqual(MAX_EVENTS, len(events))
        self.assertNotIn("active_samples", events[-1])

    def test_observation_failure_is_bounded_and_retry_is_possible(self):
        monitor = RunMonitor(self.root, "run", self.output,
                             inspect=lambda root, run_id: (_ for _ in ()).throw(
                                 ValueError("x" * 1000)), clock=lambda: 1)
        self.output.mkdir()
        status = monitor.poll()
        self.assertEqual("observation_error", status["phase"])
        self.assertLessEqual(len(status["error"]), 300)
        self.assertEqual(10, status["next_poll_seconds"])
        self.assertNotIn("error", json.loads((self.output / "monitor-events.json").read_text())[0])

    def test_public_sdk_reader_observes_real_run_without_opening_writers(self):
        from dispatcher_sdk.execution_kernel import Kernel
        from dispatcher_sdk.orchestrator import Orchestrator
        registry = Kernel.open_sqlite(self.root / "kernel.sqlite3", {}, isolation_mode="thread")
        self.addCleanup(registry.close)
        sdk = Orchestrator(self.root / "orchestrator.sqlite3", registry.kernel, runtime=registry)
        sdk.create_run("run", command_id="create")
        sdk.apply_operations("run", command_id="wait", expected_revision=0,
                             operations=[{"kind": "wait", "wait_id": "repair"}])
        sdk.close()
        with patch.object(Orchestrator, "__init__", side_effect=AssertionError("writer initialized")), \
             patch.object(Kernel, "open_sqlite", side_effect=AssertionError("kernel writer initialized")):
            report = read_run_availability(self.root, "run")
        self.assertEqual("run", report.run_id)
        self.assertEqual(1, report.open_waits)
        self.assertEqual(1, report.run_revision)

    def test_resumed_driver_rebinds_existing_monitor_without_duplicate_process(self):
        output = self.root / 'artifacts/monitor'
        output.mkdir(parents=True)
        monitor = RunMonitor(self.root, 'run', output, driver_pid=1, driver_birth='old',
                             inspect=lambda *_: Availability())
        with (output / 'monitor.lock').open('w') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with patch('modport.run_monitor.subprocess.Popen') as spawn:
                start_monitor(self.root, 'run')
                spawn.assert_not_called()
        status = monitor.poll()
        self.assertEqual(os.getpid(), monitor.driver_pid)
        self.assertTrue(status['driver_alive'])

    def test_monitor_launch_is_detached(self):
        with patch('modport.run_monitor.subprocess.Popen') as spawn:
            start_monitor(self.root, 'run')
        self.assertTrue(spawn.call_args.kwargs['start_new_session'])
        self.assertIn('modport.run_monitor', spawn.call_args.args[0])
        identity = json.loads(
            (self.root / 'artifacts/monitor/monitor-driver.json').read_text()
        )
        self.assertEqual(
            {'run_id', 'run_dir', 'pid', 'birth'},
            set(identity),
        )

    def test_monitor_launch_can_pass_optional_diagnostic_interval(self):
        with patch("modport.run_monitor.subprocess.Popen") as spawn:
            start_monitor(self.root, "run", diagnostic_interval_seconds=1800)
        command = spawn.call_args.args[0]
        self.assertEqual(
            ["--diagnostic-interval", "1800.0"],
            command[-2:],
        )

    def test_monitor_start_preserves_live_driver_lease_heartbeat(self):
        with DriverLease(self.root, 'run', heartbeat_interval=60):
            path = self.root / 'artifacts/monitor/monitor-driver.json'
            before = json.loads(path.read_text())
            with patch('modport.run_monitor.subprocess.Popen'):
                start_monitor(self.root, 'run')
            after = json.loads(path.read_text())
        self.assertEqual(before, after)
        self.assertEqual(1, after['schema_version'])
        self.assertEqual('running', after['status'])

    def test_monitor_start_preserves_other_namespace_heartbeat(self):
        with DriverLease(self.root, 'run', heartbeat_interval=60):
            path = self.root / 'artifacts/monitor/monitor-driver.json'
            before = json.loads(path.read_text())
            before['pid_namespace'] = 'pid:[driver]'
            path.write_text(json.dumps(before))
            with patch('modport.run_monitor.pid_namespace', return_value='pid:[observer]'), \
                    patch('modport.run_monitor.process_alive', return_value=False), \
                    patch('modport.run_monitor.subprocess.Popen'):
                start_monitor(self.root, 'run')
            after = json.loads(path.read_text())
        self.assertEqual(before, after)

    def test_monitor_start_keeps_legacy_behavior_without_live_lease(self):
        with DriverLease(self.root, 'run'):
            pass
        path = self.root / 'artifacts/monitor/monitor-driver.json'
        self.assertEqual('stopped', json.loads(path.read_text())['status'])
        with patch('modport.run_monitor.subprocess.Popen'):
            start_monitor(self.root, 'run')
        identity = json.loads(path.read_text())
        self.assertEqual({'run_id', 'run_dir', 'pid', 'birth'}, set(identity))


if __name__ == "__main__":
    unittest.main()
