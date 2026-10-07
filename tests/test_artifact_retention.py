import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from modport.artifact_retention import (
    ArtifactRetentionError,
    apply_retention,
    inspect_archived_artifact,
    inspect_retention_registry,
    plan_retention,
    restore_archived_artifact,
)
from modport.evidence import atomic_json, file_digest, workspace_lock


class ArtifactRetentionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.root = self.base / "run"
        self.archive = self.base / "archive"
        self.root.mkdir()
        device = patch("modport.artifact_retention._device",
            side_effect=lambda path: 1 if Path(path).resolve() == self.root.resolve() else 2)
        device.start()
        self.addCleanup(device.stop)

    def chain(self, *segments):
        """Create headers from oldest to current."""
        continuations = self.root / "artifacts/continuations"
        for index, segment in enumerate(segments[:-1]):
            header = {"run_id": segment}
            if index:
                header["continuation"] = {"previous_run_id": segments[index - 1]}
            atomic_json(continuations / segment / "run.json", header)
        current = {"run_id": segments[-1]}
        if len(segments) > 1:
            current["continuation"] = {"previous_run_id": segments[-2]}
        atomic_json(self.root / "run.json", current)

    def write_old_details(self, segment, value=b"historical detail"):
        directory = self.root / "artifacts/continuations" / segment
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "prepared.json").write_bytes(value)
        (directory / "rework-sources.json").write_bytes(value + b"-sources")
        atomic_json(directory / "successor.json", {"next": "kept"})

    def audit_generation(self, name, marker):
        directory = self.root / "audit-report/.audit-generations" / name
        directory.mkdir(parents=True)
        artifacts = {}
        for filename in ("audit.html", "audit.json", "audit.csv", "audit.md"):
            path = directory / filename
            path.write_text(marker + filename)
            artifacts[filename] = {"sha256": file_digest(path), "size": path.stat().st_size}
        atomic_json(directory / "manifest.json", {
            "schema_version": 1, "generation": name, "artifacts": artifacts})
        return directory

    def test_chain_order_controls_retention_and_missing_settlement_blocks(self):
        self.chain("one", "two", "three", "four")
        self.write_old_details("one")
        # Deliberately make the oldest segment newest by mtime.
        os.utime(self.root / "artifacts/continuations/one/run.json", None)
        plan = plan_retention(self.root, keep_rounds=3)
        self.assertEqual(["four", "three", "two", "one"], plan["segment_chain"])
        self.assertEqual(2, len(plan["candidates"]))
        self.assertTrue(all(row["status"] == "blocked" for row in plan["candidates"]))
        self.assertTrue(all("settled-state" in row["blocked_reasons"][0]
                            for row in plan["candidates"]))

    def test_archive_preserves_chain_files_and_restores_verified_content(self):
        self.chain("one", "two", "three", "four")
        self.write_old_details("one")
        original = (self.root / "artifacts/continuations/one/prepared.json").read_bytes()
        plan = plan_retention(self.root, settled_segments={"one"})
        report = apply_retention(self.root, plan, self.archive)
        self.assertEqual([], report["errors"])
        self.assertEqual(2, len(report["archived"]))
        old = self.root / "artifacts/continuations/one"
        self.assertFalse((old / "prepared.json").exists())
        self.assertFalse((old / "rework-sources.json").exists())
        self.assertTrue((old / "run.json").is_file())
        self.assertTrue((old / "successor.json").is_file())
        index = inspect_retention_registry(self.root, verify_archives=True)
        relative = "artifacts/continuations/one/prepared.json"
        self.assertIn(relative, index["entries"])
        cold = inspect_archived_artifact(self.root, relative)
        self.assertEqual(relative, cold["relative_path"])
        self.assertTrue(Path(cold["archive_path"]).is_file())
        restored = restore_archived_artifact(self.root, relative)
        self.assertEqual(original, restored.read_bytes())

    def test_stale_plan_and_active_writer_never_release_source(self):
        self.chain("one", "two", "three", "four")
        self.write_old_details("one")
        plan = plan_retention(self.root, settled_segments={"one"})
        path = self.root / "artifacts/continuations/one/prepared.json"
        path.write_bytes(b"changed after planning")
        with self.assertRaisesRegex(ArtifactRetentionError, "stale"):
            apply_retention(self.root, plan, self.archive)
        self.assertTrue(path.exists())
        fresh = plan_retention(self.root, settled_segments={"one"})
        with workspace_lock(self.root):
            report = apply_retention(self.root, fresh, self.archive)
        self.assertIn("active writer", report["errors"][0]["reason"])
        self.assertTrue(path.exists())

    def test_symlink_hardlink_and_protected_paths_are_blocked(self):
        self.chain("one", "two", "three", "four")
        directory = self.root / "artifacts/continuations/one"
        (directory / "prepared.json").symlink_to(directory / "run.json")
        os.link(directory / "run.json", directory / "rework-sources.json")
        plan = plan_retention(self.root, settled_segments={"one"},
            protected_paths={"artifacts/continuations/one/prepared.json"})
        self.assertEqual(2, len(plan["candidates"]))
        reasons = " ".join(reason for row in plan["candidates"]
                           for reason in row["blocked_reasons"])
        self.assertIn("protected", reasons)
        self.assertIn("regular file", reasons)
        self.assertIn("hard links", reasons)
        with self.assertRaises(ArtifactRetentionError):
            plan_retention(self.root, protected_paths={"../escape"})

    def test_noncurrent_audit_generation_is_archived_as_a_complete_group(self):
        self.chain("current")
        old_name = "generation-" + "1" * 32
        current_name = "generation-" + "2" * 32
        old = self.audit_generation(old_name, "old-")
        current = self.audit_generation(current_name, "current-")
        report_dir = self.root / "audit-report"
        (report_dir / ".audit-current").symlink_to(
            f".audit-generations/{current_name}")
        (report_dir / ".audit-publish.lock").touch()
        plan = plan_retention(self.root)
        candidates = [row for row in plan["candidates"]
                      if row["kind"] == "audit_generation"]
        self.assertEqual([old_name], [row["generation"] for row in candidates])
        self.assertEqual("eligible", candidates[0]["status"])
        result = apply_retention(self.root, plan, self.archive)
        self.assertEqual([], result["errors"])
        self.assertFalse(old.exists())
        self.assertTrue(current.is_dir())
        self.assertEqual(5, len([row for row in result["archived"]
                                if row["id"].startswith("audit_generation:")]))

    def test_invalid_audit_pointer_protects_all_fallback_generations(self):
        self.chain("current")
        name = "generation-" + "3" * 32
        generation = self.audit_generation(name, "old-")
        report_dir = self.root / "audit-report"
        (report_dir / ".audit-current").symlink_to("bad-target")
        plan = plan_retention(self.root, settled_segments={"current"})
        candidate = next(row for row in plan["candidates"]
                         if row["kind"] == "audit_generation")
        self.assertEqual("blocked", candidate["status"])
        self.assertIn("fallback", candidate["blocked_reasons"][0])
        (report_dir / ".audit-publish.lock").touch()
        result = apply_retention(self.root, plan, self.archive)
        self.assertEqual([], result["archived"])
        self.assertTrue(generation.is_dir())

    def test_incomplete_old_audit_generation_is_blocked(self):
        self.chain("current")
        old_name = "generation-" + "4" * 32
        current_name = "generation-" + "5" * 32
        old = self.audit_generation(old_name, "old-")
        self.audit_generation(current_name, "current-")
        (old / "audit.csv").unlink()
        report_dir = self.root / "audit-report"
        (report_dir / ".audit-current").symlink_to(
            f".audit-generations/{current_name}")
        plan = plan_retention(self.root)
        candidate = next(row for row in plan["candidates"]
                         if row.get("generation") == old_name)
        self.assertEqual("blocked", candidate["status"])
        self.assertIn("complete", " ".join(candidate["blocked_reasons"]))

    def test_archive_must_be_on_an_independent_filesystem(self):
        self.chain("one", "two", "three", "four")
        self.write_old_details("one")
        plan = plan_retention(self.root, settled_segments={"one"})
        with patch("modport.artifact_retention._device", return_value=7):
            with self.assertRaisesRegex(ArtifactRetentionError, "filesystem"):
                apply_retention(self.root, plan, self.archive)

    def test_restore_refuses_corruption_and_different_existing_target(self):
        self.chain("one", "two", "three", "four")
        self.write_old_details("one")
        plan = plan_retention(self.root, settled_segments={"one"})
        result = apply_retention(self.root, plan, self.archive)
        self.assertEqual([], result["errors"])
        relative = "artifacts/continuations/one/prepared.json"
        target = self.root / relative
        target.write_bytes(b"different")
        with self.assertRaisesRegex(ArtifactRetentionError, "different data"):
            restore_archived_artifact(self.root, relative)
        target.unlink()
        record = inspect_retention_registry(self.root)["entries"][relative]
        archive = Path(record["archive_root"]) / record["locator"]
        archive.write_bytes(b"corrupt")
        with self.assertRaisesRegex(ArtifactRetentionError, "compressed"):
            restore_archived_artifact(self.root, relative)

    def test_index_and_archive_parent_symlinks_are_rejected(self):
        self.chain("one", "two", "three", "four")
        self.write_old_details("one")
        plan = plan_retention(self.root, settled_segments={"one"})
        outside = self.base / "outside"
        outside.mkdir()
        (self.root / "artifacts/storage").symlink_to(outside)
        report = apply_retention(self.root, plan, self.archive)
        self.assertTrue(report["errors"])
        self.assertTrue((self.root / "artifacts/continuations/one/prepared.json").exists())
        self.assertFalse((outside / "retention-index.json").exists())

        (self.root / "artifacts/storage").unlink()
        archive = self.base / "archive-two"
        archive.mkdir()
        (archive / "objects").symlink_to(outside)
        report = apply_retention(self.root, plan, archive)
        self.assertTrue(report["errors"])
        self.assertTrue((self.root / "artifacts/continuations/one/prepared.json").exists())


if __name__ == "__main__":
    unittest.main()
