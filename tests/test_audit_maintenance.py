import gzip
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from modport import audit_maintenance
from modport.audit_maintenance import (
    AuditCompactionError,
    apply_compaction,
    prepare_compaction,
)
from modport.audit_storage import INLINE_LIMIT, inspect_data_reference, load_data, store_data
from modport.cli import main, parser


class AuditMaintenanceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.database = self.root / "audit.sqlite3"
        connection = sqlite3.connect(self.database)
        connection.execute(
            "CREATE TABLE events (event_id TEXT PRIMARY KEY, timestamp TEXT NOT NULL, "
            "kind TEXT NOT NULL, data TEXT NOT NULL)"
        )
        connection.close()

    def insert(self, event_id, kind, data, timestamp=None):
        serialized = data if isinstance(data, str) else json.dumps(data, ensure_ascii=False)
        timestamp = timestamp or f"2026-09-13T00:00:{event_id[-2:].zfill(2)}Z"
        connection = sqlite3.connect(self.database, timeout=10)
        try:
            connection.execute(
                "INSERT INTO events VALUES (?,?,?,?)",
                (event_id, timestamp, kind, serialized),
            )
            connection.commit()
        finally:
            connection.close()
        return serialized

    def rows(self):
        connection = sqlite3.connect(self.database)
        try:
            return connection.execute(
                "SELECT event_id,timestamp,kind,data FROM events ORDER BY event_id"
            ).fetchall()
        finally:
            connection.close()

    def fixture(self):
        small = self.insert("01", "sdk.event", {"payload": {"body": "small"}})
        first = self.insert(
            "02", "sdk.event",
            '{  "run_id" : "run", "payload" : {"body":"'
            + "x" * INLINE_LIMIT + '"} }\n',
        )
        duplicate = self.insert("03", "sdk.event", first)
        other = self.insert(
            "04", "model.event", {"payload": {"body": "y" * INLINE_LIMIT}}
        )
        already_data = {"run_id": "run", "payload": {"body": "z" * INLINE_LIMIT}}
        already = self.insert("05", "sdk.event", store_data(self.root, already_data))
        return {"01": small, "02": first, "03": duplicate, "04": other, "05": already}

    def test_prepare_keeps_database_unchanged_and_publishes_verified_plan_last(self):
        original = self.fixture()
        before = self.database.read_bytes()
        before_rows = self.rows()
        manifest_path = prepare_compaction(self.root, self.root / "plan")

        self.assertEqual(self.database.read_bytes(), before)
        self.assertEqual(self.rows(), before_rows)
        manifest = json.loads(manifest_path.read_text())
        self.assertEqual(manifest["stats"]["source_count"], 5)
        self.assertEqual(manifest["stats"]["selected_count"], 2)
        self.assertEqual(manifest["stats"]["original_bytes"],
                         2 * len(original["02"].encode()))
        self.assertLess(manifest["stats"]["new_inline_bytes"],
                        manifest["stats"]["original_bytes"])
        self.assertEqual(manifest["stats"]["blob_physical_bytes"],
                         inspect_data_reference(
                             json.loads((self.root / manifest["updates"]["path"])
                                        .read_text().splitlines()[0])["new_data"]
                         )["gzip_size"])
        self.assertEqual(manifest["stats"]["archive_bytes"],
                         (self.root / manifest["backup"]["path"]).stat().st_size)
        self.assertEqual(manifest["updates"]["count"], 2)
        with gzip.open(self.root / manifest["backup"]["path"], "rb") as archive:
            backup_raw = archive.read()
        self.assertEqual(hashlib.sha256(backup_raw).hexdigest(),
                         manifest["backup"]["raw_sha256"])
        self.assertEqual(len(backup_raw), manifest["backup"]["raw_size"])
        self.assertFalse(any((self.root / "plan").glob(".audit-snapshot-*")))

    def test_prepare_selects_exactly_at_64_kib_boundary(self):
        prefix = '{"payload":"'
        suffix = '"}'
        overhead = len((prefix + suffix).encode())
        below = prefix + "x" * (INLINE_LIMIT - overhead - 1) + suffix
        boundary = prefix + "x" * (INLINE_LIMIT - overhead) + suffix
        self.assertEqual(len(below.encode()), INLINE_LIMIT - 1)
        self.assertEqual(len(boundary.encode()), INLINE_LIMIT)
        self.insert("01", "sdk.event", below)
        self.insert("02", "sdk.event", boundary)

        manifest_path = prepare_compaction(self.root, self.root / "plan")
        manifest = json.loads(manifest_path.read_text())
        self.assertEqual(manifest["stats"]["source_count"], 2)
        self.assertEqual(manifest["stats"]["selected_count"], 1)
        update = json.loads((self.root / manifest["updates"]["path"]).read_text())
        self.assertEqual(update["event_id"], "02")

    def test_manifest_is_published_after_plaintext_snapshot_removal(self):
        self.fixture()
        original_atomic_json = audit_maintenance.atomic_json

        def assert_snapshot_gone(path, document):
            self.assertEqual(list(path.parent.glob(".audit-snapshot-*")), [])
            return original_atomic_json(path, document)

        with patch.object(audit_maintenance, "atomic_json", new=assert_snapshot_gone):
            manifest = prepare_compaction(self.root, self.root / "plan")
        self.assertTrue(manifest.is_file())

    def test_apply_round_trips_exact_old_values_and_preserves_row_identity(self):
        original = self.fixture()
        identities = [(row[0], row[1], row[2]) for row in self.rows()]
        inode = self.database.stat().st_ino
        manifest_path = prepare_compaction(self.root, self.root / "plan")
        report = apply_compaction(self.root, manifest_path)

        self.assertEqual(report["status"], "applied")
        self.assertEqual(report["applied_count"], 2)
        self.assertEqual(report["selected_count"], 2)
        self.assertTrue(report["backup_retained"])
        self.assertTrue(Path(report["backup_path"]).is_file())
        self.assertEqual(self.database.stat().st_ino, inode)
        rows = self.rows()
        self.assertEqual([(row[0], row[1], row[2]) for row in rows], identities)
        for event_id, _, _, serialized in rows:
            if event_id in {"02", "03"}:
                self.assertIsNotNone(inspect_data_reference(serialized))
                restored = load_data(self.root, serialized)
                self.assertEqual(restored, json.loads(original[event_id]))
                reference = inspect_data_reference(serialized)
                with gzip.open(self.root / reference["path"], "rb") as blob:
                    self.assertEqual(blob.read(), original[event_id].encode())
            else:
                self.assertEqual(serialized, original[event_id])

    def test_second_apply_is_idempotently_recognized(self):
        self.fixture()
        manifest = prepare_compaction(self.root, self.root / "plan")
        first = apply_compaction(self.root, manifest)
        rows = self.rows()
        second = apply_compaction(self.root, manifest)
        self.assertEqual(first["status"], "applied")
        self.assertEqual(second["status"], "already_applied")
        self.assertEqual(second["applied_count"], 0)
        self.assertEqual(self.rows(), rows)

    def test_retry_after_vacuum_failure_finishes_physical_compaction(self):
        self.fixture()
        manifest = prepare_compaction(self.root, self.root / "plan")
        real_connect = sqlite3.connect

        class VacuumFailure:
            def __init__(self, connection):
                self.connection = connection

            def execute(self, statement, *args, **kwargs):
                if statement == "VACUUM":
                    raise sqlite3.OperationalError("simulated vacuum failure")
                return self.connection.execute(statement, *args, **kwargs)

            def __getattr__(self, name):
                return getattr(self.connection, name)

        with patch.object(
                audit_maintenance.sqlite3, "connect",
                side_effect=lambda *args, **kwargs: VacuumFailure(
                    real_connect(*args, **kwargs))):
            first = apply_compaction(self.root, manifest)
        self.assertEqual(first["status"], "applied")
        self.assertEqual(first["vacuum"]["status"], "failed")
        self.assertEqual(len(self.rows()), 5)

        retry = apply_compaction(self.root, manifest)
        self.assertEqual(retry["status"], "already_applied")
        self.assertEqual(retry["vacuum"]["status"], "completed")
        self.assertEqual(len(self.rows()), 5)

    def test_source_change_aborts_without_mutation(self):
        self.fixture()
        manifest = prepare_compaction(self.root, self.root / "plan")
        self.insert("06", "sdk.event", {"payload": {"body": "late"}})
        before = self.rows()
        with self.assertRaisesRegex(AuditCompactionError, "changed after preparation"):
            apply_compaction(self.root, manifest)
        self.assertEqual(self.rows(), before)

    def test_missing_or_corrupt_backup_aborts_before_database_write(self):
        for mode in ("missing", "compressed", "raw"):
            with self.subTest(mode=mode):
                with tempfile.TemporaryDirectory() as directory:
                    case = AuditMaintenanceTests(methodName="runTest")
                    case.root = Path(directory)
                    case.database = case.root / "audit.sqlite3"
                    connection = sqlite3.connect(case.database)
                    connection.execute(
                        "CREATE TABLE events (event_id TEXT PRIMARY KEY, timestamp TEXT NOT NULL, "
                        "kind TEXT NOT NULL, data TEXT NOT NULL)"
                    )
                    connection.execute("INSERT INTO events VALUES (?,?,?,?)", (
                        "01", "time", "sdk.event",
                        json.dumps({"payload": {"body": "x" * INLINE_LIMIT}}),
                    ))
                    connection.commit()
                    connection.close()
                    manifest_path = prepare_compaction(case.root, case.root / "plan")
                    manifest = json.loads(manifest_path.read_text())
                    backup = case.root / manifest["backup"]["path"]
                    if mode == "missing":
                        backup.unlink()
                    elif mode == "compressed":
                        backup.write_bytes(backup.read_bytes()[:-1] + b"0")
                    else:
                        manifest["backup"]["raw_sha256"] = "0" * 64
                        manifest_path.write_text(json.dumps(manifest))
                    before = case.rows()
                    with self.assertRaises(AuditCompactionError):
                        apply_compaction(case.root, manifest_path)
                    self.assertEqual(case.rows(), before)

    def test_corrupt_or_missing_blob_aborts_before_database_write(self):
        for mode in ("missing", "corrupt"):
            with self.subTest(mode=mode):
                with tempfile.TemporaryDirectory() as directory:
                    root = Path(directory)
                    database = root / "audit.sqlite3"
                    connection = sqlite3.connect(database)
                    connection.execute(
                        "CREATE TABLE events (event_id TEXT PRIMARY KEY, timestamp TEXT NOT NULL, "
                        "kind TEXT NOT NULL, data TEXT NOT NULL)"
                    )
                    original = json.dumps({"payload": {"body": "x" * INLINE_LIMIT}})
                    connection.execute("INSERT INTO events VALUES (?,?,?,?)",
                                       ("01", "time", "sdk.event", original))
                    connection.commit()
                    connection.close()
                    manifest_path = prepare_compaction(root, root / "plan")
                    update = json.loads((root / "plan" / "updates.jsonl").read_text())
                    reference = inspect_data_reference(update["new_data"])
                    blob = root / reference["path"]
                    if mode == "missing":
                        blob.unlink()
                    else:
                        content = blob.read_bytes()
                        blob.write_bytes(content[:-1] + bytes([content[-1] ^ 1]))
                    before = database.read_bytes()
                    with self.assertRaises(ValueError):
                        apply_compaction(root, manifest_path)
                    self.assertEqual(database.read_bytes(), before)

    def test_blob_removed_after_preflight_rolls_back_without_updates(self):
        original = self.fixture()
        manifest = prepare_compaction(self.root, self.root / "plan")
        original_preflight = audit_maintenance._preflight_updates

        def remove_after_preflight(root, path, count):
            original_preflight(root, path, count)
            update = json.loads(path.read_text().splitlines()[0])
            reference = inspect_data_reference(update["new_data"])
            (root / reference["path"]).unlink()

        with patch.object(audit_maintenance, "_preflight_updates", new=remove_after_preflight):
            with self.assertRaises(ValueError):
                apply_compaction(self.root, manifest)
        rows = {row[0]: row[3] for row in self.rows()}
        self.assertEqual(rows, original)

    def test_oversized_update_line_is_rejected_before_transaction(self):
        self.fixture()
        manifest_path = prepare_compaction(self.root, self.root / "plan")
        manifest = json.loads(manifest_path.read_text())
        updates = self.root / manifest["updates"]["path"]
        content = b"x" * 1025 + b"\n"
        updates.write_bytes(content)
        manifest["updates"]["size"] = len(content)
        manifest["updates"]["sha256"] = hashlib.sha256(content).hexdigest()
        manifest_path.write_text(json.dumps(manifest))
        before = self.rows()
        with patch.object(audit_maintenance, "_MAX_UPDATE_LINE_BYTES", 1024):
            with self.assertRaisesRegex(AuditCompactionError, "oversized or incomplete"):
                apply_compaction(self.root, manifest_path)
        self.assertEqual(self.rows(), before)

    def test_prepare_never_publishes_a_plan_it_cannot_parse(self):
        self.fixture()
        plan = self.root / "plan"
        with patch.object(audit_maintenance, "_MAX_UPDATE_LINE_BYTES", 256):
            with self.assertRaisesRegex(AuditCompactionError, "generated update entry"):
                prepare_compaction(self.root, plan)
        self.assertFalse((plan / "manifest.json").exists())

    def test_manifest_and_artifact_paths_cannot_escape_root_or_use_symlinks(self):
        self.fixture()
        manifest_path = prepare_compaction(self.root, self.root / "plan")
        outside = Path(self.temporary.name).parent / ("outside-" + next(tempfile._get_candidate_names()))
        try:
            outside.write_text(manifest_path.read_text())
            with self.assertRaises(AuditCompactionError):
                apply_compaction(self.root, outside)

            manifest = json.loads(manifest_path.read_text())
            manifest["updates"]["path"] = "../outside.jsonl"
            manifest_path.write_text(json.dumps(manifest))
            with self.assertRaises(AuditCompactionError):
                apply_compaction(self.root, manifest_path)

            manifest_path.unlink()
            manifest_path.symlink_to(outside)
            with self.assertRaises(AuditCompactionError):
                apply_compaction(self.root, manifest_path)
        finally:
            outside.unlink(missing_ok=True)

    def test_backup_updates_and_blob_symlinks_are_rejected(self):
        self.fixture()
        manifest_path = prepare_compaction(self.root, self.root / "plan")
        manifest = json.loads(manifest_path.read_text())
        update = json.loads(
            (self.root / manifest["updates"]["path"]).read_text().splitlines()[0]
        )
        reference = inspect_data_reference(update["new_data"])
        paths = {
            "backup": self.root / manifest["backup"]["path"],
            "updates": self.root / manifest["updates"]["path"],
            "blob": self.root / reference["path"],
        }
        before = self.rows()
        for name, path in paths.items():
            with self.subTest(name=name):
                real = path.with_name(path.name + ".real")
                path.rename(real)
                path.symlink_to(real.name)
                try:
                    with self.assertRaises(ValueError):
                        apply_compaction(self.root, manifest_path)
                    self.assertEqual(self.rows(), before)
                finally:
                    path.unlink()
                    real.rename(path)

    def test_prepare_rejects_output_through_symlinked_parent(self):
        self.fixture()
        outside = Path(self.temporary.name).parent / ("outside-" + next(tempfile._get_candidate_names()))
        outside.mkdir()
        try:
            (self.root / "linked").symlink_to(outside, target_is_directory=True)
            with self.assertRaises(AuditCompactionError):
                prepare_compaction(self.root, self.root / "linked" / "plan")
            self.assertFalse((outside / "plan").exists())
        finally:
            outside.rmdir()

    def test_replaced_database_inode_aborts(self):
        self.fixture()
        manifest = prepare_compaction(self.root, self.root / "plan")
        rows = self.rows()
        replacement = self.root / "replacement.sqlite3"
        replacement.write_bytes(self.database.read_bytes())
        replacement.replace(self.database)
        with self.assertRaisesRegex(AuditCompactionError, "inode differs"):
            apply_compaction(self.root, manifest)
        self.assertEqual(self.rows(), rows)

    def test_incomplete_prepare_without_manifest_cannot_apply(self):
        self.fixture()
        plan = self.root / "partial"
        plan.mkdir()
        (plan / "updates.jsonl").write_text("partial")
        with self.assertRaises(AuditCompactionError):
            apply_compaction(self.root, plan / "manifest.json")

    def test_failed_snapshot_removes_plaintext_temporary_database(self):
        self.fixture()
        plan = self.root / "plan"
        with patch.object(
                audit_maintenance, "_validate_events_table",
                side_effect=AuditCompactionError("invalid schema")):
            with self.assertRaises(AuditCompactionError):
                prepare_compaction(self.root, plan)
        self.assertEqual(list(plan.glob(".audit-snapshot-*")), [])
        self.assertFalse((plan / "manifest.json").exists())

    def test_failed_update_preparation_removes_sensitive_temporaries(self):
        self.fixture()
        plan = self.root / "plan"
        with patch.object(audit_maintenance, "store_raw", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                prepare_compaction(self.root, plan)
        self.assertEqual(list(plan.glob(".audit-snapshot-*")), [])
        self.assertEqual(list(plan.glob(".audit-updates-*")), [])
        self.assertEqual(list(plan.glob(".audit-blob-stats-*")), [])
        self.assertFalse((plan / "manifest.json").exists())

    def test_append_after_apply_uses_same_live_database_file(self):
        self.fixture()
        manifest = prepare_compaction(self.root, self.root / "plan")
        inode = self.database.stat().st_ino
        apply_compaction(self.root, manifest)

        errors = []
        thread = threading.Thread(
            target=lambda: self._append_from_thread(errors), daemon=True
        )
        thread.start()
        thread.join(10)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(self.database.stat().st_ino, inode)
        self.assertEqual(self.rows()[-1][0], "99")

    def test_concurrent_append_waits_for_transaction_then_resumes_on_same_file(self):
        self.fixture()
        manifest = prepare_compaction(self.root, self.root / "plan")
        inode = self.database.stat().st_ino
        transaction_open = threading.Event()
        release_transaction = threading.Event()
        apply_errors = []
        writer_errors = []
        original_apply = audit_maintenance._apply_updates

        def paused_apply(*args, **kwargs):
            transaction_open.set()
            if not release_transaction.wait(5):
                raise AssertionError("test did not release compaction transaction")
            return original_apply(*args, **kwargs)

        def compact():
            try:
                apply_compaction(self.root, manifest)
            except Exception as exc:
                apply_errors.append(exc)

        with patch.object(audit_maintenance, "_apply_updates", new=paused_apply):
            compactor = threading.Thread(target=compact, daemon=True)
            compactor.start()
            self.assertTrue(transaction_open.wait(5))
            writer = threading.Thread(
                target=lambda: self._append_from_thread(writer_errors), daemon=True
            )
            writer.start()
            time.sleep(0.1)
            self.assertTrue(writer.is_alive())
            release_transaction.set()
            compactor.join(10)
            writer.join(10)

        self.assertFalse(compactor.is_alive())
        self.assertFalse(writer.is_alive())
        self.assertEqual(apply_errors, [])
        self.assertEqual(writer_errors, [])
        self.assertEqual(self.database.stat().st_ino, inode)
        self.assertEqual(self.rows()[-1][0], "99")

    def _append_from_thread(self, errors):
        try:
            from modport.telemetry import record_event
            if not record_event(
                    self.root, "99", "sdk.event", {"body": "new"}, strict=True):
                raise AssertionError("telemetry writer did not insert its event")
        except Exception as exc:
            errors.append(exc)


class AuditMaintenanceCliTests(unittest.TestCase):
    def test_parser_exposes_separate_prepare_and_apply_commands(self):
        prepare = parser().parse_args([
            "audit-compact-prepare", "--run-dir", "/run", "--output-dir", "/run/plan",
        ])
        apply = parser().parse_args([
            "audit-compact-apply", "--run-dir", "/run", "--manifest", "/run/plan/manifest.json",
        ])
        self.assertEqual(prepare.command, "audit-compact-prepare")
        self.assertEqual(prepare.output_dir, "/run/plan")
        self.assertEqual(apply.command, "audit-compact-apply")
        self.assertEqual(apply.manifest, "/run/plan/manifest.json")

    def test_prepare_command_prints_reviewable_paths_and_stats(self):
        manifest = {
            "backup": {"path": "plan/backup.gz"},
            "updates": {"path": "plan/updates.jsonl"},
            "source": {"logical_sha256": "a" * 64, "row_count": 2},
            "result": {"logical_sha256": "b" * 64, "row_count": 2},
            "stats": {"source_count": 2, "selected_count": 1},
        }
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            plan = root / "plan"
            plan.mkdir()
            manifest_path = plan / "manifest.json"
            manifest_path.write_text(json.dumps(manifest))
            with patch(
                    "modport.audit_maintenance.prepare_compaction",
                    return_value=manifest_path) as prepare, patch("builtins.print") as output:
                self.assertEqual(main([
                    "audit-compact-prepare", "--run-dir", str(root),
                    "--output-dir", str(plan),
                ]), 0)
            prepare.assert_called_once_with(str(root), str(plan))
            printed = json.loads(output.call_args.args[0])
            self.assertEqual(printed["status"], "prepared")
            self.assertEqual(printed["manifest_path"], str(manifest_path))
            self.assertEqual(printed["backup_path"], str(root / "plan/backup.gz"))
            self.assertEqual(printed["stats"], manifest["stats"])

    def test_apply_command_prints_maintenance_report(self):
        report = {
            "status": "already_applied", "applied_count": 0,
            "backup_path": "/run/plan/backup.gz",
        }
        with patch(
                "modport.audit_maintenance.apply_compaction",
                return_value=report) as apply, patch("builtins.print") as output:
            self.assertEqual(main([
                "audit-compact-apply", "--run-dir", "/run",
                "--manifest", "/run/plan/manifest.json",
            ]), 0)
        apply.assert_called_once_with("/run", "/run/plan/manifest.json")
        self.assertEqual(json.loads(output.call_args.args[0]), report)


if __name__ == "__main__":
    unittest.main()
