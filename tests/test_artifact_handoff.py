import hashlib
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import modport.artifact_handoff as artifact_handoff
from modport.artifact_handoff import (
    MAX_FILE_BYTES,
    install_handoff,
    prepare_handoff,
    validate_handoff,
)
from modport.evidence import atomic_json, file_digest
from modport.manifest import canonical_json


def git(directory: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(directory), *args],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if result.returncode:
        raise AssertionError(result.stderr)
    return result.stdout.strip()


class ArtifactHandoffTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary.name)
        self.root = self.base / "old-run"
        self.worktree = self.root / "worktree"
        self.worktree.mkdir(parents=True)
        git(self.worktree, "init")
        git(self.worktree, "config", "user.name", "ModPort Test")
        git(self.worktree, "config", "user.email", "modport@example.invalid")
        (self.worktree / "build.gradle").write_text("plugins {}\n", encoding="utf-8")
        git(self.worktree, "add", "build.gradle")
        git(self.worktree, "commit", "-m", "source")
        self.source_commit = git(self.worktree, "rev-parse", "HEAD")
        (self.worktree / "src.txt").write_text("migrated\n", encoding="utf-8")
        git(self.worktree, "add", "src.txt")
        git(self.worktree, "commit", "-m", "target")
        self.target_commit = git(self.worktree, "rev-parse", "HEAD")
        self.request = {
            "source_repository": "https://example.invalid/mod.git",
            "source_revision": self.source_commit,
            "source_loader": "forge",
            "source_minecraft": "1.20.1",
            "target_loader": "neoforge",
            "target_minecraft": "1.21.1",
        }
        atomic_json(self.root / "run.json", {
            "run_id": "old-run-id",
            "logical_run_id": "logical-id",
            "request": self.request,
            "sdk_state_that_must_not_be_copied": {"attempts": [1, 2, 3]},
        })
        atomic_json(self.root / "artifacts/source.json", {
            "source_repository": self.request["source_repository"],
            "requested_revision": self.request["source_revision"],
            "source_commit": self.source_commit,
        })
        (self.root / "reports").mkdir()
        (self.root / "reports/latest.json").write_text('{"status":"unverified"}\n')

    def tearDown(self):
        self.temporary.cleanup()

    def prepare(self, paths=None):
        destination = self.base / "handoff"
        manifest = prepare_handoff(
            self.root,
            destination,
            paths or ["reports/latest.json"],
        )
        return destination, manifest

    def replace_bundle_from(self, handoff: Path, repository: Path) -> dict:
        replacement = self.base / (handoff.name + "-replacement.bundle")
        git(repository, "bundle", "create", str(replacement), "HEAD")
        (handoff / "repository.bundle").write_bytes(replacement.read_bytes())
        manifest_path = handoff / "manifest.json"
        manifest = json.loads(manifest_path.read_text())
        manifest["source"]["target_commit"] = git(repository, "rev-parse", "HEAD")
        manifest["repository_bundle"]["size"] = replacement.stat().st_size
        manifest["repository_bundle"]["sha256"] = file_digest(replacement)
        manifest.pop("manifest_sha256")
        manifest["manifest_sha256"] = hashlib.sha256(
            canonical_json(manifest).encode("utf-8")
        ).hexdigest()
        atomic_json(manifest_path, manifest)
        return manifest

    def test_prepare_and_install_bind_artifacts_and_both_commits(self):
        bundle, manifest = self.prepare()
        self.assertEqual("unverified", manifest["acceptance_status"])
        self.assertEqual(self.source_commit, manifest["source"]["source_commit"])
        self.assertEqual(self.target_commit, manifest["source"]["target_commit"])
        self.assertNotIn("sdk_state_that_must_not_be_copied", json.dumps(manifest))
        self.assertEqual(["reports/latest.json"], [item["source_path"] for item in manifest["artifacts"]])
        self.assertEqual(4, len(manifest["diagnostics"]))

        (self.base / "new-run").mkdir()
        installed = install_handoff(bundle, self.base / "new-run")
        self.assertEqual("unverified", installed["metadata"]["acceptance_status"])
        self.assertEqual(self.source_commit, installed["metadata"]["source_commit"])
        self.assertIn("artifact_handoff", installed["refs"])
        self.assertIn("handoff:reports/latest.json", installed["refs"])
        for reference in installed["refs"].values():
            target = self.base / "new-run" / reference["path"]
            self.assertEqual(reference["sha256"], file_digest(target))
        self.assertEqual(manifest, validate_handoff(self.base / "new-run/artifacts/handoff"))

        clone = self.base / "cloned.git"
        git(self.base, "clone", "--bare", str(bundle / "repository.bundle"), str(clone))
        for commit in (self.source_commit, self.target_commit):
            subprocess.run(
                ["git", "--git-dir", str(clone), "cat-file", "-e", commit + "^{commit}"],
                check=True,
            )

    def test_carried_contract_removes_only_its_missing_diagnostic(self):
        contract = self.root / "baseline/.modport/functional-contract.json"
        contract.parent.mkdir(parents=True)
        contract.write_text("{}\n")
        _, manifest = self.prepare([
            "reports/latest.json",
            "baseline/.modport/functional-contract.json",
        ])
        self.assertNotIn("contract_not_carried", {item["code"] for item in manifest["diagnostics"]})
        self.assertEqual("unverified", manifest["acceptance_status"])

    def test_environment_lock_and_metadata_must_be_selected_together(self):
        lock = self.root / "artifacts/locked-manifest.json"
        metadata = self.root / "toolchains/neoforge-maven-metadata.xml"
        lock.write_text("{}\n", encoding="utf-8")
        metadata.parent.mkdir(parents=True)
        metadata.write_text("<metadata/>\n", encoding="utf-8")

        for name, selected, missing in (
            ("lock-only", ["artifacts/locked-manifest.json"], "toolchains/neoforge-maven-metadata.xml"),
            ("metadata-only", ["toolchains/neoforge-maven-metadata.xml"], "artifacts/locked-manifest.json"),
        ):
            output = self.base / name
            with self.subTest(selected=selected), self.assertRaisesRegex(
                    ValueError, "environment handoff must select.*" + missing.replace(".", r"\.")):
                prepare_handoff(self.root, output, selected)
            self.assertFalse(output.exists())

        _, manifest = self.prepare([
            "artifacts/locked-manifest.json",
            "toolchains/neoforge-maven-metadata.xml",
        ])
        self.assertEqual(
            ["artifacts/locked-manifest.json", "toolchains/neoforge-maven-metadata.xml"],
            [item["source_path"] for item in manifest["artifacts"]],
        )

        manifest_path = self.base / "handoff/manifest.json"
        metadata_entry = manifest["artifacts"].pop()
        (self.base / "handoff" / metadata_entry["path"]).unlink()
        manifest.pop("manifest_sha256")
        manifest["manifest_sha256"] = artifact_handoff._manifest_digest(manifest)
        atomic_json(manifest_path, manifest)
        with self.assertRaisesRegex(
                ValueError, "environment handoff must select.*neoforge-maven-metadata\\.xml"):
            validate_handoff(self.base / "handoff")

    def test_dirty_target_is_rejected_without_creating_output(self):
        (self.worktree / "uncommitted.txt").write_text("not committed")
        output = self.base / "handoff"
        with self.assertRaisesRegex(ValueError, "uncommitted changes"):
            prepare_handoff(self.root, output, ["reports/latest.json"])
        self.assertFalse(output.exists())

    def test_explicit_committed_head_handoff_excludes_dirty_target(self):
        (self.worktree / "src.txt").write_text("unaccepted edit\n")
        (self.worktree / "uncommitted.txt").write_text("unaccepted file\n")
        output = self.base / "committed-only"
        manifest = prepare_handoff(
            self.root, output, ["reports/latest.json"], committed_head_only=True)
        self.assertEqual(self.target_commit, manifest["source"]["target_commit"])
        self.assertEqual("committed_head_only",
                         manifest["source"]["worktree_selection"]["mode"])
        self.assertEqual(2, manifest["source"]["worktree_selection"]["excluded_status_entries"])
        self.assertIn("uncommitted_changes_excluded",
                      [item["code"] for item in manifest["diagnostics"]])
        installed_root = self.base / "committed-new-run"
        installed_root.mkdir()
        install_handoff(output, installed_root)
        checkout = self.base / "committed-checkout"
        subprocess.run(["git", "clone", str(output / "repository.bundle"), str(checkout)],
                       check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.assertEqual("migrated\n", (checkout / "src.txt").read_text())
        self.assertFalse((checkout / "uncommitted.txt").exists())

    def test_committed_head_handoff_rejects_selected_worktree_and_hidden_index(self):
        (self.worktree / "src.txt").write_text("unaccepted edit\n")
        with self.assertRaisesRegex(ValueError, "cannot select worktree"):
            prepare_handoff(
                self.root, self.base / "selected-worktree", ["worktree/src.txt"],
                committed_head_only=True)
        git(self.worktree, "update-index", "--assume-unchanged", "build.gradle")
        with self.assertRaisesRegex(ValueError, "hidden index flag"):
            prepare_handoff(
                self.root, self.base / "hidden-committed", ["reports/latest.json"],
                committed_head_only=True)

    def test_hidden_worktree_change_is_rejected(self):
        git(self.worktree, "update-index", "--assume-unchanged", "build.gradle")
        (self.worktree / "build.gradle").write_text("hidden change\n")
        with self.assertRaisesRegex(ValueError, "hidden index flag"):
            prepare_handoff(self.root, self.base / "hidden", ["reports/latest.json"])

    def test_scheduler_and_database_payloads_are_rejected(self):
        for relative in ("input.json", "prepared.json", "rework-sources.json", "kernel.sqlite3"):
            path = self.root / "artifacts" / relative
            path.write_text("{}")
            with self.subTest(relative=relative):
                with self.assertRaisesRegex(ValueError, "scheduler/database"):
                    prepare_handoff(self.root, self.base / ("out-" + relative), ["artifacts/" + relative])
        with self.assertRaisesRegex(ValueError, "scheduler/database"):
            prepare_handoff(self.root, self.base / "git-metadata", ["worktree/.git"])

    def test_path_escape_and_symlink_are_rejected(self):
        outside = self.base / "outside.txt"
        outside.write_text("secret")
        (self.root / "reports/link.txt").symlink_to(outside)
        with self.assertRaisesRegex(ValueError, "contained relative"):
            prepare_handoff(self.root, self.base / "escape", ["../outside.txt"])
        with self.assertRaisesRegex(ValueError, "symlink"):
            prepare_handoff(self.root, self.base / "link", ["reports/link.txt"])

    def test_large_selected_file_is_rejected_before_copy(self):
        huge = self.root / "reports/huge.bin"
        with huge.open("wb") as stream:
            stream.truncate(MAX_FILE_BYTES + 1)
        with self.assertRaisesRegex(ValueError, "exceeds 64 MiB"):
            prepare_handoff(self.root, self.base / "large", ["reports/huge.bin"])
        self.assertFalse((self.base / "large").exists())

    def test_tampered_file_bundle_and_manifest_are_rejected(self):
        original, _ = self.prepare()
        cases = []
        for name in ("file", "bundle", "manifest"):
            copy = self.base / ("tampered-" + name)
            subprocess.run(["cp", "-a", str(original), str(copy)], check=True)
            cases.append((name, copy))
        (cases[0][1] / "files/reports/latest.json").write_text("changed")
        with (cases[1][1] / "repository.bundle").open("ab") as stream:
            stream.write(b"changed")
        manifest_path = cases[2][1] / "manifest.json"
        manifest = json.loads(manifest_path.read_text())
        manifest["source"]["run_id"] = "forged"
        manifest_path.write_text(json.dumps(manifest))
        for name, path in cases:
            with self.subTest(name=name), self.assertRaisesRegex(ValueError, "checksum|size"):
                validate_handoff(path)

    def test_self_consistent_bundle_with_unrelated_source_commit_is_rejected(self):
        handoff, _ = self.prepare()
        repository = self.base / "unrelated-repository"
        subprocess.run(["git", "clone", str(self.worktree), str(repository)], check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        git(repository, "config", "user.name", "ModPort Test")
        git(repository, "config", "user.email", "modport@example.invalid")
        git(repository, "checkout", "--orphan", "unrelated")
        git(repository, "rm", "-rf", ".")
        (repository / "unrelated.txt").write_text("orphan history\n")
        git(repository, "add", "unrelated.txt")
        git(repository, "commit", "-m", "unrelated root")
        unrelated = git(repository, "rev-parse", "HEAD")
        replacement = self.base / "replacement.bundle"
        git(repository, "bundle", "create", str(replacement), "--all")
        (handoff / "repository.bundle").write_bytes(replacement.read_bytes())

        manifest_path = handoff / "manifest.json"
        manifest = json.loads(manifest_path.read_text())
        manifest["source"]["source_commit"] = unrelated
        manifest["repository_bundle"]["size"] = replacement.stat().st_size
        manifest["repository_bundle"]["sha256"] = file_digest(replacement)
        manifest.pop("manifest_sha256")
        manifest["manifest_sha256"] = hashlib.sha256(
            canonical_json(manifest).encode("utf-8")
        ).hexdigest()
        atomic_json(manifest_path, manifest)

        with self.assertRaisesRegex(ValueError, "not an ancestor"):
            validate_handoff(handoff)

    def test_compressed_bundle_cannot_hide_oversize_checkout_blob(self):
        handoff, _ = self.prepare()
        repository = self.base / "blob-repository"
        subprocess.run(["git", "clone", str(self.worktree), str(repository)], check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        git(repository, "config", "user.name", "ModPort Test")
        git(repository, "config", "user.email", "modport@example.invalid")
        (repository / "compressed.bin").write_bytes(b"A" * (256 * 1024))
        git(repository, "add", "compressed.bin")
        git(repository, "commit", "-m", "highly compressible large blob")
        manifest = self.replace_bundle_from(handoff, repository)
        self.assertLess(manifest["repository_bundle"]["size"], 32 * 1024)

        with patch("modport.artifact_handoff.MAX_FILE_BYTES", 32 * 1024):
            with self.assertRaisesRegex(ValueError, "blob exceeding"):
                validate_handoff(handoff)

    def test_two_checkout_logical_size_and_combined_entry_limit_are_bounded(self):
        handoff, _ = self.prepare()
        repository = self.base / "logical-size-repository"
        subprocess.run(["git", "clone", str(self.worktree), str(repository)], check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        git(repository, "config", "user.name", "ModPort Test")
        git(repository, "config", "user.email", "modport@example.invalid")
        (repository / "logical.bin").write_bytes(b"B" * (64 * 1024))
        git(repository, "add", "logical.bin")
        git(repository, "commit", "-m", "compressed logical checkout")
        self.replace_bundle_from(handoff, repository)
        physical = sum(path.stat().st_size for path in handoff.rglob("*") if path.is_file())
        self.assertLess(physical + 100, 64 * 1024)
        with patch("modport.artifact_handoff.MAX_HANDOFF_BYTES", physical + 100):
            with self.assertRaisesRegex(ValueError, "checkouts exceed"):
                validate_handoff(handoff)

        with patch("modport.artifact_handoff.MAX_TREE_ENTRIES", 2):
            with self.assertRaisesRegex(ValueError, "trees exceed"):
                validate_handoff(handoff)

    def test_bundle_is_rehashed_after_tree_inspection(self):
        handoff, _ = self.prepare()
        original = artifact_handoff._tree_checkout_totals
        calls = 0

        def mutate_after_first_tree(*args, **kwargs):
            nonlocal calls
            result = original(*args, **kwargs)
            calls += 1
            if calls == 1:
                with (handoff / "repository.bundle").open("ab") as stream:
                    stream.write(b"changed during inspection")
            return result

        with patch("modport.artifact_handoff._tree_checkout_totals", side_effect=mutate_after_first_tree):
            with self.assertRaisesRegex(ValueError, "changed during tree verification"):
                validate_handoff(handoff)

    def test_provenance_replacement_during_prepare_is_rejected(self):
        for index, relative in enumerate(("run.json", "artifacts/source.json")):
            with self.subTest(relative=relative):
                target = self.root / relative
                original_bytes = target.read_bytes()
                output = self.base / f"provenance-race-{index}"
                copied = artifact_handoff._copy_selected
                changed = False

                def replace_after_copy(*args, **kwargs):
                    nonlocal changed
                    result = copied(*args, **kwargs)
                    if not changed:
                        changed = True
                        value = json.loads(original_bytes)
                        value["concurrent_replacement"] = relative
                        atomic_json(target, value)
                    return result

                try:
                    with patch("modport.artifact_handoff._copy_selected", side_effect=replace_after_copy):
                        with self.assertRaisesRegex(ValueError, "provenance changed"):
                            prepare_handoff(self.root, output, ["reports/latest.json"])
                    self.assertFalse(output.exists())
                finally:
                    target.write_bytes(original_bytes)

    def test_unlisted_files_and_symlinks_are_rejected(self):
        original, _ = self.prepare()
        extra = self.base / "extra"
        linked = self.base / "linked"
        subprocess.run(["cp", "-a", str(original), str(extra)], check=True)
        subprocess.run(["cp", "-a", str(original), str(linked)], check=True)
        (extra / "unlisted.txt").write_text("not in manifest")
        (linked / "escape").symlink_to(self.base / "outside")
        with self.assertRaisesRegex(ValueError, "contents differ"):
            validate_handoff(extra)
        with self.assertRaisesRegex(ValueError, "symlink"):
            validate_handoff(linked)

    def test_install_refuses_existing_destination(self):
        bundle, _ = self.prepare()
        run = self.base / "new-run"
        run.mkdir()
        install_handoff(bundle, run)
        with self.assertRaises(FileExistsError):
            install_handoff(bundle, run)

    def test_source_evidence_must_match_frozen_request(self):
        source = json.loads((self.root / "artifacts/source.json").read_text())
        source["source_repository"] = "https://example.invalid/other.git"
        atomic_json(self.root / "artifacts/source.json", source)
        with self.assertRaisesRegex(ValueError, "does not match frozen request"):
            prepare_handoff(self.root, self.base / "mismatch", ["reports/latest.json"])

    def test_selected_paths_are_explicit_and_duplicate_free(self):
        with self.assertRaisesRegex(ValueError, "non-empty list"):
            prepare_handoff(self.root, self.base / "empty", [])
        with self.assertRaisesRegex(ValueError, "duplicated"):
            prepare_handoff(
                self.root,
                self.base / "duplicate",
                ["reports/latest.json", "reports/latest.json"],
            )


if __name__ == "__main__":
    unittest.main()
