"""Focused local tests for immutable MDK and Gradle environment seeds."""
from concurrent.futures import ThreadPoolExecutor
import hashlib
import os
from pathlib import Path
import subprocess
import tempfile
from threading import Barrier, Event
import unittest
from unittest.mock import patch

import modport.environment_build_cache as environment_build_cache

from modport.environment_build_cache import (
    EnvironmentBuildCache,
    gradle_cache_compatibility,
)


MDK_URL = "https://github.com/example/MDK.git"
NEOFORGE_VERSION = "26.1.2.106"


def git(path: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(path), "-c", "user.name=Fixture",
         "-c", "user.email=fixture@example.invalid", *args],
        text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
    )
    if result.returncode:
        raise AssertionError(result.stderr)
    return result.stdout.strip()


class EnvironmentBuildCacheTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.cache = EnvironmentBuildCache(self.root / "host-cache")
        self.checkout = self.root / "mdk-source"
        self.checkout.mkdir()
        subprocess.run(["git", "init", str(self.checkout)], check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        (self.checkout / "build.gradle").write_text("plugins { id 'java' }\n", encoding="utf-8")
        git(self.checkout, "add", "build.gradle")
        git(self.checkout, "commit", "-m", "fixture MDK")
        git(self.checkout, "remote", "add", "origin", MDK_URL)
        self.mdk_commit = git(self.checkout, "rev-parse", "HEAD")

    def gradle_tree(self, label="seed") -> Path:
        modules = self.root / label / "caches" / "modules-2"
        artifact = modules / "files-2.1" / "org.example" / "library" / "1.0" / "abc" / "library-1.0.jar"
        artifact.parent.mkdir(parents=True)
        artifact.write_bytes(b"immutable dependency bytes")
        metadata = modules / "metadata-2.107" / "descriptors" / "org.example" / "library" / "1.0" / "descriptor.bin"
        metadata.parent.mkdir(parents=True)
        metadata.write_bytes(b"gradle module descriptor")
        (modules / "gc.properties").write_text("discard", encoding="utf-8")
        (modules / "metadata-2.107" / "metadata.lock").write_text("discard", encoding="utf-8")
        return modules

    def publish_gradle(self, modules: Path, *, version="9.1.0", target=NEOFORGE_VERSION):
        return self.cache.publish_gradle_modules(
            modules, version, mdk_repository=MDK_URL, mdk_commit=self.mdk_commit,
            neoforge_version=target, mdk_bootstrap_succeeded=True,
        )

    def test_gradle_cache_compatibility_families_and_unknown_versions(self):
        self.assertEqual(gradle_cache_compatibility("8.10.2"), "modules-2-metadata-2.106")
        self.assertEqual(gradle_cache_compatibility("8.11.0"), "modules-2-metadata-2.107")
        self.assertEqual(gradle_cache_compatibility("9.1.0"), "modules-2-metadata-2.107")
        self.assertNotEqual(gradle_cache_compatibility("8.10.2"),
                            gradle_cache_compatibility("8.11.0"))
        for version in ("8.10.3", "9.9.0", "9.1-rc-1", "10.0.0", "not-a-version"):
            with self.subTest(version=version), self.assertRaises(ValueError):
                gradle_cache_compatibility(version)

    def test_exact_mdk_bundle_materializes_without_alternates_or_shared_objects(self):
        record = self.cache.publish_mdk_bundle(self.checkout, MDK_URL, self.mdk_commit)
        self.assertEqual(record["commit"], self.mdk_commit)
        self.assertEqual(record["repository"], MDK_URL)
        bundle = self.root / "host-cache" / "mdk-bundles" / record["cache_key"] / "mdk.bundle"
        self.assertEqual(hashlib.sha256(bundle.read_bytes()).hexdigest(), record["bundle_sha256"])
        destination = self.root / "run" / "toolchains" / "mdk"
        materialized = self.cache.materialize_mdk(MDK_URL, self.mdk_commit, destination)
        self.assertEqual(materialized["materialized_path"], str(destination))
        self.assertEqual(git(destination, "rev-parse", "HEAD"), self.mdk_commit)
        self.assertEqual(git(destination, "remote", "get-url", "origin"), MDK_URL)
        self.assertEqual(git(destination, "status", "--porcelain"), "")
        self.assertFalse((destination / ".git" / "objects" / "info" / "alternates").exists())
        cache_inodes = {(entry.stat().st_dev, entry.stat().st_ino)
                        for entry in (destination / ".git" / "objects").rglob("*") if entry.is_file()}
        source_inodes = {(entry.stat().st_dev, entry.stat().st_ino)
                         for entry in (self.checkout / ".git" / "objects").rglob("*") if entry.is_file()}
        self.assertFalse(cache_inodes & source_inodes)
        self.assertTrue(self.cache.materialize_mdk(MDK_URL, self.mdk_commit, destination))

    def test_try_materialize_only_returns_none_for_a_missing_seed(self):
        cold_cache = EnvironmentBuildCache(self.root / "cold-cache")
        destination = self.root / "cold-run" / "mdk"
        self.assertIsNone(cold_cache.try_materialize_mdk(MDK_URL, self.mdk_commit, destination))
        self.cache.publish_mdk_bundle(self.checkout, MDK_URL, self.mdk_commit)
        record = self.cache.publish_mdk_bundle(self.checkout, MDK_URL, self.mdk_commit)
        bundle = self.root / "host-cache" / "mdk-bundles" / record["cache_key"] / "mdk.bundle"
        bundle.chmod(0o644)
        bundle.write_bytes(b"tampered")
        with self.assertRaisesRegex(ValueError, "SHA-256"):
            self.cache.try_materialize_mdk(MDK_URL, self.mdk_commit, destination)

    def test_mdk_source_identity_dirty_checkout_and_bundle_tampering_fail_closed(self):
        with self.assertRaisesRegex(ValueError, "origin"):
            self.cache.publish_mdk_bundle(self.checkout, "https://example.invalid/wrong.git", self.mdk_commit)
        (self.checkout / "untracked.txt").write_text("dirty", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "clean"):
            self.cache.publish_mdk_bundle(self.checkout, MDK_URL, self.mdk_commit)
        (self.checkout / "untracked.txt").unlink()
        record = self.cache.publish_mdk_bundle(self.checkout, MDK_URL, self.mdk_commit)
        bundle = self.root / "host-cache" / "mdk-bundles" / record["cache_key"] / "mdk.bundle"
        bundle.chmod(0o644)
        bundle.write_bytes(b"tampered")
        with self.assertRaisesRegex(ValueError, "SHA-256"):
            self.cache.materialize_mdk(MDK_URL, self.mdk_commit, self.root / "bad-checkout")

    def test_concurrent_mdk_publication_is_idempotent(self):
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(
                lambda _: self.cache.publish_mdk_bundle(self.checkout, MDK_URL, self.mdk_commit), range(2)
            ))
        self.assertEqual(results[0]["cache_key"], results[1]["cache_key"])
        self.assertEqual(results[0]["bundle_sha256"], results[1]["bundle_sha256"])

    def test_shallow_mdk_checkout_can_seed_a_self_contained_bundle(self):
        full = self.root / "full-history"
        full.mkdir()
        subprocess.run(["git", "init", str(full)], check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        (full / "build.gradle").write_text("plugins { id 'java' }\n", encoding="utf-8")
        git(full, "add", "build.gradle")
        git(full, "commit", "-m", "old MDK")
        (full / "build.gradle").write_text("plugins { id 'java-library' }\n", encoding="utf-8")
        git(full, "add", "build.gradle")
        git(full, "commit", "-m", "current MDK")
        bare = self.root / "upstream.git"
        subprocess.run(["git", "clone", "--bare", str(full), str(bare)], check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        shallow = self.root / "shallow-mdk"
        environment = {**os.environ, "GIT_ALLOW_PROTOCOL": "file"}
        subprocess.run(["git", "clone", "--depth=1", bare.as_uri(), str(shallow)],
                       check=True, env=environment, stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL)
        git(shallow, "remote", "set-url", "origin", MDK_URL)
        commit = git(shallow, "rev-parse", "HEAD")
        self.assertEqual(git(shallow, "rev-parse", "--is-shallow-repository"), "true")
        record = self.cache.publish_mdk_bundle(shallow, MDK_URL, commit)
        destination = self.root / "shallow-run" / "mdk"
        self.cache.materialize_mdk(MDK_URL, commit, destination)
        self.assertEqual(git(destination, "rev-parse", "HEAD"), commit)
        self.assertEqual((destination / "build.gradle").read_text(encoding="utf-8"),
                         "plugins { id 'java-library' }\n")
        self.assertEqual(record["commit"], commit)

    def test_gradle_seed_is_readonly_scoped_and_private_materialization(self):
        modules = self.gradle_tree()
        published = self.publish_gradle(modules)
        self.assertEqual(published["compatibility"], "modules-2-metadata-2.107")
        snapshot = Path(published["snapshot_path"])
        snapshot_modules = snapshot / "modules-2"
        self.assertFalse((snapshot_modules / "gc.properties").exists())
        self.assertFalse(any(path.name.endswith(".lock") for path in snapshot_modules.rglob("*")))
        source_file = modules / "files-2.1" / "org.example" / "library" / "1.0" / "abc" / "library-1.0.jar"
        copied_file = snapshot_modules / source_file.relative_to(modules)
        self.assertNotEqual((source_file.stat().st_dev, source_file.stat().st_ino),
                            (copied_file.stat().st_dev, copied_file.stat().st_ino))
        self.assertFalse(snapshot.stat().st_mode & 0o222)
        self.assertFalse(snapshot_modules.stat().st_mode & 0o222)
        self.assertFalse(copied_file.stat().st_mode & 0o222)
        selected = self.cache.compatible_gradle_snapshots(
            "9.2.0", mdk_repository=MDK_URL, mdk_commit=self.mdk_commit,
            neoforge_version=NEOFORGE_VERSION,
        )
        self.assertEqual([published["snapshot_key"]], [item["snapshot_key"] for item in selected])
        ro_path = self.cache.read_only_gradle_cache(
            published["snapshot_key"], "9.2.0", mdk_repository=MDK_URL,
            mdk_commit=self.mdk_commit, neoforge_version=NEOFORGE_VERSION,
        )
        self.assertEqual(ro_path, snapshot)
        destination = self.root / "run" / "caches" / "modules-2"
        self.cache.materialize_gradle_modules(
            published["snapshot_key"], destination, "9.2.0", mdk_repository=MDK_URL,
            mdk_commit=self.mdk_commit, neoforge_version=NEOFORGE_VERSION,
        )
        self.assertEqual((destination / source_file.relative_to(modules)).read_bytes(), source_file.read_bytes())
        self.assertTrue((destination / source_file.relative_to(modules)).stat().st_mode & 0o200)
        with self.assertRaises(ValueError):
            self.cache.read_only_gradle_cache(
                published["snapshot_key"], "9.2.0", mdk_repository=MDK_URL,
                mdk_commit=self.mdk_commit, neoforge_version="26.1.2.105",
            )

    def test_gradle_requires_successful_bootstrap_and_safe_cache_tree(self):
        modules = self.gradle_tree()
        with self.assertRaisesRegex(ValueError, "successful clean MDK bootstrap"):
            self.cache.publish_gradle_modules(
                modules, "9.1.0", mdk_repository=MDK_URL, mdk_commit=self.mdk_commit,
                neoforge_version=NEOFORGE_VERSION, mdk_bootstrap_succeeded=False,
            )
        link = modules / "unsafe-link"
        link.symlink_to(modules / "gc.properties")
        with self.assertRaisesRegex(ValueError, "symlink"):
            self.publish_gradle(modules)
        link.unlink()
        fifo = modules / "special"
        os.mkfifo(fifo)
        with self.assertRaisesRegex(ValueError, "special"):
            self.publish_gradle(modules)

    def test_gradle_snapshot_detects_tamper_and_shared_writable_store(self):
        published = self.publish_gradle(self.gradle_tree())
        snapshot = Path(published["snapshot_path"])
        target = snapshot / "modules-2" / "files-2.1" / "org.example" / "library" / "1.0" / "abc" / "library-1.0.jar"
        target.chmod(0o644)
        target.write_bytes(b"tampered cache")
        target.chmod(0o444)
        with self.assertRaisesRegex(ValueError, "hash manifest"):
            self.cache.read_only_gradle_cache(
                published["snapshot_key"], "9.1.0", mdk_repository=MDK_URL,
                mdk_commit=self.mdk_commit, neoforge_version=NEOFORGE_VERSION,
            )
        cache_root = self.root / "host-cache"
        cache_root.chmod(0o777)
        with self.assertRaisesRegex(ValueError, "group/world writable"):
            self.cache.compatible_gradle_snapshots(
                "9.1.0", mdk_repository=MDK_URL, mdk_commit=self.mdk_commit,
                neoforge_version=NEOFORGE_VERSION,
            )

    def test_concurrent_gradle_publication_is_idempotent_and_scope_bound(self):
        modules = self.gradle_tree()
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: self.publish_gradle(modules), range(2)))
        self.assertEqual(results[0]["snapshot_key"], results[1]["snapshot_key"])
        self.assertEqual([], self.cache.compatible_gradle_snapshots(
            "9.1.0", mdk_repository=MDK_URL, mdk_commit=self.mdk_commit,
            neoforge_version="26.1.2.105",
        ))

    def test_gradle_catalog_reader_skips_pending_publication_without_waiting(self):
        modules = self.gradle_tree()
        copy_started = Event()
        finish_copy = Event()
        copy_files = environment_build_cache._copy_tree_files

        def paused_copy(source, destination, files):
            copy_started.set()
            if not finish_copy.wait(timeout=5):
                raise TimeoutError("test did not release the staged snapshot copy")
            copy_files(source, destination, files)

        with patch("modport.environment_build_cache._copy_tree_files", side_effect=paused_copy):
            with ThreadPoolExecutor(max_workers=2) as pool:
                publishing = pool.submit(self.publish_gradle, modules)
                self.assertTrue(copy_started.wait(timeout=5), "publisher never reached staged copy")
                try:
                    reading = pool.submit(
                        self.cache.compatible_gradle_snapshots,
                        "9.1.0", mdk_repository=MDK_URL,
                        mdk_commit=self.mdk_commit, neoforge_version=NEOFORGE_VERSION,
                    )
                    self.assertEqual([], reading.result(timeout=2))
                finally:
                    finish_copy.set()
                published = publishing.result(timeout=5)

        selected = self.cache.compatible_gradle_snapshots(
            "9.1.0", mdk_repository=MDK_URL, mdk_commit=self.mdk_commit,
            neoforge_version=NEOFORGE_VERSION,
        )
        self.assertEqual([published["snapshot_key"]], [item["snapshot_key"] for item in selected])

    def test_different_gradle_snapshot_entries_publish_concurrently(self):
        first = self.gradle_tree("first-gradle-cache")
        second = self.gradle_tree("second-gradle-cache")
        artifact = second / "files-2.1" / "org.example" / "library" / "1.0" / "abc" / "library-1.0.jar"
        artifact.write_bytes(b"different dependency bytes")
        copies_entered = Barrier(2)
        copy_files = environment_build_cache._copy_tree_files

        def rendezvous_copy(source, destination, files):
            copies_entered.wait(timeout=5)
            copy_files(source, destination, files)

        with patch("modport.environment_build_cache._copy_tree_files", side_effect=rendezvous_copy):
            with ThreadPoolExecutor(max_workers=2) as pool:
                futures = [pool.submit(self.publish_gradle, modules)
                           for modules in (first, second)]
                results = [future.result(timeout=10) for future in futures]

        self.assertNotEqual(results[0]["snapshot_key"], results[1]["snapshot_key"])
        selected = self.cache.compatible_gradle_snapshots(
            "9.1.0", mdk_repository=MDK_URL, mdk_commit=self.mdk_commit,
            neoforge_version=NEOFORGE_VERSION,
        )
        self.assertEqual({item["snapshot_key"] for item in results},
                         {item["snapshot_key"] for item in selected})


if __name__ == "__main__":
    unittest.main()
