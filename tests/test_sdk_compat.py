"""SDK release identity and read-only storage preflight regressions."""

from copy import deepcopy
from dataclasses import replace
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from dispatcher_sdk import runtime_identity
from dispatcher_sdk.execution_kernel import Kernel
from dispatcher_sdk.orchestrator import Orchestrator

from modport.sdk_compat import (
    SDKCompatibilityError, inspect_runtime, require_compatible_storage, sdk_release,
)


class SDKCompatibilityTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "run"

    def test_real_source_release(self):
        self.assertEqual(sdk_release(), "0.7.1")

    def test_source_checkout_without_metadata_is_supported(self):
        report = runtime_identity()
        report = replace(report, module=replace(
            report.module, distribution_version=None, source_version="0.7.1",
            version_agreement="unknown", distribution_record="unknown",
        ))
        with patch("modport.sdk_compat.dispatcher_sdk.runtime_identity", return_value=report):
            self.assertEqual(sdk_release(), "0.7.1")

    def test_wrong_versions_and_known_mismatches_are_rejected(self):
        report = runtime_identity()
        for changed in (
            {"source_version": "0.5.1"}, {"source_version": None},
            {"distribution_version": "0.5.1"},
            {"version_agreement": "mismatch"},
            {"distribution_record": "mismatch"},
        ):
            with self.subTest(changed=changed):
                bad = replace(report, module=replace(report.module, **changed))
                with patch("modport.sdk_compat.dispatcher_sdk.runtime_identity", return_value=bad):
                    with self.assertRaisesRegex(SDKCompatibilityError, "requires dispatcher-sdk 0.7.1"):
                        sdk_release()

    def test_missing_stores_are_reported_and_not_created(self):
        report = require_compatible_storage(self.root)
        json.dumps(report)
        self.assertFalse(self.root.exists())
        self.assertEqual([item["name"] for item in report["storages"]], ["kernel", "orchestrator"])
        self.assertEqual([item["status"] for item in report["storages"]], ["missing", "missing"])

    def _create_stores(self):
        runtime = Kernel.open_sqlite(self.root / "kernel.sqlite3", {})
        orchestrator = Orchestrator(self.root / "orchestrator.sqlite3", runtime.kernel, runtime=runtime)
        orchestrator.close()
        runtime.close()

    def test_current_stores_pass_and_bytes_are_preserved(self):
        self._create_stores()
        # SQLite's read-only WAL connections may maintain -wal/-shm sidecars.
        before = {path.name: path.read_bytes() for path in self.root.glob("*.sqlite3")}
        report = require_compatible_storage(self.root, {})
        self.assertTrue(all(item["execute"]["status"] == "supported" for item in report["storages"]))
        self.assertEqual(before, {path.name: path.read_bytes() for path in self.root.glob("*.sqlite3")})

    def test_kernel_only_allows_initializing_missing_orchestrator(self):
        runtime = Kernel.open_sqlite(self.root / "kernel.sqlite3", {})
        runtime.close()
        require_compatible_storage(self.root, {})
        self.assertFalse((self.root / "orchestrator.sqlite3").exists())

    def test_damaged_storage_rejected_without_changes(self):
        self.root.mkdir()
        path = self.root / "kernel.sqlite3"
        path.write_bytes(b"not sqlite")
        with self.assertRaisesRegex(SDKCompatibilityError, "Back up.*copy upgrade"):
            require_compatible_storage(self.root)
        self.assertEqual(path.read_bytes(), b"not sqlite")
        self.assertFalse((self.root / "orchestrator.sqlite3").exists())

    def test_swapped_component_store_is_rejected(self):
        runtime = Kernel.open_sqlite(self.root / "orchestrator.sqlite3", {})
        runtime.close()
        with self.assertRaises(SDKCompatibilityError):
            require_compatible_storage(self.root)

    def test_unknown_inspection_and_binding_mismatch_are_rejected(self):
        self._create_stores()
        report = inspect_runtime(self.root, {})
        for changed in (
            {"status": "unknown", "exists": None},
            {"resume": {"status": "unsupported", "reasons": ["binding_mismatch"]}},
        ):
            with self.subTest(changed=changed):
                bad = deepcopy(report)
                bad["storages"][0].update(changed)
                with patch("modport.sdk_compat.inspect_runtime", return_value=bad):
                    with self.assertRaises(SDKCompatibilityError):
                        require_compatible_storage(self.root, {})

    def test_preflight_uses_one_public_identity_observation(self):
        with patch("modport.sdk_compat.dispatcher_sdk.runtime_identity", wraps=runtime_identity) as identify:
            require_compatible_storage(self.root, {})
            identify.assert_called_once_with(component_paths={
                "kernel": self.root / "kernel.sqlite3",
                "orchestrator": self.root / "orchestrator.sqlite3",
            }, handlers={})


if __name__ == "__main__":
    unittest.main()
