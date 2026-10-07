import fcntl
import json
import multiprocessing
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
import warnings

import modport.runner as runner_module
from modport.run_monitor import pid_namespace, process_alive, process_birth
from modport.runner import (
    DRIVER_LOCK_NAME,
    DriverHealthError,
    DriverLease,
    DriverLeaseBusyError,
    DriverLeaseError,
    read_driver_health,
)


def _kill_same_process(pid: int, birth: str | None) -> None:
    if not process_alive(pid, birth):
        return
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def _hold_lease(root: str, run_id: str, ready, release) -> None:
    with DriverLease(root, run_id, heartbeat_interval=0.05):
        ready.send(True)
        ready.close()
        release.recv()


def _crash_with_lease(root: str, ready) -> None:
    with DriverLease(root, "crashed", heartbeat_interval=0.05):
        ready.send(True)
        ready.close()
        signal.pause()


class DriverLeaseTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    @unittest.skipUnless(hasattr(os, "fork"), "requires POSIX flock process semantics")
    def test_same_root_rejects_a_second_process_across_run_ids(self):
        context = multiprocessing.get_context("fork")
        ready_parent, ready_child = context.Pipe()
        release_parent, release_child = context.Pipe()
        process = context.Process(
            target=_hold_lease,
            args=(str(self.root), "segment-a", ready_child, release_child),
        )
        process.start()
        self.addCleanup(lambda: process.is_alive() and process.kill())
        self.assertTrue(ready_parent.poll(5))
        self.assertTrue(ready_parent.recv())
        with self.assertRaises(DriverLeaseBusyError):
            with DriverLease(self.root, "segment-b"):
                pass
        release_parent.send(True)
        process.join(5)
        self.assertEqual(0, process.exitcode)
        with DriverLease(self.root, "segment-b"):
            self.assertEqual("running", read_driver_health(self.root)["status"])

    @unittest.skipUnless(hasattr(os, "fork"), "requires POSIX flock process semantics")
    def test_sigkill_releases_lock_for_a_new_driver(self):
        context = multiprocessing.get_context("fork")
        ready_parent, ready_child = context.Pipe()
        process = context.Process(target=_crash_with_lease, args=(str(self.root), ready_child))
        process.start()
        self.assertTrue(ready_parent.poll(5))
        self.assertTrue(ready_parent.recv())
        process.kill()
        process.join(5)
        self.assertEqual(-signal.SIGKILL, process.exitcode)
        with DriverLease(self.root, "replacement"):
            snapshot = read_driver_health(self.root)
            self.assertEqual("replacement", snapshot["run_id"])
            self.assertEqual(os.getpid(), snapshot["pid"])

    @unittest.skipUnless(hasattr(os, "fork"), "requires POSIX fork semantics")
    def test_fork_worker_does_not_keep_killed_driver_lock_alive(self):
        read_descriptor, write_descriptor = os.pipe()
        driver_pid = os.fork()
        if driver_pid == 0:
            os.close(read_descriptor)
            try:
                with DriverLease(self.root, "driver-with-worker", heartbeat_interval=0.05):
                    close_inherited = runner_module._close_inherited_lease_fds

                    def delayed_close_inherited() -> None:
                        # Force the scheduling window where the parent could
                        # otherwise publish the worker before its lock FD closes.
                        time.sleep(0.2)
                        close_inherited()

                    runner_module._close_inherited_lease_fds = delayed_close_inherited
                    with warnings.catch_warnings():
                        warnings.simplefilter("ignore", DeprecationWarning)
                        try:
                            worker_pid = os.fork()
                        finally:
                            runner_module._close_inherited_lease_fds = close_inherited
                    if worker_pid == 0:
                        os.close(write_descriptor)
                        signal.pause()
                        os._exit(0)
                    os.write(write_descriptor, str(worker_pid).encode())
                    os.close(write_descriptor)
                    signal.pause()
            finally:
                os._exit(2)
        os.close(write_descriptor)
        driver_birth = process_birth(driver_pid)
        self.addCleanup(_kill_same_process, driver_pid, driver_birth)
        worker_pid = int(os.read(read_descriptor, 64))
        os.close(read_descriptor)
        worker_birth = process_birth(worker_pid)
        self.addCleanup(_kill_same_process, worker_pid, worker_birth)
        os.kill(driver_pid, signal.SIGKILL)
        waited_pid, wait_status = os.waitpid(driver_pid, 0)
        self.assertEqual(driver_pid, waited_pid)
        self.assertEqual(-signal.SIGKILL, os.waitstatus_to_exitcode(wait_status))
        self.assertTrue(process_alive(worker_pid, worker_birth))
        with DriverLease(self.root, "replacement-after-worker"):
            self.assertEqual(
                "replacement-after-worker",
                read_driver_health(self.root)["run_id"],
            )

    def test_fork_ack_timeout_marks_local_lease_unhealthy_without_blocking(self):
        lease = DriverLease(self.root, "fork-timeout", heartbeat_interval=10)
        with self.assertRaisesRegex(DriverLeaseError, "fork coordination failed"):
            with lease:
                started = time.monotonic()
                with patch.object(runner_module, "FORK_ACK_TIMEOUT", 0.01), \
                     patch("modport.runner.select.select", return_value=([], [], [])) as wait:
                    runner_module._before_fork()
                    runner_module._after_fork_parent()
                self.assertLess(time.monotonic() - started, 0.5)
                self.assertLessEqual(wait.call_args.args[3], 0.01)
                self.assertTrue(lease._stop.is_set())
                lease.check_health()
        snapshot = read_driver_health(self.root)
        self.assertEqual("failed", snapshot["status"])
        self.assertEqual("DriverLeaseError", snapshot["failure_type"])

    def test_heartbeat_is_atomic_and_exit_status_is_persisted(self):
        with DriverLease(self.root, "run", heartbeat_interval=0.02) as lease:
            self.assertIsNone(lease.check_health())
            first = read_driver_health(self.root)
            deadline = time.monotonic() + 2
            current = first
            while current["timestamp"] <= first["timestamp"] and time.monotonic() < deadline:
                time.sleep(0.01)
                current = read_driver_health(self.root)
            self.assertGreater(current["timestamp"], first["timestamp"])
            self.assertEqual("running", current["status"])
            self.assertEqual(lease.birth, current["birth"])
            self.assertEqual(str(self.root), current["run_dir"])
            self.assertEqual("run", current["run_id"])
        self.assertEqual("stopped", read_driver_health(self.root)["status"])
        leftovers = [path.name for path in lease.health_path.parent.iterdir()
                     if path.name.startswith(".monitor-driver.")]
        self.assertEqual([], leftovers)

    def test_check_health_raises_promptly_on_heartbeat_error(self):
        lease = DriverLease(self.root, "run", heartbeat_interval=0.01)
        with self.assertRaisesRegex(DriverLeaseError, "heartbeat persistence failed"):
            with lease:
                with patch("modport.runner._atomic_health", side_effect=OSError("disk failed")):
                    deadline = time.monotonic() + 2
                    while lease._heartbeat_error is None and time.monotonic() < deadline:
                        time.sleep(0.005)
                    lease.check_health()
        snapshot = read_driver_health(self.root)
        self.assertEqual("failed", snapshot["status"])
        self.assertEqual("DriverLeaseError", snapshot["failure_type"])

    def test_exception_marks_failed_without_hiding_the_exception(self):
        with self.assertRaisesRegex(RuntimeError, "body failed"):
            with DriverLease(self.root, "run", heartbeat_interval=1):
                raise RuntimeError("body failed")
        snapshot = read_driver_health(self.root)
        self.assertEqual("failed", snapshot["status"])
        self.assertEqual("RuntimeError", snapshot["failure_type"])
        with DriverLease(self.root, "next"):
            pass

    def test_lock_descriptor_is_not_inherited_by_exec_worker(self):
        with DriverLease(self.root, "run") as lease:
            self.assertFalse(os.get_inheritable(lease._descriptor))
            result = subprocess.run(
                [sys.executable, "-c", "import os,sys; os.fstat(int(sys.argv[1]))", str(lease._descriptor)],
                close_fds=False,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            self.assertNotEqual(0, result.returncode)

    def test_symlinked_root_lock_and_health_paths_are_rejected(self):
        real = self.root / "real"
        real.mkdir()
        linked = self.root / "linked"
        linked.symlink_to(real, target_is_directory=True)
        with self.assertRaises(OSError):
            with DriverLease(linked, "run"):
                pass

        lock_target = self.root / "lock-target"
        lock_target.write_text("untouched", encoding="utf-8")
        (self.root / DRIVER_LOCK_NAME).symlink_to(lock_target)
        with self.assertRaises(OSError):
            with DriverLease(self.root, "run"):
                pass
        self.assertEqual("untouched", lock_target.read_text(encoding="utf-8"))
        (self.root / DRIVER_LOCK_NAME).unlink()

        monitor = self.root / "artifacts" / "monitor"
        monitor.mkdir(parents=True)
        health_target = self.root / "health-target"
        health_target.write_text("untouched", encoding="utf-8")
        (monitor / "monitor-driver.json").symlink_to(health_target)
        with self.assertRaises(DriverHealthError):
            with DriverLease(self.root, "run"):
                pass
        self.assertEqual("untouched", health_target.read_text(encoding="utf-8"))

    def test_health_reader_rejects_malformed_or_foreign_snapshot(self):
        monitor = self.root / "artifacts" / "monitor"
        monitor.mkdir(parents=True)
        health = monitor / "monitor-driver.json"
        health.write_text("{}", encoding="utf-8")
        with self.assertRaises(DriverHealthError):
            read_driver_health(self.root)
        health.write_text(json.dumps({
            "schema_version": 1, "run_id": "run", "run_dir": "/elsewhere",
            "pid": 1, "birth": "boot:1", "timestamp": 1, "status": "running",
        }), encoding="utf-8")
        with self.assertRaises(DriverHealthError):
            read_driver_health(self.root)

    def test_health_reader_accepts_run_monitor_identity_format(self):
        monitor = self.root / "artifacts" / "monitor"
        monitor.mkdir(parents=True)
        (monitor / "monitor-driver.json").write_text(json.dumps({
            "run_id": "run", "run_dir": str(self.root),
            "pid": os.getpid(), "birth": process_birth(os.getpid()),
        }), encoding="utf-8")
        snapshot = read_driver_health(self.root)
        self.assertEqual(0, snapshot["schema_version"])
        self.assertEqual("running", snapshot["status"])
        self.assertIsInstance(snapshot["timestamp"], float)

    def test_lease_does_not_construct_or_write_sdk_state(self):
        from dispatcher_sdk.execution_kernel import Kernel
        from dispatcher_sdk.orchestrator import Orchestrator

        with patch.object(Orchestrator, "__init__", side_effect=AssertionError("SDK writer opened")), \
             patch.object(Kernel, "open_sqlite", side_effect=AssertionError("SDK kernel opened")):
            with DriverLease(self.root, "run"):
                snapshot = read_driver_health(self.root)
                self.assertEqual("running", snapshot["status"])
                self.assertEqual(pid_namespace(), snapshot.get("pid_namespace"))
        self.assertFalse((self.root / "orchestrator.sqlite3").exists())
        self.assertFalse((self.root / "kernel.sqlite3").exists())


if __name__ == "__main__":
    unittest.main()
