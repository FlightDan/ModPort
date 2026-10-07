"""Focused checks for the authenticated shared Minecraft asset cache."""
from __future__ import annotations

from hashlib import sha1
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import modport.environment_asset_cache as environment_asset_cache_module
from modport.environment_asset_cache import EnvironmentAssetCache


SOURCE_VERSION = "1.20.1"
TARGET_VERSION = "26.1.2"


def _write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def _version(version: str, index_id: str, index_bytes: bytes) -> bytes:
    return json.dumps({
        "id": version,
        "assetIndex": {
            "id": index_id,
            "sha1": sha1(index_bytes).hexdigest(),
            "size": len(index_bytes),
        },
    }, sort_keys=True, separators=(",", ":")).encode()


def _index(objects: dict[str, bytes]) -> bytes:
    return json.dumps({
        "objects": {
            name: {"hash": sha1(data).hexdigest(), "size": len(data)}
            for name, data in objects.items()
        }
    }, sort_keys=True, separators=(",", ":")).encode()


class EnvironmentAssetCacheTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="modport-assets-test-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.cache = EnvironmentAssetCache(self.root / "environment-cache")
        self.source_objects = {
            "minecraft/sounds/first.ogg": b"source asset one",
            "minecraft/sounds/second.ogg": b"source asset two",
            "minecraft/sounds/missing.ogg": b"source asset missing",
        }
        self.source_index = _index(self.source_objects)
        self.source_version = _version(SOURCE_VERSION, "5", self.source_index)

        self.target_objects = {"minecraft/sounds/target.ogg": b"target-only asset"}
        self.target_index = _index(self.target_objects)
        self.target_version = _version(TARGET_VERSION, "30", self.target_index)
        self.launcher = json.dumps({"versions": [
            {"id": SOURCE_VERSION, "sha1": sha1(self.source_version).hexdigest()},
            {"id": TARGET_VERSION, "sha1": sha1(self.target_version).hexdigest()},
        ]}, sort_keys=True, separators=(",", ":")).encode()

    def _private_tree(self, label: str, *, source_version: bytes | None = None,
                      include_source_index: bool = True) -> tuple[Path, Path, Path]:
        base = self.root / label
        # Match the actual ForgeGradle baseline layout. Callers can use the
        # same API with NeoFormRuntime's separate assets root.
        version_path = base / "caches/forge_gradle/minecraft_repo/versions/1.20.1/version.json"
        assets = base / "caches/forge_gradle/assets"
        index_path = assets / "indexes/5.json"
        _write(version_path, source_version if source_version is not None else self.source_version)
        if include_source_index:
            _write(index_path, self.source_index)
        return assets, version_path, index_path

    def _harvest(self, assets: Path, version_path: Path, index_path: Path) -> dict:
        return self.cache.harvest(
            assets, minecraft_version=SOURCE_VERSION,
            authenticated_launcher_manifest=self.launcher,
            version_manifest_path=version_path, asset_index_path=index_path)

    def _seed(self, assets: Path, version_path: Path,
              index_path: Path | None = None) -> dict:
        kwargs = {}
        if index_path is not None:
            kwargs["asset_index_path"] = index_path
        return self.cache.seed(
            assets, minecraft_version=SOURCE_VERSION,
            authenticated_launcher_manifest=self.launcher,
            version_manifest_path=version_path, **kwargs)

    def test_harvests_verified_partial_baseline_assets_and_seeds_an_independent_run(self) -> None:
        failed_assets, version_path, index_path = self._private_tree("failed-run")
        first_name, second_name, missing_name = self.source_objects
        _write(failed_assets / "objects" / sha1(self.source_objects[first_name]).hexdigest()[:2]
               / sha1(self.source_objects[first_name]).hexdigest(), self.source_objects[first_name])
        corrupt = self.source_objects[second_name][:-1] + b"X"
        self.assertEqual(len(corrupt), len(self.source_objects[second_name]))
        _write(failed_assets / "objects" / sha1(self.source_objects[second_name]).hexdigest()[:2]
               / sha1(self.source_objects[second_name]).hexdigest(), corrupt)
        # missing_name is deliberately absent, modeling a failed download.

        harvested = self._harvest(failed_assets, version_path, index_path)
        self.assertTrue(harvested["index_harvested"])
        self.assertEqual(harvested["objects_harvested"], 1)
        self.assertEqual(harvested["objects_rejected"], 1)
        self.assertEqual(harvested["objects_missing"], 1)

        next_assets, next_version, next_index = self._private_tree(
            "artifact-only-run", include_source_index=False)
        seeded = self._seed(next_assets, next_version)
        self.assertEqual(seeded["asset_index_id"], "5")
        self.assertTrue(seeded["index_seeded"])
        self.assertEqual(seeded["objects_seeded"], 1)
        self.assertEqual(seeded["objects_available"], 1)
        self.assertEqual(seeded["objects_missing"], 2)
        self.assertEqual(next_index.read_bytes(), self.source_index)

        good_hash = sha1(self.source_objects[first_name]).hexdigest()
        shared = self.cache.store / "objects" / good_hash[:2] / good_hash
        private = next_assets / "objects" / good_hash[:2] / good_hash
        self.assertEqual(private.read_bytes(), self.source_objects[first_name])
        self.assertNotEqual(os.stat(private).st_ino, os.stat(shared).st_ino)
        self.assertEqual(os.stat(shared).st_mode & 0o222, 0)
        private.write_bytes(b"run-private mutation")
        self.assertEqual(shared.read_bytes(), self.source_objects[first_name])

        # The target version has a different index ID and different bytes. The
        # explicit source version and index paths above must not select it.
        target_root = self.root / "target-run/caches/neoformruntime/assets"
        target_version_path = self.root / "target-run/caches/neoformruntime/artifacts/version.json"
        target_index_path = target_root / "indexes/30.json"
        _write(target_version_path, self.target_version)
        _write(target_index_path, self.target_index)
        target_result = self.cache.harvest(
            target_root, minecraft_version=TARGET_VERSION,
            authenticated_launcher_manifest=self.launcher,
            version_manifest_path=target_version_path,
            asset_index_path=target_index_path)
        self.assertEqual(target_result["asset_index_id"], "30")

    def test_seed_rehashes_cache_and_repairs_or_removes_same_size_corruption(self) -> None:
        source_assets, version_path, index_path = self._private_tree("bootstrap")
        first_name, second_name, _missing_name = self.source_objects
        for name in (first_name, second_name):
            data = self.source_objects[name]
            digest = sha1(data).hexdigest()
            _write(source_assets / "objects" / digest[:2] / digest, data)
        self._harvest(source_assets, version_path, index_path)

        first_name, second_name, _missing_name = self.source_objects
        first_hash = sha1(self.source_objects[first_name]).hexdigest()
        shared_first = self.cache.store / "objects" / first_hash[:2] / first_hash
        shared_first.chmod(0o644)
        shared_first.write_bytes(b"!" * len(self.source_objects[first_name]))

        run_assets, run_version, run_index = self._private_tree("next-run")
        for name in (first_name, second_name):
            data = self.source_objects[name]
            digest = sha1(data).hexdigest()
            corrupt = b"?" * len(data)
            _write(run_assets / "objects" / digest[:2] / digest, corrupt)

        seeded = self._seed(run_assets, run_version, run_index)
        first_private = run_assets / "objects" / first_hash[:2] / first_hash
        self.assertFalse(first_private.exists())
        second_hash = sha1(self.source_objects[second_name]).hexdigest()
        second_private = run_assets / "objects" / second_hash[:2] / second_hash
        self.assertEqual(second_private.read_bytes(), self.source_objects[second_name])
        self.assertEqual(seeded["objects_rejected"], 1)
        self.assertEqual(seeded["objects_seeded"], 1)
        self.assertEqual(seeded["objects_missing"], 2)

    def test_rejects_version_manifest_not_pinned_by_authenticated_launcher(self) -> None:
        assets, version_path, index_path = self._private_tree("tampered-version")
        _write(version_path, self.source_version + b" ")
        with self.assertRaisesRegex(ValueError, "launcher SHA-1"):
            self._harvest(assets, version_path, index_path)
        self.assertFalse(self.cache.store.exists())

    def test_rejects_index_path_for_another_version_and_same_size_index_tampering(self) -> None:
        assets, version_path, index_path = self._private_tree("wrong-index-path")
        wrong_path = assets / "indexes/30.json"
        with self.assertRaisesRegex(ValueError, "locked index ID"):
            self._harvest(assets, version_path, wrong_path)

        corrupt = self.source_index[:-1] + (b" " if self.source_index[-1:] != b" " else b"\n")
        self.assertEqual(len(corrupt), len(self.source_index))
        _write(index_path, corrupt)
        harvested = self._harvest(assets, version_path, index_path)
        self.assertFalse(harvested["index_verified"])
        self.assertTrue(harvested["index_rejected"])
        self.assertFalse(self.cache.store.exists())

    def test_rejects_symlinks_and_special_object_files(self) -> None:
        for kind in ("symlink", "fifo"):
            with self.subTest(kind=kind):
                assets, version_path, index_path = self._private_tree(kind)
                object_hash = sha1(self.source_objects["minecraft/sounds/first.ogg"]).hexdigest()
                object_path = assets / "objects" / object_hash[:2] / object_hash
                object_path.parent.mkdir(parents=True, exist_ok=True)
                if kind == "symlink":
                    outside = self.root / "outside-object"
                    _write(outside, self.source_objects["minecraft/sounds/first.ogg"])
                    object_path.symlink_to(outside)
                else:
                    if not hasattr(os, "mkfifo"):
                        self.skipTest("FIFO creation is unavailable")
                    os.mkfifo(object_path)
                with self.assertRaises(ValueError):
                    self._harvest(assets, version_path, index_path)
                self.assertFalse(self.cache.store.exists())

    def test_open_parent_fd_blocks_ancestor_symlink_swap_during_seed(self) -> None:
        bootstrap_assets, bootstrap_version, bootstrap_index = self._private_tree("bootstrap-swap")
        object_name, object_bytes = next(iter(self.source_objects.items()))
        object_hash = sha1(object_bytes).hexdigest()
        _write(bootstrap_assets / "objects" / object_hash[:2] / object_hash, object_bytes)
        self._harvest(bootstrap_assets, bootstrap_version, bootstrap_index)

        run_assets, run_version, _run_index = self._private_tree(
            "run-swap", include_source_index=False)
        outside = self.root / "outside-swap-target"
        outside.mkdir()
        prefix = run_assets / "objects" / object_hash[:2]
        moved_prefix = prefix.with_name(prefix.name + "-held")
        real_replace = os.replace
        swapped = False

        def swap_ancestor_then_replace(source, destination, **kwargs):
            nonlocal swapped
            if not swapped and destination == object_hash:
                prefix.rename(moved_prefix)
                prefix.symlink_to(outside, target_is_directory=True)
                swapped = True
            return real_replace(source, destination, **kwargs)

        with patch.object(environment_asset_cache_module.os, "replace",
                          side_effect=swap_ancestor_then_replace):
            with self.assertRaises(ValueError):
                self._seed(run_assets, run_version)

        self.assertTrue(swapped)
        self.assertFalse((outside / object_hash).exists())
        self.assertEqual((moved_prefix / object_hash).read_bytes(), object_bytes)


if __name__ == "__main__":
    unittest.main()
