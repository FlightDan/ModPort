"""Storage admission is bounded, read-only and independent of SDK writers."""

from hashlib import sha256
from pathlib import Path
import sqlite3
import stat
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

from modport import storage_budget as storage


MIB = 1024 * 1024
GIB = 1024 * MIB


class StoragePolicyTests(unittest.TestCase):
    def test_defaults_and_serialization(self):
        policy = storage.StoragePolicy()
        self.assertEqual(policy.sdk_limit_bytes, 512 * MIB)
        self.assertEqual(policy.sdk_safety_bytes, 64 * MIB)
        self.assertEqual(policy.run_soft_limit_bytes, 20 * GIB)
        self.assertEqual(policy.min_free_bytes, 2 * GIB)
        self.assertEqual(storage.StoragePolicy(**policy.to_dict()), policy)

    def test_environment_accepts_larger_finite_positive_limits(self):
        policy = storage.StoragePolicy.from_env({
            "MODPORT_SDK_STORAGE_LIMIT_MIB": "2048",
            "MODPORT_SDK_STORAGE_SAFETY_MIB": "128",
            "MODPORT_RUN_STORAGE_LIMIT_MIB": "40960",
            "MODPORT_STORAGE_MIN_FREE_MIB": "4096",
        })
        self.assertEqual(policy.sdk_limit_bytes, 2048 * MIB)
        self.assertEqual(policy.sdk_safety_bytes, 128 * MIB)
        self.assertEqual(policy.run_soft_limit_bytes, 40 * GIB)
        self.assertEqual(policy.min_free_bytes, 4 * GIB)

    def test_environment_cannot_disable_a_limit(self):
        names = (
            "MODPORT_SDK_STORAGE_LIMIT_MIB",
            "MODPORT_SDK_STORAGE_SAFETY_MIB",
            "MODPORT_RUN_STORAGE_LIMIT_MIB",
            "MODPORT_STORAGE_MIN_FREE_MIB",
        )
        for name in names:
            for value in (None, "", "0", "-1", "1.5", "inf", " 1"):
                with self.subTest(name=name, value=value):
                    with self.assertRaises(ValueError):
                        storage.StoragePolicy.from_env({name: value})

    def test_safety_reserve_must_fit_inside_sdk_limit(self):
        with self.assertRaises(ValueError):
            storage.StoragePolicy(sdk_limit_bytes=MIB, sdk_safety_bytes=MIB)

    def test_environment_cannot_weaken_fixed_reserve_floors(self):
        for environment in (
            {"MODPORT_SDK_STORAGE_SAFETY_MIB": "63"},
            {"MODPORT_STORAGE_MIN_FREE_MIB": "2047"},
        ):
            with self.subTest(environment=environment):
                with self.assertRaises(ValueError):
                    storage.StoragePolicy.from_env(environment)


class StorageBudgetTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "run"
        self.root.mkdir()
        self.policy = storage.StoragePolicy(
            sdk_limit_bytes=512 * MIB,
            sdk_safety_bytes=64 * MIB,
            run_soft_limit_bytes=16 * MIB,
            min_free_bytes=2 * GIB,
        )

    def enough_space(self, free=100 * GIB):
        return patch.object(storage.shutil, "disk_usage", return_value=SimpleNamespace(free=free))

    def test_real_sparse_sdk_file_is_rejected_without_importing_sdk(self):
        database = self.root / "orchestrator.sqlite3"
        with sqlite3.connect(database) as connection:
            connection.execute("CREATE TABLE facts (value TEXT)")
        with database.open("r+b") as stream:
            stream.truncate(448 * MIB + 1)
        before = (database.stat().st_size, database.stat().st_mtime_ns)
        with self.enough_space(), patch.dict(sys.modules, {"dispatcher_sdk": None}):
            with self.assertRaisesRegex(storage.StorageBudgetError, "sdk current=.*limit=.*safety_reserve"):
                storage.check_storage_budget(
                    self.root, policy=self.policy, phase="tick", record=False)
        self.assertEqual((database.stat().st_size, database.stat().st_mtime_ns), before)
        self.assertFalse((self.root / "artifacts").exists())

    def test_low_space_is_rejected_with_clear_metrics(self):
        with self.enough_space(2 * GIB - 1):
            with self.assertRaises(storage.StorageBudgetError) as raised:
                storage.check_storage_budget(
                    self.root, policy=self.policy, phase="recover", record=False)
        self.assertIn("filesystem free=", str(raised.exception))
        self.assertIn("reserve=", str(raised.exception))
        self.assertEqual(raised.exception.report["violations"], ["free_space"])

    def test_pending_bytes_reserve_sdk_and_free_space(self):
        database = self.root / "kernel.sqlite3"
        with database.open("wb") as stream:
            stream.truncate(447 * MIB)
        with self.enough_space():
            accepted = storage.check_storage_budget(
                self.root, policy=self.policy, phase="tick", pending_bytes=MIB,
                record=False)
            self.assertEqual(accepted["sdk_projected_bytes"], 448 * MIB)
            with self.assertRaises(storage.StorageBudgetError) as raised:
                storage.check_storage_budget(
                    self.root, policy=self.policy, phase="tick", pending_bytes=MIB + 1,
                    record=False)
        self.assertEqual(raised.exception.report["violations"], ["sdk"])

        with self.enough_space(2 * GIB + MIB):
            storage.check_storage_budget(
                self.root, policy=self.policy, phase="tick", pending_bytes=MIB,
                record=False)
            with self.assertRaises(storage.StorageBudgetError) as raised:
                storage.check_storage_budget(
                    self.root, policy=self.policy, phase="tick", pending_bytes=MIB + 1,
                    record=False)
        self.assertIn("free_space", raised.exception.report["violations"])

    def test_tick_does_not_scan_run_but_full_phase_does(self):
        artifact = self.root / "artifacts" / "large.bin"
        artifact.parent.mkdir()
        with artifact.open("wb") as stream:
            stream.truncate(self.policy.run_soft_limit_bytes + 1)
        with self.enough_space():
            quick = storage.check_storage_budget(
                self.root, policy=self.policy, phase="tick", record=False)
            self.assertFalse(quick["full_scan"])
            self.assertIsNone(quick["run_bytes"])
            with self.assertRaises(storage.StorageBudgetError) as raised:
                storage.check_storage_budget(
                    self.root, policy=self.policy, phase="submit", record=False)
        self.assertEqual(raised.exception.report["violations"], ["run"])

    def test_qualified_maintenance_phase_gets_complete_sample(self):
        with self.enough_space():
            report = storage.check_storage_budget(
                self.root, policy=self.policy, phase="audit-maintenance:preflight",
                record=False)
        self.assertTrue(report["full_scan"])

    def test_only_leading_entrypoint_selects_full_scan(self):
        for phase in (
            "submit", "submit-final", "continue-final", "recover-reopen",
            "execute", "maintenance", "audit-maintenance:preflight",
        ):
            with self.subTest(phase=phase):
                self.assertTrue(storage._full_scan(phase))
        for phase in (
            "kernel-handler:test_execute:effect-result",
            "kernel-handler:test_execute:execution-result",
            "kernel-recovery:test_execute:effect-result",
            "tick",
        ):
            with self.subTest(phase=phase):
                self.assertFalse(storage._full_scan(phase))

    def test_complete_sample_ignores_only_entries_that_disappear_before_stat(self):
        present = SimpleNamespace(
            path=str(self.root / "artifacts" / "present.bin"),
            stat=lambda **_: SimpleNamespace(st_mode=stat.S_IFREG, st_size=19),
        )
        vanished = SimpleNamespace(path=str(self.root / "artifacts" / ".modport-gone"))
        vanished.stat = lambda **_: (_ for _ in ()).throw(FileNotFoundError("gone"))
        entries = MagicMock()
        entries.__iter__.return_value = iter((vanished, present))
        entries.__exit__.return_value = False
        with patch.object(storage.os, "scandir", return_value=entries):
            observed = storage.sample_storage_footprint(self.root)
        self.assertEqual(19, observed["total_bytes"])
        self.assertEqual(1, observed["files"])
        self.assertEqual(19, observed["categories"]["artifacts"])

    def test_complete_sample_still_fails_closed_on_permission_error(self):
        denied = SimpleNamespace(path=str(self.root / "denied.bin"))
        denied.stat = lambda **_: (_ for _ in ()).throw(PermissionError("denied"))
        entries = MagicMock()
        entries.__iter__.return_value = iter((denied,))
        entries.__exit__.return_value = False
        with patch.object(storage.os, "scandir", return_value=entries):
            with self.assertRaisesRegex(storage.StorageBudgetError, "PermissionError"):
                storage.sample_storage_footprint(self.root)

    def test_complete_sample_ignores_child_directory_removed_after_queueing(self):
        child = SimpleNamespace(
            path=str(self.root / "retired"),
            stat=lambda **_: SimpleNamespace(st_mode=stat.S_IFDIR, st_size=0),
        )
        root_entries = MagicMock()
        root_entries.__iter__.return_value = iter((child,))
        root_entries.__exit__.return_value = False
        with patch.object(storage.os, "scandir",
                          side_effect=(root_entries, FileNotFoundError("retired"))):
            observed = storage.sample_storage_footprint(self.root)
        self.assertEqual(0, observed["total_bytes"])
        self.assertEqual(0, observed["files"])

    def test_complete_sample_fails_if_root_disappears_before_scandir(self):
        with patch.object(storage.os, "scandir",
                          side_effect=FileNotFoundError("root disappeared")):
            with self.assertRaisesRegex(storage.StorageBudgetError, "FileNotFoundError"):
                storage.sample_storage_footprint(self.root)

    def test_complete_sample_has_disjoint_categories(self):
        files = {
            "kernel.sqlite3": 11,
            "audit-blobs/a.gz": 13,
            "audit-report/report.json": 17,
            "artifacts/result.json": 19,
            "workspaces/one/file.bin": 23,
            "toolchains/cache.bin": 29,
            "other.bin": 31,
        }
        for relative, size in files.items():
            path = self.root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("wb") as stream:
                stream.truncate(size)
        observed = storage.sample_storage_footprint(self.root)
        self.assertEqual(observed["total_bytes"], sum(files.values()))
        self.assertEqual(observed["files"], len(files))
        self.assertEqual(observed["categories"], {
            "sdk": 11,
            "audit_blobs": 13,
            "reports": 17,
            "artifacts": 19,
            "workspaces_toolchains": 52,
            "other": 31,
        })

    def test_record_is_single_bounded_json_and_unchanged_observation_is_not_rewritten(self):
        database = self.root / "kernel.sqlite3"
        database.write_bytes(b"db")
        real_atomic = storage.atomic_json
        with self.enough_space(), patch.object(storage, "atomic_json", wraps=real_atomic) as write:
            first = storage.check_storage_budget(
                self.root, policy=self.policy, phase="tick")
            second = storage.check_storage_budget(
                self.root, policy=self.policy, phase="tick")
            self.assertEqual(first, second)
            self.assertEqual(write.call_count, 1)
            database.write_bytes(b"database grew")
            storage.check_storage_budget(self.root, policy=self.policy, phase="tick")
            self.assertEqual(write.call_count, 2)
        status = self.root / "artifacts/storage/status.json"
        self.assertTrue(status.is_file())
        self.assertLess(status.stat().st_size, 16 * 1024)
        self.assertEqual(list((self.root / "artifacts/storage").iterdir()), [status])

    def test_repeated_same_phase_error_is_deduplicated(self):
        real_atomic = storage.atomic_json
        with self.enough_space(1), patch.object(storage, "atomic_json", wraps=real_atomic) as write:
            for _ in range(2):
                with self.assertRaises(storage.StorageBudgetError):
                    storage.check_storage_budget(
                        self.root, policy=self.policy, phase="recover")
        self.assertEqual(write.call_count, 1)

    def test_check_mutates_only_its_diagnostic(self):
        database = self.root / "orchestrator.sqlite3"
        database.write_bytes(b"immutable source")
        before = (sha256(database.read_bytes()).hexdigest(), database.stat().st_mtime_ns)
        with self.enough_space():
            storage.check_storage_budget(self.root, policy=self.policy, phase="tick")
        after = (sha256(database.read_bytes()).hexdigest(), database.stat().st_mtime_ns)
        self.assertEqual(after, before)
        files = {path.relative_to(self.root).as_posix()
                 for path in self.root.rglob("*") if path.is_file()}
        self.assertEqual(files, {"orchestrator.sqlite3", "artifacts/storage/status.json"})

    def test_missing_root_uses_nearest_existing_parent_without_creating_it(self):
        missing = self.root / "future" / "nested"
        with patch.object(storage.shutil, "disk_usage",
                          return_value=SimpleNamespace(free=100 * GIB)) as usage:
            report = storage.check_storage_budget(
                missing, policy=self.policy, phase="submit", record=True)
        self.assertEqual(report["run_bytes"], 0)
        usage.assert_called_once_with(self.root)
        self.assertFalse(missing.exists())

    def test_nonregular_sdk_sidecar_fails_closed(self):
        (self.root / "kernel.sqlite3-wal").symlink_to(self.root / "outside")
        with self.enough_space():
            with self.assertRaisesRegex(storage.StorageBudgetError, "not a regular file"):
                storage.check_storage_budget(
                    self.root, policy=self.policy, phase="tick", record=False)


if __name__ == "__main__":
    unittest.main()
