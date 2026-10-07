"""Focused tests for private Wrapper/JDK cache publication and materialization."""
import hashlib
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import subprocess
import stat
import tempfile
import threading
import unittest

from modport.environment_toolchain_cache import (
    EnvironmentToolchainCache,
)


MDK_URL = "https://github.com/example/MDK.git"
NEOFORGE_VERSION = "26.1.2.106"
GRADLE_VERSION = "9.2.1"
JAVA_VERSION = "25"



def git(path: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(path), "-c", "user.name=Fixture",
         "-c", "user.email=fixture@example.invalid", *args],
        text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
    )
    if result.returncode:
        raise AssertionError(result.stderr)
    return result.stdout.strip()


class EnvironmentToolchainCacheTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.cache = EnvironmentToolchainCache(self.root / "host-cache")
        self.run_root = self.root / "source-run"
        self.mdk = self.run_root / "toolchains" / "mdk"
        self.mdk.mkdir(parents=True)
        subprocess.run(["git", "init", str(self.mdk)], check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        (self.mdk / "build.gradle").write_text("plugins { id 'java' }\n", encoding="utf-8")
        git(self.mdk, "add", "build.gradle")
        git(self.mdk, "commit", "-m", "fixture MDK")
        git(self.mdk, "remote", "add", "origin", MDK_URL)
        self.mdk_commit = git(self.mdk, "rev-parse", "HEAD")
        self.gradle_home = self.run_root / "toolchains" / "gradle-cache"
        wrapper_dist = self.gradle_home / "wrapper" / "dists" / f"gradle-{GRADLE_VERSION}-bin" / "dist-hash"
        wrapper_dist.mkdir(parents=True)
        (wrapper_dist / f"gradle-{GRADLE_VERSION}-bin.zip").write_bytes(b"gradle wrapper archive")
        (wrapper_dist / f"gradle-{GRADLE_VERSION}-bin.zip.ok").write_bytes(b"")
        self.java = self.gradle_home / "jdks" / "temurin-25-linux-x64" / "bin" / "java"
        self.java.parent.mkdir(parents=True)
        self.java.write_bytes(b"verified java executable bytes")
        self.java.chmod(0o755)
        (self.java.parents[1] / "release").write_text('JAVA_VERSION="25"\n', encoding="utf-8")
        self.java_sha256 = hashlib.sha256(self.java.read_bytes()).hexdigest()

    def publish(self):
        return self.cache.publish_bootstrap_toolchains(
            self.run_root,
            mdk_bootstrap_succeeded=True,
            mdk_repository=MDK_URL,
            mdk_commit=self.mdk_commit,
            neoforge_version=NEOFORGE_VERSION,
            gradle_version=GRADLE_VERSION,
            java_version=JAVA_VERSION,
            verified_java=self.java,
            verified_java_sha256=self.java_sha256,
        )

    def key_options(self):
        return {
            "mdk_repository": MDK_URL,
            "mdk_commit": self.mdk_commit,
            "neoforge_version": NEOFORGE_VERSION,
            "gradle_version": GRADLE_VERSION,
            "java_version": JAVA_VERSION,
            "verified_java_sha256": self.java_sha256,
        }

    def test_cold_publish_and_hot_materialize_are_private_copies(self):
        record = self.publish()
        self.assertEqual("gradle-wrapper-jdk-v1", record["kind"])
        self.assertEqual(self.java_sha256, record["identity"]["java_sha256"])
        entry = Path(record["cache_path"])
        cached_wrapper = entry / "wrapper/dists" / f"gradle-{GRADLE_VERSION}-bin/dist-hash/gradle-{GRADLE_VERSION}-bin.zip"
        cached_java = entry / record["verified_java_path"]
        self.assertEqual(b"gradle wrapper archive", cached_wrapper.read_bytes())
        self.assertEqual(self.java.read_bytes(), cached_java.read_bytes())
        self.assertNotEqual((self.java.stat().st_dev, self.java.stat().st_ino),
                            (cached_java.stat().st_dev, cached_java.stat().st_ino))

        new_run = self.root / "new-run"
        new_run.mkdir()
        scope = self.key_options()
        scope.pop("verified_java_sha256")
        compatible = self.cache.compatible_toolchain_snapshots(**scope)
        self.assertEqual([record["cache_key"]], [item["cache_key"] for item in compatible])
        materialized = self.cache.try_materialize_toolchains(
            new_run, **scope,
        )
        self.assertEqual(self.java_sha256, materialized["identity"]["java_sha256"])
        self.assertEqual(str(new_run / "toolchains/gradle-cache"),
                         materialized["materialized_gradle_home"])
        new_java = Path(materialized["materialized_java"])
        new_wrapper = new_run / "toolchains/gradle-cache/wrapper/dists" / f"gradle-{GRADLE_VERSION}-bin/dist-hash/gradle-{GRADLE_VERSION}-bin.zip"
        self.assertEqual(self.java.read_bytes(), new_java.read_bytes())
        self.assertEqual(b"gradle wrapper archive", new_wrapper.read_bytes())
        self.assertNotEqual((cached_java.stat().st_dev, cached_java.stat().st_ino),
                            (new_java.stat().st_dev, new_java.stat().st_ino))
        self.assertNotEqual((cached_wrapper.stat().st_dev, cached_wrapper.stat().st_ino),
                            (new_wrapper.stat().st_dev, new_wrapper.stat().st_ino))
        self.assertTrue(new_java.stat().st_mode & 0o111)
        self.assertTrue(new_java.stat().st_mode & 0o200)

    def test_missing_source_or_cache_is_a_miss(self):
        empty_run = self.root / "empty-run"
        empty_run.mkdir()
        self.assertIsNone(self.cache.try_materialize_toolchains(
            empty_run, **self.key_options(),
        ))
        self.assertFalse((empty_run / "toolchains/gradle-cache").exists())

        missing_source_run = self.root / "missing-source"
        missing_source_run.mkdir()
        (missing_source_run / "toolchains/mdk").mkdir(parents=True)
        self.assertIsNone(self.cache.publish_bootstrap_toolchains(
            missing_source_run,
            mdk_bootstrap_succeeded=True,
            mdk_repository=MDK_URL,
            mdk_commit=self.mdk_commit,
            neoforge_version=NEOFORGE_VERSION,
            gradle_version=GRADLE_VERSION,
            java_version=JAVA_VERSION,
            verified_java=missing_source_run / "toolchains/gradle-cache/jdks/missing/bin/java",
            verified_java_sha256=self.java_sha256,
        ))

    def test_cached_tamper_fails_closed(self):
        record = self.publish()
        cache_java = Path(record["cache_path"]) / record["verified_java_path"]
        cache_java.chmod(0o755)
        cache_java.write_bytes(b"tampered Java")
        cache_java.chmod(0o555)
        with self.assertRaisesRegex(ValueError, "hash manifest"):
            self.cache.try_materialize_toolchains(self.root / "new-run", **self.key_options())

    def test_failed_or_dirty_bootstrap_and_unsafe_inputs_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "successful MDK compileJava"):
            self.cache.publish_bootstrap_toolchains(
                self.run_root, mdk_bootstrap_succeeded=False,
                mdk_repository=MDK_URL, mdk_commit=self.mdk_commit,
                neoforge_version=NEOFORGE_VERSION, gradle_version=GRADLE_VERSION,
                java_version=JAVA_VERSION, verified_java=self.java,
                verified_java_sha256=self.java_sha256,
            )
        (self.mdk / "dirty.txt").write_text("dirty", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "not clean"):
            self.publish()
        (self.mdk / "dirty.txt").unlink()

        wrapper_lock = self.gradle_home / "wrapper/dists" / f"gradle-{GRADLE_VERSION}-bin/dist-hash/gradle-{GRADLE_VERSION}-bin.zip.lck"
        jdk_lock = self.java.parents[2] / "eclipse_adoptium-25-amd64-linux.2.reserved.lock"
        wrapper_lock.write_text("transient", encoding="utf-8")
        jdk_lock.write_text("transient", encoding="utf-8")
        record = self.publish()
        self.assertFalse(any(Path(name).name.endswith((".lck", ".lock"))
                             for name in record["files"]))
        wrapper_lock.unlink()
        jdk_lock.unlink()

        java_link = self.java.parent / "java-link"
        java_link.symlink_to(self.java)
        with self.assertRaises(ValueError):
            self.cache.publish_bootstrap_toolchains(
                self.run_root, mdk_bootstrap_succeeded=True,
                mdk_repository=MDK_URL, mdk_commit=self.mdk_commit,
                neoforge_version=NEOFORGE_VERSION, gradle_version=GRADLE_VERSION,
                java_version=JAVA_VERSION, verified_java=java_link,
                verified_java_sha256=self.java_sha256,
            )

    def test_cache_snapshot_lock_file_is_rejected(self):
        record = self.publish()
        wrapper_dir = Path(record["cache_path"]) / "wrapper/dists" / f"gradle-{GRADLE_VERSION}-bin/dist-hash"
        wrapper_dir.chmod(0o755)
        (wrapper_dir / "unexpected.lock").write_text("must not ship", encoding="utf-8")
        with self.assertRaises(ValueError):
            self.cache.try_materialize_toolchains(self.root / "new-run", **self.key_options())

    def test_unrelated_scope_corruption_does_not_block_compatible_lookup(self):
        record = self.publish()
        unrelated = self.cache.root / "wrapper-jdks" / "unrelated-scope" / "bad-entry"
        unrelated.mkdir(parents=True)
        (unrelated / "manifest.json").write_text("not json", encoding="utf-8")

        scope = self.key_options()
        scope.pop("verified_java_sha256")
        compatible = self.cache.compatible_toolchain_snapshots(**scope)
        self.assertEqual([record["cache_key"]], [item["cache_key"] for item in compatible])

    def test_concurrent_publish_and_materialize_use_complete_private_entries(self):
        barrier = threading.Barrier(2)

        def publish():
            barrier.wait(timeout=5)
            return self.publish()

        with ThreadPoolExecutor(max_workers=2) as executor:
            published = list(executor.map(lambda _: publish(), range(2)))
        self.assertEqual(published[0]["cache_key"], published[1]["cache_key"])
        self.assertEqual(published[0]["files"], published[1]["files"])

        new_runs = [self.root / "parallel-run-a", self.root / "parallel-run-b"]
        for run_root in new_runs:
            run_root.mkdir()

        def materialize(run_root):
            return self.cache.try_materialize_toolchains(run_root, **self.key_options())

        with ThreadPoolExecutor(max_workers=2) as executor:
            materialized = list(executor.map(materialize, new_runs))
        self.assertTrue(all(record is not None for record in materialized))
        java_paths = [Path(record["materialized_java"]) for record in materialized]
        self.assertEqual([self.java.read_bytes()] * 2,
                         [path.read_bytes() for path in java_paths])
        self.assertNotEqual((java_paths[0].stat().st_dev, java_paths[0].stat().st_ino),
                            (java_paths[1].stat().st_dev, java_paths[1].stat().st_ino))

    def test_adoptium_0777_legal_file_is_normalized_in_host_and_run_copies(self):
        legal_notice = (self.java.parents[1] / "legal" / "java.desktop"
                        / "ASSEMBLY_EXCEPTION")
        legal_notice.parent.mkdir(parents=True)
        legal_notice.write_text("fixture legal notice\n", encoding="utf-8")
        legal_notice.chmod(0o777)

        record = self.publish()
        relative = f"jdks/{legal_notice.relative_to(self.gradle_home / 'jdks').as_posix()}"
        cached_notice = Path(record["cache_path"]) / relative
        self.assertEqual(0, stat.S_IMODE(cached_notice.stat().st_mode) & 0o022)

        new_run = self.root / "legal-notice-run"
        new_run.mkdir()
        materialized = self.cache.try_materialize_toolchains(
            new_run, **self.key_options(),
        )
        copied_notice = new_run / "toolchains/gradle-cache" / relative
        self.assertEqual("fixture legal notice\n", copied_notice.read_text(encoding="utf-8"))
        self.assertEqual(0, stat.S_IMODE(copied_notice.stat().st_mode) & 0o022)

    def test_group_writable_java_launcher_is_not_cached(self):
        self.java.chmod(0o777)
        with self.assertRaisesRegex(ValueError, "group/world writable"):
            self.publish()



if __name__ == "__main__":
    unittest.main()
