"""Status reads persisted authority without requiring the execution deployment."""

from copy import deepcopy
from hashlib import sha256
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from modport import MigrationOperations, MigrationRequest
from modport.evidence import atomic_json, read_json
from modport.sdk_compat import SDKCompatibilityError
from fixtures_modport import registry


class ReadOnlyStatusTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "run"
        self.operations = MigrationOperations(handlers=registry(), isolation_mode="thread")
        request = MigrationRequest("example", "https://example.invalid/mod.git", "1.20.1", "26.1.2",
                                   source_revision="a" * 40)
        self.run = self.operations.submit(request, run_dir=self.root, run_id="status-test")

    def files(self):
        # SQLite read-only connections may create SHM and empty WAL coordination
        # files. They must not change persisted database/evidence/audit contents.
        return {str(path.relative_to(self.root)): (sha256(path.read_bytes()).hexdigest(), path.stat().st_mtime_ns)
                for path in self.root.rglob("*") if path.is_file()
                and not path.name.endswith("-shm")
                and not (path.name.endswith("-wal") and path.stat().st_size == 0)}

    def test_changed_deployment_reads_running_state_without_writers_or_audit(self):
        before = self.files()
        changed = MigrationOperations(handlers=registry(fail_stage="source"), isolation_mode="thread")
        with patch("modport.operations.open_runtime", side_effect=AssertionError("live writer")), \
                patch("modport.operations.compile_migration_workflow", side_effect=AssertionError("new rules")), \
                patch.object(changed, "_audit_action", side_effect=AssertionError("audit write")):
            result = changed.status(self.root, self.run.run_id)
        self.assertEqual(result.status, "running")
        self.assertEqual(result.snapshot["run_id"], self.run.run_id)
        self.assertEqual(result.snapshot["state"], self.run.snapshot["state"])
        self.assertEqual(result.snapshot["status_summary"]["source"],
                         "dispatcher-sdk public summary and work-availability APIs")
        self.assertNotIn("tasks", result.snapshot)
        self.assertEqual(before, self.files())

    def test_status_reads_committed_state_while_runtime_is_open(self):
        with self.operations.session(self.root, self.run.run_id) as (_, header, runtime, sdk):
            state = self.operations.tick(sdk, header)
            result = self.operations.status(self.root, self.run.run_id)
            self.assertEqual(result.snapshot["status_summary"]["revision"], state["revision"])
            self.assertGreater(result.snapshot["status_summary"]["task_count"], 0)
            self.assertNotIn("tasks", result.snapshot)
            self.assertIn("source", result.snapshot["status_summary"]["current_wait"]["planned_task_ids"])

    def test_modified_header_is_rejected_without_source_writes(self):
        header = read_json(self.root / "run.json")
        header["request"]["budget"]["max_rework_rounds"] += 1
        atomic_json(self.root / "run.json", header)
        before = self.files()
        with self.assertRaisesRegex(ValueError, "authoritative SDK Run disagrees"):
            self.operations.status(self.root, self.run.run_id, detail=True)
        self.assertEqual(before, self.files())

    def test_definition_mismatch_is_rejected(self):
        state = deepcopy(self.run.snapshot)
        state["definition"] = {"changed": True}
        with patch("modport.operations.Orchestrator.get_run", return_value=state):
            with self.assertRaisesRegex(ValueError, "authoritative SDK Run disagrees"):
                self.operations.status(self.root, self.run.run_id, detail=True)

    def test_missing_initial_evidence_is_rejected(self):
        header = read_json(self.root / "run.json")
        evidence = self.root / header["initial_refs"]["acceptance_rubric"]["path"]
        evidence.unlink()
        with self.assertRaises(ValueError):
            self.operations.status(self.root, self.run.run_id)

    def test_unrecognized_storage_is_rejected_before_sdk_constructor(self):
        # A valid SQLite file without the declared SDK component is not a Run store.
        import sqlite3
        for name in ("kernel.sqlite3", "orchestrator.sqlite3"):
            with self.subTest(name=name):
                original = (self.root / name).read_bytes()
                try:
                    (self.root / name).unlink()
                    connection = sqlite3.connect(self.root / name)
                    connection.execute("CREATE TABLE unrelated (value TEXT)")
                    connection.close()
                    before = self.files()
                    with patch("modport.operations.Orchestrator", side_effect=AssertionError("unchecked store")):
                        with self.assertRaises(SDKCompatibilityError):
                            self.operations.status(self.root, self.run.run_id)
                    self.assertEqual(before, self.files())
                finally:
                    (self.root / name).write_bytes(original)

    def test_missing_frozen_sdk_identity_is_rejected(self):
        header = read_json(self.root / "run.json")
        del header["sdk_identity"]
        atomic_json(self.root / "run.json", header)
        with self.assertRaisesRegex(ValueError, "supported SDK identity"):
            self.operations.status(self.root, self.run.run_id)


if __name__ == "__main__":
    unittest.main()
