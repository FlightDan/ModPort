"""Tests for execution-bound host verification interface evidence."""
import inspect
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from modport.contract_inputs import validate_baseline_gradle_tasks
from modport.evidence import verified_path
from modport.goal_planning import validate_goal
from modport.goal_validation import _check
from modport.handlers import BaselineContractVerificationHandler, ClientSmokeHandler
from modport.host_interface import publish_host_interface


class HostInterfaceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def test_publishes_deployed_goal_and_gate_sources_with_provenance(self):
        ref = publish_host_interface(self.root, "run-1:goal:1")

        self.assertRegex(ref["path"], r"^artifacts/repair-evidence/[0-9a-f]{64}/evidence$")
        self.assertEqual(ref["metadata"]["execution_id"], "run-1:goal:1")
        self.assertEqual(ref["metadata"]["source_capture"], "inspect.getsource")
        self.assertEqual(ref["metadata"]["source_paths"], [
            "src/modport/goal_validation.py",
            "src/modport/goal_planning.py",
            "src/modport/contract_inputs.py",
            "src/modport/handlers.py",
        ])
        body = verified_path(self.root, ref).read_text(encoding="utf-8")
        for source in (_check, validate_goal, validate_baseline_gradle_tasks,
                       BaselineContractVerificationHandler, ClientSmokeHandler):
            self.assertIn(inspect.getsource(source), body)
        self.assertIn("type is gradle_regression", body)
        self.assertIn("automatically adds /workspace/.modport/characterization.init.gradle", body)
        self.assertIn("Tasks are not flags", body)

    def test_publication_is_execution_specific_and_idempotently_immutable(self):
        first = publish_host_interface(self.root, "run-1:goal:1")
        replay = publish_host_interface(self.root, "run-1:goal:1")
        second = publish_host_interface(self.root, "run-1:goal:2")

        self.assertEqual(first, replay)
        self.assertNotEqual(first["path"], second["path"])
        self.assertNotEqual(first["sha256"], second["sha256"])
        source = self.root / first["metadata"]["source_path"]
        source.write_text("changed after publication", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "already differs"):
            publish_host_interface(self.root, "run-1:goal:1")
        self.assertNotEqual(verified_path(self.root, first).read_text(), source.read_text())

    def test_rejects_execution_path_traversal_and_symlinked_storage(self):
        for execution_id in ("", ".", "..", "../escape", "nested/escape", "bad\\path"):
            with self.subTest(execution_id=execution_id), self.assertRaisesRegex(
                    ValueError, "safe nonempty execution ID"):
                publish_host_interface(self.root, execution_id)

        outside = self.root / "outside"
        outside.mkdir()
        artifacts = self.root / "artifacts"
        artifacts.symlink_to(outside, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "unsafe host interface support path"):
            publish_host_interface(self.root, "run-2:goal:1")

    def test_rejects_deployed_source_change_under_existing_execution(self):
        publish_host_interface(self.root, "run-1:goal:1")
        with patch("modport.host_interface._interface_text", return_value="new deployed source"):
            with self.assertRaisesRegex(ValueError, "already differs"):
                publish_host_interface(self.root, "run-1:goal:1")


if __name__ == "__main__":
    unittest.main()
