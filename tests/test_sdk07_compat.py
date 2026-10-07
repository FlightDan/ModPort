"""Deployment boundaries specific to dispatcher-sdk 0.7.1."""

from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from dispatcher_sdk.execution_kernel import Kernel
from dispatcher_sdk.orchestrator import Orchestrator

from modport.models import LockedManifest, MigrationRequest
from modport.sdk_compat import SDKCompatibilityError, inspect_runtime, require_compatible_storage


class SDK07CompatibilityTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "run"

    def _create_current_stores(self) -> None:
        runtime = Kernel.open_sqlite(self.root / "kernel.sqlite3", {})
        orchestrator = Orchestrator(
            self.root / "orchestrator.sqlite3", runtime.kernel, runtime=runtime
        )
        orchestrator.close()
        runtime.close()

    def test_current_component_schemas_are_exactly_kernel_five_orchestrator_four(self):
        self._create_current_stores()
        report = require_compatible_storage(self.root, {})
        observed = {item["name"]: item["schemas"] for item in report["storages"]}
        self.assertEqual(observed["kernel"]["kernel"], 5)
        self.assertEqual(observed["orchestrator"]["orchestrator"], 4)

    def test_identity_verdict_cannot_override_wrong_exact_schema(self):
        self._create_current_stores()
        report = inspect_runtime(self.root, {})
        wrong = deepcopy(report)
        orchestrator = next(
            item for item in wrong["storages"] if item["name"] == "orchestrator"
        )
        orchestrator["schemas"]["orchestrator"] = 2
        orchestrator["status"] = "recognized"
        orchestrator["execute"] = {"status": "supported", "reasons": []}
        orchestrator["resume"] = {"status": "supported", "reasons": []}
        with patch("modport.sdk_compat.inspect_runtime", return_value=wrong):
            with self.assertRaisesRegex(SDKCompatibilityError, "expected 4"):
                require_compatible_storage(self.root, {})

    def test_incomplete_or_missing_component_observation_fails_closed(self):
        self._create_current_stores()
        report = inspect_runtime(self.root, {})
        for changed in (
            {**deepcopy(report), "complete": False},
            {**deepcopy(report), "storages": deepcopy(report["storages"][:1])},
        ):
            with self.subTest(
                complete=changed["complete"],
                storages=[item["name"] for item in changed["storages"]],
            ):
                with patch("modport.sdk_compat.inspect_runtime", return_value=changed):
                    with self.assertRaisesRegex(
                        SDKCompatibilityError, "one complete observation"
                    ):
                        require_compatible_storage(self.root, {})

    def test_new_manifest_defaults_to_sdk07_but_historical_value_is_preserved(self):
        request = MigrationRequest(
            mod_id="fixture",
            source_repository="https://example.invalid/mod.git",
            source_minecraft="1.20.1",
            target_minecraft="1.21.1",
        )
        current = LockedManifest(request, "21.1.77")
        self.assertEqual(current.sdk_version, "0.7.1")
        serialized = current.to_dict()
        historical = dict(serialized, sdk_version="0.6.0")
        restored = LockedManifest.from_mapping(historical)
        self.assertEqual(restored.sdk_version, "0.6.0")
        self.assertEqual(restored.to_dict()["sdk_version"], "0.6.0")


if __name__ == "__main__":
    unittest.main()
