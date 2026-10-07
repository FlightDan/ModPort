import json
import tempfile
import unittest
from pathlib import Path

from modport.execution_progress import (
    mark_current_execution_progress, progress_path, read_execution_progress,
    record_command_progress, track_execution_progress, write_execution_progress,
)
from modport.contracts import OperationInput
from modport.run_monitor import _execution_progress_projection


class ExecutionProgressTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.identity = {
            "run_id": "run-1", "task_id": "coder-1", "stage_id": "coder",
            "command_id": "run-1:coder-1:1", "attempt": 1,
        }

    def test_host_and_worker_stages_share_one_execution_identity(self):
        self.assertTrue(write_execution_progress(
            self.root, self.identity, "planned", registry_revision="r1", now=100))
        self.assertTrue(write_execution_progress(
            self.root, self.identity, "dispatched", registry_revision="r1", now=101))
        self.assertTrue(write_execution_progress(
            self.root, self.identity, "worker_entered", kernel_attempt=2, fence=7,
            registry_revision="r1", now=102))
        # Dispatch can execute immediately after SDK commit; a delayed host
        # post-commit write must not regress the worker marker.
        self.assertTrue(write_execution_progress(
            self.root, self.identity, "dispatched", registry_revision="r1", now=103))

        record = read_execution_progress(self.root, self.identity["command_id"])
        self.assertEqual("worker_entered", record["phase"])
        self.assertEqual(2, record["kernel_attempt"])
        self.assertEqual(7, record["fence"])
        self.assertEqual("r1", record["registry_revision"])
        self.assertEqual(
            ["planned", "dispatched", "worker_entered"],
            [transition["phase"] for transition in record["transitions"]],
        )

    def test_identity_cannot_be_rebound_and_transitions_remain_bounded(self):
        write_execution_progress(self.root, self.identity, "worker_entered", now=100)
        changed = {**self.identity, "task_id": "other-task"}
        self.assertFalse(write_execution_progress(self.root, changed, "handler_entered", now=101))

        phases = ["input_loading", "input_ready", "restoring_inputs", "waiting_memory",
                  "waiting_workspace", "handler_entered", "model_started", "settling",
                  "finished", "failed", "planned", "dispatched", "worker_entered"]
        for index, phase in enumerate(phases, start=102):
            write_execution_progress(self.root, self.identity, phase, now=index)
        record = read_execution_progress(self.root, self.identity["command_id"])
        self.assertLessEqual(len(record["transitions"]), 12)

    def test_wait_observations_are_sampled_and_monitor_marks_stalls(self):
        write_execution_progress(self.root, self.identity, "dispatched", now=100)
        rows = {"summaries": [{"execution_id": self.identity["command_id"],
                               "task_id": "coder-1", "application_attempt": 0,
                               "attempt": 1, "fence": 2, "state": "running"}]}
        projection = _execution_progress_projection(self.root, rows, 130)
        self.assertEqual("startup_unconfirmed", projection[0]["phase"])

        write_execution_progress(self.root, self.identity, "waiting_memory", kernel_attempt=1,
            fence=2, wait={"reason": "memory_pressure", "required_bytes": 2048,
                           "reserved_bytes": 512, "sampled_at": 200, "stage": "coder"},
            now=200)
        for tick in range(8):
            write_execution_progress(self.root, self.identity, "waiting_memory", kernel_attempt=1,
                fence=2, wait={"reason": "memory_pressure", "required_bytes": 2048,
                               "reserved_bytes": 512, "sampled_at": 201 + tick,
                               "stage": "coder"}, now=201 + tick)
        record = read_execution_progress(self.root, self.identity["command_id"])
        self.assertLessEqual(len(record["wait_samples"]), 8)
        projection = _execution_progress_projection(self.root, rows, 270)
        self.assertEqual("no_observed_progress", projection[0]["state"])
        self.assertEqual("memory_pressure", projection[0]["wait"]["reason"])

        mismatched = {"summaries": [{**rows["summaries"][0], "application_attempt": 1}]}
        projection = _execution_progress_projection(self.root, mismatched, 270)
        self.assertEqual("progress_identity_mismatch", projection[0]["phase"])

    def test_reader_rejects_symlink_and_nonfinite_timestamp(self):
        path = progress_path(self.root, self.identity["command_id"])
        path.parent.mkdir(parents=True)
        other = self.root / "other.json"
        other.write_text("{}")
        path.symlink_to(other)
        self.assertIsNone(read_execution_progress(self.root, self.identity["command_id"]))

        path.unlink()
        write_execution_progress(self.root, self.identity, "planned", now=100)
        content = json.loads(path.read_text())
        content["last_progress_at"] = float("nan")
        path.write_text(json.dumps(content))
        self.assertIsNone(read_execution_progress(self.root, self.identity["command_id"]))

    def test_command_progress_is_disabled_for_frozen_v21_and_enabled_for_v22(self):
        marker = self.root / "run.json"
        payload = {**self.identity, "run_dir": str(self.root),
                   "options": {"workflow_version": 21}}
        command = {"payload": payload, "registry_revision": "r1"}
        marker.write_text(json.dumps({"definition": {"workflow_version": 21}}))
        self.assertFalse(record_command_progress(
            self.root, command, "worker_entered", kernel_attempt=1, fence=2))
        self.assertIsNone(read_execution_progress(self.root, self.identity["command_id"]))

        marker.write_text(json.dumps({"definition": {"workflow_version": 22}}))
        command["payload"] = {**payload, "options": {"workflow_version": 22}}
        self.assertTrue(record_command_progress(
            self.root, command, "worker_entered", kernel_attempt=1, fence=2))
        self.assertEqual("worker_entered", read_execution_progress(
            self.root, self.identity["command_id"])["phase"])

    def test_v21_tracking_cannot_fail_execution_on_strict_v22_marker(self):
        from types import SimpleNamespace

        self.identity["run_dir"] = str(self.root)
        operation = OperationInput(
            "run-1", "coder-1", "coder", self.identity["command_id"],
            str(self.root), options={"workflow_version": 21},
        )
        (self.root / "run.json").write_text(json.dumps({
            "definition": {"workflow_version": 21},
        }))
        context = SimpleNamespace(lease=SimpleNamespace(attempt=1, fence=2))
        with track_execution_progress(self.root, operation, context, "r1"):
            self.assertFalse(mark_current_execution_progress(
                "handler_entered", strict=True))
        self.assertIsNone(read_execution_progress(self.root, self.identity["command_id"]))

    def test_v25_records_worker_and_handler_progress_without_changing_frozen_v23_v24(self):
        from types import SimpleNamespace

        marker = self.root / "run.json"
        payload = {**self.identity, "run_dir": str(self.root)}
        command = {"payload": payload, "registry_revision": "r1"}
        for version in (23, 24):
            marker.write_text(json.dumps({"definition": {"workflow_version": version}}))
            command["payload"] = {**payload, "options": {"workflow_version": version}}
            self.assertFalse(record_command_progress(
                self.root, command, "worker_entered", kernel_attempt=1, fence=2))
        self.assertIsNone(read_execution_progress(self.root, self.identity["command_id"]))

        marker.write_text(json.dumps({"definition": {"workflow_version": 25}}))
        command["payload"] = {**payload, "options": {"workflow_version": 25}}
        self.assertTrue(record_command_progress(
            self.root, command, "worker_entered", kernel_attempt=1, fence=2))
        operation = OperationInput(
            "run-1", "coder-1", "coder", self.identity["command_id"],
            str(self.root), options={"workflow_version": 25},
        )
        context = SimpleNamespace(lease=SimpleNamespace(attempt=1, fence=2))
        with track_execution_progress(self.root, operation, context, "r1"):
            self.assertTrue(mark_current_execution_progress("handler_entered", strict=True))
        progress = read_execution_progress(self.root, self.identity["command_id"])
        self.assertEqual("handler_entered", progress["phase"])
        self.assertEqual((1, 2), (progress["kernel_attempt"], progress["fence"]))

    def test_writer_refuses_a_symlinked_progress_directory(self):
        run = self.root / "run"
        run.mkdir()
        artifacts = run / "artifacts"
        artifacts.mkdir()
        outside = self.root / "outside"
        outside.mkdir()
        (artifacts / "execution-progress").symlink_to(outside, target_is_directory=True)
        self.assertFalse(write_execution_progress(run, self.identity, "planned", now=100))
        self.assertEqual([], list(outside.iterdir()))


if __name__ == "__main__":
    unittest.main()
