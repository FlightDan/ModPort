import json
import csv
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from modport.audit_export import AuditExportError, export_isolated
from modport.operations import MigrationOperations
from modport.telemetry import record_event


class AuditExportTests(unittest.TestCase):
    def test_separate_export_preserves_complete_small_event(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            record_event(root, "one", "usage", {"input_tokens": 7})
            paths = export_isolated(root, memory_mib=256)
            report = json.loads(paths["json"].read_text())
            self.assertEqual("one", report["events"][0]["event_id"])
            self.assertEqual(7, report["events"][0]["payload"]["input_tokens"])
            self.assertTrue(all(path.is_file() for path in paths.values()))

    def test_large_inline_event_exports_complete_csv_with_small_child_limit(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            payload = 'x,"\n' * (8 * 1024 * 1024)
            with sqlite3.connect(root / "audit.sqlite3") as connection:
                connection.execute("CREATE TABLE events (event_id TEXT PRIMARY KEY, "
                                   "timestamp TEXT, kind TEXT, data TEXT)")
                connection.execute("INSERT INTO events VALUES ('large','now','sdk.event',?)",
                                   (json.dumps({"payload": {"text": payload}}),))
            paths = export_isolated(root, memory_mib=256, timeout=60)
            previous = csv.field_size_limit(128 * 1024 * 1024)
            try:
                with paths["csv"].open(newline="") as file:
                    row = next(csv.DictReader(file))
                self.assertEqual(payload, json.loads(row["payload"])["text"])
            finally:
                csv.field_size_limit(previous)

    def test_report_memory_failure_is_contained_in_child(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            # SQLite allocates this on disk, without constructing it in Python.
            with sqlite3.connect(root / "audit.sqlite3") as connection:
                connection.execute("CREATE TABLE events (event_id TEXT PRIMARY KEY, "
                                   "timestamp TEXT, kind TEXT, data TEXT)")
                connection.execute("INSERT INTO events VALUES ('huge','now','sdk.event',?)",
                                   ("{}",))
                connection.execute("UPDATE events SET data=zeroblob(?)", (160 * 1024 * 1024,))
            with self.assertRaises(AuditExportError):
                export_isolated(root, memory_mib=128, timeout=30)
            # The parent remains usable and original evidence stays present.
            with sqlite3.connect(root / "audit.sqlite3") as connection:
                self.assertEqual(160 * 1024 * 1024,
                                 connection.execute("SELECT length(data) FROM events").fetchone()[0])

    def test_export_failure_is_reported_without_raising_into_run(self):
        with patch("modport.operations.export_isolated",
                   side_effect=AuditExportError("memory limit reached")):
            with self.assertWarnsRegex(RuntimeWarning, "memory limit reached"):
                MigrationOperations._export_audit("unused")


if __name__ == "__main__":
    unittest.main()
