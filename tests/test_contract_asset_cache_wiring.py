"""Exercise the real contract-verifier seed, build, and harvest handoff."""
from dataclasses import replace
from pathlib import Path
import subprocess
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import modport.environment_asset_cache
from modport.handlers import (BaselineContractVerificationHandler,
                              _contract_asset_cache_context)
import test_handlers as fixtures


class ContractAssetCacheWiringTests(unittest.TestCase):
    def test_context_uses_authenticated_launcher_and_separate_gradle_homes(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            (root / "artifacts").mkdir()
            (root / "artifacts" / "locked-manifest.json").write_text("{}", encoding="utf-8")
            command = fixtures.HandlerTests._command(root, "contract_verify")
            command = replace(command, options={**command.options, "workflow_version": 26})
            baseline_version = (root / "toolchains" / "baseline-contract-gradle-cache" /
                                "caches" / "forge_gradle" / "minecraft_repo" /
                                "versions" / "1.20.1" / "version.json")
            target_version = (root / "toolchains" / "gradle-cache" /
                              "caches" / "neoformruntime" / "artifacts" /
                              "minecraft_26.1.2_version_manifest.json")
            for path in (baseline_version, target_version):
                path.parent.mkdir(parents=True)
                path.write_text("{}", encoding="utf-8")
            manifest = SimpleNamespace(mdk_repository="https://example.invalid/mdk.git",
                mdk_commit="a" * 40, minecraft_version="26.1.2",
                neoforge_version="26.1.2.101", gradle_version="9.0",
                validate=lambda: None)
            shared = SimpleNamespace(root=root / "environment-cache")
            cache = object()
            with patch("modport.handlers._request", return_value={
                     "dependency_cache": str(root / "dependency-cache"),
                     "source_minecraft": "1.20.1"}), \
                 patch("modport.handlers._host_environment_build_cache", return_value=shared), \
                 patch("modport.handlers.LockedManifest.from_mapping", return_value=manifest), \
                 patch("modport.environment_neoform_cache.EnvironmentNeoFormCache.verified_launcher_manifest",
                       return_value=b"authenticated-launcher") as launcher_reader, \
                 patch("modport.environment_asset_cache.EnvironmentAssetCache", return_value=cache):
                baseline = _contract_asset_cache_context(
                    command, root, "baseline-contract-gradle-cache", baseline=True)
                target = _contract_asset_cache_context(
                    command, root, "target-contract-gradle-cache", baseline=False)

            self.assertEqual(2, launcher_reader.call_count)
            self.assertIs(cache, baseline[0])
            self.assertIs(cache, target[0])
            self.assertEqual(b"authenticated-launcher",
                             baseline[1]["authenticated_launcher_manifest"])
            self.assertEqual(baseline_version, baseline[1]["version_manifest_path"])
            self.assertEqual(root / "toolchains" / "baseline-contract-gradle-cache" /
                             "caches" / "forge_gradle" / "assets",
                             baseline[1]["assets_root"])
            self.assertEqual(target_version, target[1]["version_manifest_path"])
            self.assertEqual(root / "toolchains" / "target-contract-gradle-cache" /
                             "caches" / "neoformruntime" / "assets",
                             target[1]["assets_root"])

    def test_failed_contract_build_still_harvests_verified_partial_assets(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            fixtures.HandlerTests._write_contract_fixture(root)
            command = fixtures.HandlerTests._command(root, "contract_verify")
            command = replace(command, options={
                **command.options, "workflow_version": 26,
                "deadline_epoch": time.time() + 600,
            })
            events = []

            class AssetCache:
                def seed(self, **paths):
                    events.append("seed")
                    return {"index_verified": True, "objects_seeded": 3}

                def harvest(self, **paths):
                    events.append("harvest")
                    return {"index_verified": True, "objects_harvested": 2}

            def sandbox(_root, _workspace, args, **kwargs):
                return ["dry"] if "--dry-run" in args else ["full"]

            def execute(args, *, cwd, log, timeout=None):
                events.append(args[0])
                output = ("BUILD SUCCESSFUL\n" if args[0] == "dry" else
                          "java.net.SocketTimeoutException: Read timed out\n")
                log.parent.mkdir(parents=True, exist_ok=True)
                log.write_text(output, encoding="utf-8")
                return subprocess.CompletedProcess(args, 0 if args[0] == "dry" else 1,
                                                   output)

            with patch("modport.handlers._sandboxed_build_command", side_effect=sandbox), \
                 patch("modport.handlers._forge_baseline_init", return_value="mock.gradle"), \
                 patch("modport.handlers._contract_asset_cache_context",
                       return_value=(AssetCache(), {"assets_root": root / "private"})), \
                 patch("modport.handlers._exec", side_effect=execute):
                result = BaselineContractVerificationHandler()(command)

            self.assertEqual(["dry", "seed", "full", "harvest"], events)
            self.assertEqual("failed", result.status)
            self.assertEqual("baseline_contract_failed", result.error_code)
            self.assertEqual(3, result.outputs["asset_cache"]["seed"]["objects_seeded"])
            self.assertEqual(2, result.outputs["asset_cache"]["harvest"]["objects_harvested"])

    def test_workload_timeout_harvests_partial_assets(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            fixtures.HandlerTests._write_contract_fixture(root)
            command = fixtures.HandlerTests._command(root, "contract_verify")
            command = replace(command, options={
                **command.options, "workflow_version": 26,
                "deadline_epoch": time.time() + 600,
            })
            events = []

            class AssetCache:
                def seed(self, **paths):
                    events.append("seed")
                    return {"index_verified": True}

                def harvest(self, **paths):
                    events.append("harvest")
                    return {"objects_harvested": 2}

            def execute(args, *, cwd, log, timeout=None):
                events.append(args[0])
                if args[0] == "full":
                    log.parent.mkdir(parents=True, exist_ok=True)
                    log.write_text("Read timed out\n", encoding="utf-8")
                    raise subprocess.TimeoutExpired(args, timeout, output=b"Read timed out")
                log.parent.mkdir(parents=True, exist_ok=True)
                log.write_text("BUILD SUCCESSFUL\n", encoding="utf-8")
                return subprocess.CompletedProcess(args, 0, "BUILD SUCCESSFUL\n")

            with patch("modport.handlers._sandboxed_build_command",
                       side_effect=lambda _root, _workspace, args, **kwargs:
                       ["dry"] if "--dry-run" in args else ["full"]), \
                 patch("modport.handlers._forge_baseline_init", return_value="mock.gradle"), \
                 patch("modport.handlers._contract_asset_cache_context",
                       return_value=(AssetCache(), {"assets_root": root / "private"})), \
                 patch("modport.handlers._exec", side_effect=execute):
                result = BaselineContractVerificationHandler()(command)

            self.assertEqual(["dry", "seed", "full", "harvest"], events)
            self.assertEqual("failed", result.status)
            self.assertEqual(2, result.outputs["asset_cache"]["harvest"]["objects_harvested"])

    def test_cold_version_manifest_created_during_failed_build_is_harvested(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            fixtures.HandlerTests._write_contract_fixture(root)
            command = fixtures.HandlerTests._command(root, "contract_verify")
            command = replace(command, options={
                **command.options, "workflow_version": 26,
                "deadline_epoch": time.time() + 600,
            })
            events = []

            class AssetCache:
                def harvest(self, **paths):
                    events.append("harvest")
                    return {"index_verified": True, "objects_harvested": 1}

            context = (AssetCache(), {"assets_root": root / "private"})

            def execute(args, *, cwd, log, timeout=None):
                events.append(args[0])
                log.parent.mkdir(parents=True, exist_ok=True)
                log.write_text("BUILD FAILED\n" if args[0] == "full"
                               else "BUILD SUCCESSFUL\n", encoding="utf-8")
                return subprocess.CompletedProcess(args, 1 if args[0] == "full" else 0,
                                                   log.read_text(encoding="utf-8"))

            with patch("modport.handlers._sandboxed_build_command",
                       side_effect=lambda _root, _workspace, args, **kwargs:
                       ["dry"] if "--dry-run" in args else ["full"]), \
                 patch("modport.handlers._forge_baseline_init", return_value="mock.gradle"), \
                 patch("modport.handlers._contract_asset_cache_context",
                       side_effect=[None, context]) as find_context, \
                 patch("modport.handlers._exec", side_effect=execute):
                result = BaselineContractVerificationHandler()(command)

            self.assertEqual(["dry", "full", "harvest"], events)
            self.assertEqual(2, find_context.call_count)
            self.assertEqual("failed", result.status)
            self.assertEqual(1, result.outputs["asset_cache"]["harvest"]["objects_harvested"])
            self.assertEqual("harvested", result.outputs["asset_cache"]["state"])

    def test_busy_shared_cache_preserves_the_build_result(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            fixtures.HandlerTests._write_contract_fixture(root)
            command = fixtures.HandlerTests._command(root, "contract_verify")
            command = replace(command, options={
                **command.options, "workflow_version": 26,
                "deadline_epoch": time.time() + 600,
            })
            events = []

            class BusyCache:
                def seed(self, **paths):
                    events.append("seed")
                    raise TimeoutError("shared cache lock is busy")

                def harvest(self, **paths):
                    events.append("harvest")
                    raise TimeoutError("shared cache lock is busy")

            def execute(args, *, cwd, log, timeout=None):
                events.append(args[0])
                log.parent.mkdir(parents=True, exist_ok=True)
                result = "BUILD SUCCESSFUL\n" if args[0] == "dry" else "compiler error\n"
                log.write_text(result, encoding="utf-8")
                return subprocess.CompletedProcess(args, 0 if args[0] == "dry" else 1,
                                                   result)

            with patch("modport.handlers._sandboxed_build_command",
                       side_effect=lambda _root, _workspace, args, **kwargs:
                       ["dry"] if "--dry-run" in args else ["full"]), \
                 patch("modport.handlers._forge_baseline_init", return_value="mock.gradle"), \
                 patch("modport.handlers._contract_asset_cache_context",
                       return_value=(BusyCache(), {"assets_root": root / "private"})), \
                 patch("modport.handlers._exec", side_effect=execute):
                result = BaselineContractVerificationHandler()(command)

            self.assertEqual(["dry", "seed", "full", "harvest"], events)
            self.assertEqual("failed", result.status)
            self.assertEqual("baseline_contract_failed", result.error_code)
            self.assertEqual("busy", result.outputs["asset_cache"]["state"])


if __name__ == "__main__":
    unittest.main()
