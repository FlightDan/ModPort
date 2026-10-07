"""Behavioral checks for verified NeoForm input reuse across independent Runs."""
from __future__ import annotations

from hashlib import sha1
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from modport.environment_neoform_cache import EnvironmentNeoFormCache


MINECRAFT = "26.1.2"
SCOPE = {
    "mdk_repository": "https://github.com/NeoForgeMDKs/MDK-26.1.2-ModDevGradle.git",
    "mdk_commit": "27a6e7184401d39b1ebd32a13fec7ceba2ca5c67",
    "minecraft_version": MINECRAFT,
    "neoforge_version": "26.1.2.106",
    "gradle_version": "9.2.1",
}


def _write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def _fixture(runtime: Path) -> None:
    artifacts = runtime / "artifacts"
    client = b"fake client input"
    server = b"fake server input"
    version = json.dumps({"downloads": {
        "client": {"sha1": sha1(client).hexdigest()},
        "server": {"sha1": sha1(server).hexdigest()},
    }}).encode()
    launcher = json.dumps({"versions": [
        {"id": MINECRAFT, "sha1": sha1(version).hexdigest()}
    ]}).encode()
    _write(artifacts / "minecraft_launcher_manifest.json", launcher)
    _write(artifacts / f"minecraft_{MINECRAFT}_version_manifest.json", version)
    _write(artifacts / f"minecraft_{MINECRAFT}_client.jar", client)
    _write(artifacts / f"minecraft_{MINECRAFT}_server.jar", server)
    _write(runtime / "intermediate_results/recompile_output.jar", b"derived official MDK result")


class EnvironmentNeoFormCacheTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="modport-neoform-test-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.runtime = self.root / "cold/caches/neoformruntime"
        _fixture(self.runtime)
        self.cache = EnvironmentNeoFormCache(self.root / "shared")
        official_bytes = (self.runtime / "artifacts/minecraft_launcher_manifest.json").read_bytes()
        fetch_patch = patch(
            "modport.environment_neoform_cache._fetch_official_launcher_manifest",
            return_value=official_bytes)
        self.official_fetch = fetch_patch.start()
        self.addCleanup(fetch_patch.stop)

    def _publish(self) -> dict:
        return self.cache.publish_bootstrap(
            self.runtime, mdk_bootstrap_succeeded=True, **SCOPE)

    def test_materializes_verified_inputs_into_independent_writable_run(self) -> None:
        published = self._publish()
        reused = self.cache.try_materialize(self.root / "new-run/gradle-cache", **SCOPE)
        self.assertEqual(published["snapshot_key"], reused["snapshot_key"])
        copied = Path(reused["materialized_path"]) / "artifacts/minecraft_launcher_manifest.json"
        snapshot = self.cache.root / "neoform-runtime-v2" / published["scope_sha256"] / published["snapshot_key"]
        original = (snapshot / "payload/artifacts/minecraft_launcher_manifest.json").read_bytes()
        copied.write_bytes(b"run-private mutation")
        self.assertEqual(original, (snapshot / "payload/artifacts/minecraft_launcher_manifest.json").read_bytes())
        self.assertNotEqual(os.stat(copied).st_ino,
                            os.stat(snapshot / "payload/artifacts/minecraft_launcher_manifest.json").st_ino)
        self.assertIsNone(self.cache.try_materialize(
            self.root / "other-run/gradle-cache", **{**SCOPE, "neoforge_version": "26.1.2.107"}))
        self.official_fetch.assert_called_once()

    def test_reads_only_authenticated_official_launcher_manifest(self) -> None:
        self.assertIsNone(self.cache.verified_launcher_manifest(**SCOPE))
        published = self._publish()
        expected = (self.runtime / "artifacts/minecraft_launcher_manifest.json").read_bytes()
        self.assertEqual(expected, self.cache.verified_launcher_manifest(**SCOPE))
        path = (self.cache.root / "neoform-runtime-v2" / published["scope_sha256"]
                / published["snapshot_key"] / "payload/artifacts/minecraft_launcher_manifest.json")
        path.chmod(0o644)
        path.write_bytes(b"tampered launcher")
        with self.assertRaises(ValueError):
            self.cache.verified_launcher_manifest(**SCOPE)

    def test_rejects_corrupt_shared_snapshot_before_copying(self) -> None:
        published = self._publish()
        path = (self.cache.root / "neoform-runtime-v2" / published["scope_sha256"]
                / published["snapshot_key"] / "payload/intermediate_results/recompile_output.jar")
        path.chmod(0o644)
        path.write_bytes(b"tampered")
        with self.assertRaises(ValueError):
            self.cache.try_materialize(self.root / "new-run/gradle-cache", **SCOPE)
        self.assertFalse((self.root / "new-run/gradle-cache/caches/neoformruntime").exists())

    def test_rejects_failed_bootstrap_and_broken_mojang_chain(self) -> None:
        with self.assertRaises(ValueError):
            self.cache.publish_bootstrap(self.runtime, mdk_bootstrap_succeeded=False, **SCOPE)
        jar = self.runtime / f"artifacts/minecraft_{MINECRAFT}_client.jar"
        jar.write_bytes(b"wrong client")
        with self.assertRaisesRegex(ValueError, "client failed"):
            self._publish()

    def test_rejects_symlinked_sandbox_artifact(self) -> None:
        jar = self.runtime / f"artifacts/minecraft_{MINECRAFT}_server.jar"
        jar.unlink()
        jar.symlink_to(self.root / "somewhere-else")
        with self.assertRaises(ValueError):
            self._publish()

    def test_rejects_malformed_launcher_metadata_without_host_crash(self) -> None:
        launcher = self.runtime / "artifacts/minecraft_launcher_manifest.json"
        launcher.write_text(json.dumps({"versions": [{"id": MINECRAFT, "sha1": None}]}))
        with self.assertRaises(ValueError):
            self._publish()

    def test_reuses_one_verified_seed_when_repeated_cold_builds_differ(self) -> None:
        first = self._publish()
        (self.runtime / "intermediate_results/recompile_output.jar").write_bytes(
            b"another successful build result")
        second = self._publish()
        self.assertNotEqual(first["snapshot_key"], second["snapshot_key"])
        reused = self.cache.try_materialize(self.root / "new-run/gradle-cache", **SCOPE)
        self.assertIn(reused["snapshot_key"], {first["snapshot_key"], second["snapshot_key"]})

    def test_rejects_unmanifested_payload_root(self) -> None:
        published = self._publish()
        payload = (self.cache.root / "neoform-runtime-v2" / published["scope_sha256"]
                   / published["snapshot_key"] / "payload")
        payload.chmod(0o755)
        (payload / "unexpected").mkdir()
        with self.assertRaisesRegex(ValueError, "unmanifested roots"):
            self.cache.try_materialize(self.root / "new-run/gradle-cache", **SCOPE)

    def test_rejects_self_consistent_sandbox_manifest_that_differs_from_official(self) -> None:
        artifacts = self.runtime / "artifacts"
        altered_client = b"altered client from sandbox"
        _write(artifacts / f"minecraft_{MINECRAFT}_client.jar", altered_client)
        version = json.loads((artifacts / f"minecraft_{MINECRAFT}_version_manifest.json").read_text())
        version["downloads"]["client"]["sha1"] = sha1(altered_client).hexdigest()
        version_bytes = json.dumps(version).encode()
        _write(artifacts / f"minecraft_{MINECRAFT}_version_manifest.json", version_bytes)
        _write(artifacts / "minecraft_launcher_manifest.json",
               json.dumps({"versions": [{"id": MINECRAFT,
                                           "sha1": sha1(version_bytes).hexdigest()}]}).encode())
        with self.assertRaisesRegex(ValueError, "launcher SHA-1"):
            self._publish()

    def test_rejects_symlinked_run_cache_ancestor_on_replay(self) -> None:
        self._publish()
        gradle_home = self.root / "new-run/gradle-cache"
        gradle_home.mkdir(parents=True)
        outside = self.root / "outside"
        outside.mkdir()
        (gradle_home / "caches").symlink_to(outside)
        with self.assertRaises(ValueError):
            self.cache.try_materialize(gradle_home, **SCOPE)
        self.assertFalse((outside / "neoformruntime").exists())


if __name__ == "__main__":
    unittest.main()
