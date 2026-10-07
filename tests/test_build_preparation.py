from dataclasses import replace
from hashlib import sha256
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from modport import Budget, LockedManifest, MigrationRequest
from modport.build_preparation import (
    BuildPreparationIntegrityError,
    StaleBuildPreparationPlan,
    UnsupportedBuildLayout,
    apply_build_preparation,
    authenticate_target_config,
    draft_build_preparation,
    revert_build_preparation,
)


def git(root: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(root), "-c", "user.name=Fixture",
         "-c", "user.email=fixture@example.invalid", *arguments],
        text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
    )
    if result.returncode:
        raise AssertionError(result.stderr)
    return result.stdout.strip()


class BuildPreparationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.worktree = self.root / "worktree"
        self.mdk = self.root / "toolchains" / "mdk"
        self.worktree.mkdir()
        self.mdk.mkdir(parents=True)
        git(self.worktree, "init")
        (self.worktree / "Example.java").write_text("final class Example {}\n", encoding="utf-8")
        git(self.worktree, "add", ".")
        git(self.worktree, "commit", "-m", "source")
        self.contents = {
            "build.gradle": "plugins { id 'net.neoforged.moddev' version '2.0.99' }\n",
            "settings.gradle": "pluginManagement { repositories { gradlePluginPortal() } }\n",
            "gradle.properties": "org.gradle.jvmargs=-Xmx2G\n",
            "gradle/wrapper/gradle-wrapper.properties": (
                "distributionUrl=https\\://services.gradle.org/distributions/gradle-9.1-bin.zip\n"
            ),
            "src/main/templates/META-INF/neoforge.mods.toml": "modLoader=\"javafml\"\n",
            "gradlew": "#!/bin/sh\nexec gradle \"$@\"\n",
            "gradlew.bat": "@echo off\r\ngradle %*\r\n",
        }
        self.binary_contents = {
            "gradle/wrapper/gradle-wrapper.jar": b"PK\x03\x04locked-wrapper-fixture\x00",
        }
        checksums = {}
        for relative, text in self.contents.items():
            path = self.mdk / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
            checksums[f"mdk:{relative}"] = sha256(text.encode()).hexdigest()
        for relative, data in self.binary_contents.items():
            path = self.mdk / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
            checksums[f"mdk:{relative}"] = sha256(data).hexdigest()
        request = MigrationRequest(
            "example", "https://example.invalid/source.git", "1.20.1", "1.21.1",
            budget=Budget(max_seconds=30),
        )
        self.manifest = LockedManifest(
            request, "21.1.77", source_commit=git(self.worktree, "rev-parse", "HEAD"),
            mdk_repository="https://github.com/neoforged/MDK-1.21-ModDevGradle.git",
            mdk_commit="a" * 40, checksums=checksums,
        )

    def install_exact_mdk(self) -> None:
        for relative, text in self.contents.items():
            path = self.worktree / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(text.encode())
        for relative, data in self.binary_contents.items():
            path = self.worktree / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)

    def test_missing_exact_files_plan_apply_idempotently_and_revert(self):
        plan = draft_build_preparation(self.root, self.worktree, self.manifest)
        self.assertTrue(plan.supported, plan.diagnostics)
        expected_paths = set(self.contents) | set(self.binary_contents)
        self.assertEqual(expected_paths, {change.path for change in plan.changes})
        report = plan.to_dict()
        self.assertNotIn(str(self.root), json.dumps(report))
        self.assertFalse(report["acceptance_evidence"])
        self.assertIn("source-template", " ".join(report["remaining_scope"]))

        result = apply_build_preparation(self.root, self.worktree, self.manifest, plan)
        self.assertEqual("applied", result.state)
        for relative, text in self.contents.items():
            self.assertEqual(text.encode(), (self.worktree / relative).read_bytes())
        for relative, data in self.binary_contents.items():
            self.assertEqual(data, (self.worktree / relative).read_bytes())
        repeated = apply_build_preparation(self.root, self.worktree, self.manifest, plan)
        self.assertEqual("already_applied", repeated.state)
        self.assertEqual(0, repeated.applied_changes)

        reverted = revert_build_preparation(self.root, self.worktree, self.manifest, plan)
        self.assertEqual("reverted", reverted.state)
        self.assertTrue(all(not (self.worktree / relative).exists()
                            for relative in expected_paths))

    def test_exact_committed_configuration_authenticates_candidate_and_marker(self):
        plan = draft_build_preparation(self.root, self.worktree, self.manifest)
        apply_build_preparation(self.root, self.worktree, self.manifest, plan)
        git(self.worktree, "add", ".")
        git(self.worktree, "commit", "-m", "add locked MDK skeleton")
        marker = authenticate_target_config(self.worktree, self.manifest, self.mdk)
        self.assertEqual(git(self.worktree, "rev-parse", "HEAD"), marker.candidate_identity)
        self.assertEqual(set(self.contents) | set(self.binary_contents),
                         {item.path for item in marker.files})
        self.assertEqual({"neo_version": self.manifest.neoforge_version},
                         dict(marker.required_gradle_properties))
        self.assertEqual(marker, authenticate_target_config(
            self.worktree, self.manifest, self.mdk, marker.to_dict()))
        forged = marker.to_dict()
        forged["candidate_identity"]["value"] = "0" * 40
        with self.assertRaises(StaleBuildPreparationPlan):
            authenticate_target_config(self.worktree, self.manifest, self.mdk, forged)

    def test_comment_or_wrong_plugin_version_never_authenticates(self):
        self.install_exact_mdk()
        build = self.worktree / "build.gradle"
        for index, content in enumerate((
            "// id 'net.neoforged.moddev' version '2.0.99'\nplugins { id 'java' }\n",
            "plugins { id 'net.neoforged.moddev' version '0.0.1' }\n",
        )):
            with self.subTest(index=index):
                build.write_text(content, encoding="utf-8")
                git(self.worktree, "add", ".")
                git(self.worktree, "commit", "-m", f"unsupported build {index}")
                with self.assertRaises(UnsupportedBuildLayout):
                    authenticate_target_config(self.worktree, self.manifest, self.mdk)

    def test_custom_gradle_is_unsupported_and_never_overwritten(self):
        custom = "plugins { id 'java' }\ndependencies { implementation('custom:library:1') }\n"
        (self.worktree / "build.gradle").write_text(custom, encoding="utf-8")
        git(self.worktree, "add", "build.gradle")
        git(self.worktree, "commit", "-m", "custom build")
        plan = draft_build_preparation(self.root, self.worktree, self.manifest)
        self.assertFalse(plan.supported)
        self.assertEqual((), plan.changes)
        row = next(item for item in plan.draft if item["path"] == "build.gradle")
        self.assertEqual("manual_merge_required", row["action"])
        self.assertEqual("locked_source_template_absent", row["reason"])
        with self.assertRaises(UnsupportedBuildLayout):
            apply_build_preparation(self.root, self.worktree, self.manifest, plan)
        self.assertEqual(custom, (self.worktree / "build.gradle").read_text(encoding="utf-8"))
        with self.assertRaises(UnsupportedBuildLayout):
            authenticate_target_config(self.worktree, self.manifest, self.mdk)

    def test_kotlin_or_multimodule_layout_is_report_only(self):
        path = self.worktree / "module" / "build.gradle.kts"
        path.parent.mkdir()
        path.write_text("plugins { java }\n", encoding="utf-8")
        git(self.worktree, "add", ".")
        git(self.worktree, "commit", "-m", "multi project")
        plan = draft_build_preparation(self.root, self.worktree, self.manifest)
        self.assertFalse(plan.supported)
        self.assertEqual((), plan.changes)
        self.assertTrue(any("module/build.gradle.kts" in item for item in plan.diagnostics))

    def test_ordinary_mod_descriptor_is_not_a_gradle_layout_ambiguity(self):
        descriptor = self.worktree / "src/main/resources/META-INF/neoforge.mods.toml"
        descriptor.parent.mkdir(parents=True)
        descriptor.write_text('modLoader="javafml"\n', encoding="utf-8")
        git(self.worktree, "add", ".")
        git(self.worktree, "commit", "-m", "add mod descriptor")
        plan = draft_build_preparation(self.root, self.worktree, self.manifest)
        self.assertTrue(plan.supported, plan.diagnostics)
        self.assertFalse(any(descriptor.name in item for item in plan.diagnostics))

    def test_locked_mdk_hash_mismatch_fails_before_candidate_write(self):
        bad = replace(self.manifest, checksums={
            **dict(self.manifest.checksums), "mdk:build.gradle": "0" * 64,
        })
        with self.assertRaisesRegex(BuildPreparationIntegrityError, "SHA-256 mismatch"):
            draft_build_preparation(self.root, self.worktree, bad)
        self.assertFalse((self.worktree / "build.gradle").exists())

    def test_missing_wrapper_lock_is_an_unsupported_draft(self):
        checksums = dict(self.manifest.checksums)
        del checksums["mdk:gradle/wrapper/gradle-wrapper.jar"]
        plan = draft_build_preparation(
            self.root, self.worktree, replace(self.manifest, checksums=checksums)
        )
        self.assertFalse(plan.supported)
        self.assertEqual((), plan.changes)
        self.assertTrue(any("gradle-wrapper.jar" in item for item in plan.diagnostics))

    def test_mdk_changed_after_plan_is_rejected_before_apply(self):
        plan = draft_build_preparation(self.root, self.worktree, self.manifest)
        (self.mdk / "build.gradle").write_text("tampered\n", encoding="utf-8")
        with self.assertRaises(BuildPreparationIntegrityError):
            apply_build_preparation(self.root, self.worktree, self.manifest, plan)
        self.assertFalse((self.worktree / "build.gradle").exists())

    def test_apply_rebuilds_private_payload_from_authenticated_mdk(self):
        plan = draft_build_preparation(self.root, self.worktree, self.manifest)
        first, *remaining = plan.changes
        forged = replace(
            plan,
            changes=(replace(first, _after=b"forged by caller\n"), *remaining),
        )
        apply_build_preparation(self.root, self.worktree, self.manifest, forged)
        self.assertEqual(
            (self.mdk / first.path).read_bytes(),
            (self.worktree / first.path).read_bytes(),
        )

    def test_directory_fsync_failure_removes_linked_file_and_keeps_candidate_clean(self):
        plan = draft_build_preparation(self.root, self.worktree, self.manifest)
        expected_paths = {change.path for change in plan.changes}
        with patch("modport.build_preparation.os.fsync",
                   side_effect=[None, OSError("injected directory fsync failure")]):
            with self.assertRaisesRegex(OSError, "injected directory fsync failure"):
                apply_build_preparation(self.root, self.worktree, self.manifest, plan)
        self.assertEqual("", git(self.worktree, "status", "--porcelain", "--untracked-files=all"))
        self.assertTrue(all(not (self.worktree / relative).exists()
                            for relative in expected_paths))

    def test_empty_addition_plan_and_revert_preserve_original_head(self):
        self.install_exact_mdk()
        git(self.worktree, "add", ".")
        git(self.worktree, "commit", "-m", "exact MDK configuration")
        original = git(self.worktree, "rev-parse", "HEAD")
        plan = draft_build_preparation(self.root, self.worktree, self.manifest)
        self.assertTrue(plan.supported)
        self.assertEqual((), plan.changes)
        applied = apply_build_preparation(self.root, self.worktree, self.manifest, plan)
        self.assertEqual(0, applied.applied_changes)
        reverted = revert_build_preparation(self.root, self.worktree, self.manifest, plan)
        self.assertEqual(0, reverted.applied_changes)
        self.assertEqual(original, git(self.worktree, "rev-parse", "HEAD"))

    def test_candidate_identity_change_rejects_apply(self):
        plan = draft_build_preparation(self.root, self.worktree, self.manifest)
        (self.worktree / "note.txt").write_text("changed\n", encoding="utf-8")
        git(self.worktree, "add", "note.txt")
        git(self.worktree, "commit", "-m", "new candidate")
        with self.assertRaisesRegex(StaleBuildPreparationPlan, "candidate identity"):
            apply_build_preparation(self.root, self.worktree, self.manifest, plan)


if __name__ == "__main__":
    unittest.main()
