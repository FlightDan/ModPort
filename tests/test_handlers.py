import hashlib
import json
import os
import shutil
import shlex
import sys
from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
import subprocess
import tempfile
from threading import Barrier, Event, Lock, get_ident
import time
import unittest
import urllib.error
from unittest.mock import Mock, patch

from modport.contracts import OperationInput, OperationResult

from modport.characterization import CharacterizationContract, FrozenContractError, ReviewRecord, freeze_contract
from modport.handlers import (
    BaselineContractVerificationHandler,
    BuildAndBehaviorHandler,
    CodexStageHandler,
    ClientSmokeHandler,
    FreezeContractHandler,
    GradleHandler,
    LockEnvironmentHandler,
    MAVEN_METADATA,
    MAVEN_METADATA_MIRROR,
    NEOFORGE_UNPINNED_METADATA_TTL_SECONDS,
    ReviewHandler,
    ApprovedCandidateHandler,
    AcceptancePreflightHandler,
    ValidateInputHandler,
    _sandboxed_build_command,
    _exec,
    _hash_sandbox_java,
    _verify_locked_artifacts,
    _fresh_baseline_report,
    _read_artifact_ref,
    _neoforge_metadata_cache_location,
    _publish_environment_metadata_cache,
    _read_environment_metadata_cache,
    _resolve_neoforge_metadata,
    _result,
    build_registry,
    resolve_neoforge_versions,
)
from modport.artifact_handoff import install_handoff, prepare_handoff
from modport.evidence import atomic_json, file_digest
from modport.manifest import canonical_json, manifest_sha256, select_neoforge_candidate
from modport.models import Budget, LockedManifest, MigrationRequest
from modport.rubric import acceptance_rubric


class HandlerTests(unittest.TestCase):
    def _require_sandbox_namespaces(self):
        probe = subprocess.run(["bwrap", "--unshare-all", "--ro-bind", "/", "/", "/bin/true"], capture_output=True, text=True)
        if probe.returncode and "Operation not permitted" in probe.stderr:
            self.skipTest("host prohibits bubblewrap namespace creation: " + probe.stderr.strip())
        self.assertEqual(probe.returncode, 0, probe.stderr)

    @staticmethod
    def _command(root: Path, stage_id: str, *, payload=None) -> OperationInput:
        rubric = acceptance_rubric()
        rubric_path = root / "artifacts" / "acceptance-rubric.json"
        rubric_path.parent.mkdir(parents=True, exist_ok=True)
        rubric_path.write_text(canonical_json(rubric) + "\n", encoding="utf-8")
        rule_refs = {}
        for key in ("agent_rules", "evidence_protocol"):
            path = root / "artifacts" / (key + ".md")
            path.write_text("Shared rules for " + key)
            rule_refs[key] = {"path": path.relative_to(root).as_posix(), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
        return OperationInput(
            run_id="run-1",
            task_id="task-1",
            stage_id=stage_id,
            command_id="command-1",
            run_dir=str(root),
            payload=payload or {},
            options={"acceptance_rubric_sha256": rubric["rubric_sha256"]},
            artifact_refs={
                **rule_refs,
                "acceptance_rubric": {
                    "path": "artifacts/acceptance-rubric.json",
                    "sha256": hashlib.sha256(rubric_path.read_bytes()).hexdigest(),
                    "media_type": "application/json",
                    "metadata": {"rubric_sha256": rubric["rubric_sha256"]},
                }
            },
        )

    @staticmethod
    def _stage_result(command: OperationInput, status: str, *, outputs=None) -> OperationResult:
        return OperationResult(
            status=status,
            run_id=command.run_id,
            task_id=command.task_id,
            stage_id=command.stage_id,
            command_id=command.command_id,
            outputs=outputs or {},
        )

    def test_neoforge_metadata_prefers_stable_and_falls_back_to_beta(self):
        xml = b"""
        <metadata><versioning><versions>
          <version>21.1.80-beta</version>
          <version>21.1.78</version>
          <version>21.1.77</version>
          <version>21.1.81-alpha</version>
          <version>20.4.200</version>
        </versions></versioning></metadata>
        """
        candidates = resolve_neoforge_versions(xml, "21.1")
        self.assertEqual(
            [(item.version, item.channel) for item in candidates],
            [("21.1.80-beta", "beta"), ("21.1.78", "stable"), ("21.1.77", "stable"), ("21.1.81-alpha", "other")],
        )
        self.assertEqual(select_neoforge_candidate(candidates).version, "21.1.78")

        beta_only = resolve_neoforge_versions(
            b"<metadata><versioning><versions>"
            b"<version>21.1.9-beta</version><version>21.1.10-beta</version>"
            b"</versions></versioning></metadata>",
            "21.1",
        )
        self.assertEqual(select_neoforge_candidate(beta_only).version, "21.1.10-beta")
        self.assertEqual(
            [item.version for item in resolve_neoforge_versions(xml, "1.21.1")],
            ["21.1.80-beta", "21.1.78", "21.1.77", "21.1.81-alpha"],
        )

    def _metadata_handoff_fixture(self, root: Path, *, alter_xml_after_lock: bool = False,
                                  metadata: bytes | None = None):
        old_run = root.parent / f"{root.name}-old-run"
        worktree = old_run / "worktree"
        worktree.mkdir(parents=True)

        def git(*args):
            result = subprocess.run(
                ["git", "-C", str(worktree), *args], text=True,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
            )
            if result.returncode:
                raise AssertionError(result.stderr)
            return result.stdout.strip()

        git("init")
        git("config", "user.name", "ModPort Test")
        git("config", "user.email", "modport@example.invalid")
        (worktree / "build.gradle").write_text("plugins {}\n", encoding="utf-8")
        git("add", "build.gradle")
        git("commit", "-m", "baseline")
        source_commit = git("rev-parse", "HEAD")
        (worktree / "migration.txt").write_text("candidate\n", encoding="utf-8")
        git("add", "migration.txt")
        git("commit", "-m", "candidate")
        target_commit = git("rev-parse", "HEAD")
        mdk_commit = "a" * 40
        request = {
            "mod_id": "scalinghealth",
            "source_repository": "https://example.invalid/scalinghealth.git",
            "source_minecraft": "1.20.1",
            "target_minecraft": "26.1.2",
            "source_loader": "forge",
            "target_loader": "neoforge",
            "source_loader_version": "47.0.1",
            "target_loader_version": "26.1.2.106",
            "source_java": "17",
            "target_java": "25",
            "source_revision": source_commit,
            "mdk_revision": mdk_commit,
            "dependency_cache": str(root.parent / "dependency-cache"),
            "budget": Budget().to_dict(),
        }
        atomic_json(old_run / "run.json", {
            "run_id": "old-run-id", "request": request,
        })
        atomic_json(old_run / "artifacts/source.json", {
            "source_repository": request["source_repository"],
            "source_commit": source_commit,
            "requested_revision": source_commit,
        })
        if metadata is None:
            metadata = (
                b"<metadata><versioning><versions>"
                b"<version>26.1.2.105</version><version>26.1.2.106</version>"
                b"</versions></versioning></metadata>"
            )
        (old_run / "toolchains").mkdir(parents=True)
        metadata_path = old_run / "toolchains/neoforge-maven-metadata.xml"
        metadata_path.write_bytes(metadata)
        old_lock = LockedManifest(
            request=MigrationRequest.from_mapping(request),
            neoforge_version="26.1.2.106", source_commit=source_commit,
            neoforge_channel="stable", minecraft_version="26.1.2",
            java_version="25",
            java_toolchain={"executable": "jdks/old/bin/java", "java_sha256": "b" * 64,
                            "version_output": 'openjdk version "25.0.1"'},
            gradle_version="9.2.1",
            mdk_repository="https://github.com/NeoForgeMDKs/MDK-26.1.2-ModDevGradle.git",
            mdk_commit=mdk_commit, sdk_version="0.7.0.dev0", workflow_version=23,
            checksums={"neoforge_maven_metadata_sha256": hashlib.sha256(metadata).hexdigest()},
        )
        old_lock_payload = old_lock.to_dict()
        old_lock_payload["manifest_sha256"] = hashlib.sha256(
            canonical_json(old_lock_payload).encode("utf-8")
        ).hexdigest()
        atomic_json(old_run / "artifacts/locked-manifest.json", old_lock_payload)
        if alter_xml_after_lock:
            metadata_path.write_bytes(metadata.replace(b"26.1.2.106", b"26.1.2.107"))

        package = root.parent / f"{root.name}-handoff"
        prepare_handoff(old_run, package, [
            "artifacts/source.json", "artifacts/locked-manifest.json",
            "toolchains/neoforge-maven-metadata.xml",
        ])
        root.mkdir(parents=True, exist_ok=True)
        installed = install_handoff(package, root)
        current_source = {
            "source_repository": request["source_repository"],
            "source_commit": source_commit,
            "requested_revision": source_commit,
            "artifact_handoff": {
                "source_run_id": installed["metadata"]["source_run_id"],
                "target_commit": target_commit,
                "acceptance_status": "unverified",
                "scheduler_history_imported": False,
            },
        }
        current_source_path = root / "artifacts/source.json"
        atomic_json(current_source_path, current_source)
        command = self._command(root, "environment_lock", payload={"request": request})
        command = replace(
            command,
            options={**dict(command.options), "workflow_version": 25},
            artifact_refs={
                **dict(command.artifact_refs), **installed["refs"],
                "source_evidence": {
                    "path": "artifacts/source.json",
                    "sha256": file_digest(current_source_path),
                    "media_type": "application/json",
                },
            },
        )
        mdk = root / "toolchains/mdk"
        (mdk / "gradle/wrapper").mkdir(parents=True)
        (mdk / "build.gradle").write_text(
            "java { toolchain { languageVersion = JavaLanguageVersion.of(25) } }\n",
            encoding="utf-8",
        )
        (mdk / "gradle/wrapper/gradle-wrapper.properties").write_text(
            "distributionUrl=https\\://services.gradle.org/distributions/gradle-9.2.1-bin.zip\n",
            encoding="utf-8",
        )
        java = root / "toolchains/gradle-cache/jdks/current/bin/java"
        java.parent.mkdir(parents=True)
        java.write_bytes(b"current-java-toolchain")
        return command, request, source_commit, mdk_commit, java, metadata

    @staticmethod
    def _successful_process(args, *, stdout=""):
        return subprocess.CompletedProcess(args, 0, stdout, "")

    def _environment_lock_mocks(self, root: Path, *, mdk_commit: str, java: Path):
        mdk_repository = "https://github.com/NeoForgeMDKs/MDK-26.1.2-ModDevGradle.git"
        cache = Mock()
        cache.root = root.parent / "environment-cache"
        cache.try_materialize_mdk.return_value = None
        cache.publish_mdk_bundle.return_value = {
            "cache_key": "a" * 64, "bundle_sha256": "b" * 64,
        }
        cache.compatible_gradle_snapshots.return_value = []
        cache.publish_gradle_modules.return_value = {
            "snapshot_key": "c" * 64, "content_sha256": "c" * 64,
        }
        toolchains = Mock()
        toolchains.try_materialize_toolchains.return_value = None
        toolchains.publish_bootstrap_toolchains.return_value = {
            "cache_key": "d" * 64,
            "identity": {"java_sha256": hashlib.sha256(java.read_bytes()).hexdigest()},
        }

        def execute(args, **kwargs):
            if kwargs["log"].name.startswith("mdk-java-version-"):
                output = 'openjdk version "25.0.1"'
            else:
                output = mdk_commit if "rev-parse" in args else ""
            return self._successful_process(args, stdout=output)

        def probe(*, args, **kwargs):
            if args[0] == "git" and "config" in args:
                return self._successful_process(args, stdout=mdk_repository)
            if args[0] == "git" and "status" in args:
                return self._successful_process(args)
            raise AssertionError(f"unexpected process probe: {args}")

        @contextmanager
        def build_scope():
            with patch.multiple("modport.handlers",
                                _sandboxed_build_command=Mock(side_effect=lambda _root, _mdk, args, **_kwargs: ["bwrap", *args]),
                                _host_environment_build_cache=Mock(return_value=cache)), \
                    patch("modport.environment_toolchain_cache.EnvironmentToolchainCache",
                          return_value=toolchains):
                yield

        return patch("modport.handlers._exec", side_effect=execute), \
            patch("modport.handlers.probe_process", side_effect=probe), build_scope()

    def test_environment_java_version_check_stays_in_sandbox(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw) / "new-run"
            command, _, _, mdk_commit, java, _ = self._metadata_handoff_fixture(root)
            execute, probe, build = self._environment_lock_mocks(root, mdk_commit=mdk_commit, java=java)
            with execute as execution, probe as host_probe, build:
                result = LockEnvironmentHandler()(command)
            self.assertEqual(result.status, "completed")
            java_commands = [call.args[0] for call in execution.call_args_list
                             if call.kwargs["log"].name.startswith("mdk-java-version-")]
            self.assertEqual(java_commands, [["bwrap", "/gradle-cache/jdks/current/bin/java", "-version"]])
            self.assertFalse(any(str(java) in str(call) for call in host_probe.call_args_list))

    def test_environment_partial_neoform_cache_keeps_local_manifest_without_forcing_offline(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw) / "new-run"
            command, _, _, mdk_commit, java, _ = self._metadata_handoff_fixture(root)
            execute, probe, build = self._environment_lock_mocks(root, mdk_commit=mdk_commit, java=java)
            neoform = Mock()
            neoform.try_materialize.return_value = {
                "snapshot_key": "e" * 64, "content_sha256": "e" * 64,
            }
            with execute as execution, probe, build, \
                    patch("modport.environment_neoform_cache.EnvironmentNeoFormCache",
                          return_value=neoform):
                result = LockEnvironmentHandler()(command)
            self.assertEqual(result.status, "completed")
            bootstrap = [call.args[0] for call in execution.call_args_list
                         if call.kwargs["log"].name == "mdk-java-toolchain.log"]
            self.assertEqual(len(bootstrap), 1)
            self.assertIn(
                "-PneoForge.neoFormRuntime.launcherManifestUrl=file:///gradle-cache/caches/neoformruntime/artifacts/minecraft_launcher_manifest.json",
                bootstrap[0],
            )
            self.assertNotIn("--offline", bootstrap[0])

    def test_sandbox_java_digest_rejects_links_fifo_and_oversize(self):
        with tempfile.TemporaryDirectory() as raw:
            home = Path(raw)
            java = home / "jdks/current/bin/java"
            java.parent.mkdir(parents=True)
            java.write_bytes(b"launcher")
            relative = java.relative_to(home)
            self.assertEqual(hashlib.sha256(b"launcher").hexdigest(),
                             _hash_sandbox_java(home, relative))
            java.unlink()
            java.symlink_to("/dev/zero")
            with self.assertRaises(OSError):
                _hash_sandbox_java(home, relative)
            java.unlink()
            os.mkfifo(java)
            with self.assertRaisesRegex(ValueError, "regular file"):
                _hash_sandbox_java(home, relative)
            java.unlink()
            with java.open("wb") as stream:
                stream.truncate(64 * 1024 * 1024 + 1)
            with self.assertRaisesRegex(ValueError, "bounded"):
                _hash_sandbox_java(home, relative)

    def test_environment_lock_uses_authenticated_handoff_without_network(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw) / "new-run"
            command, request, source_commit, mdk_commit, java, metadata = self._metadata_handoff_fixture(root)
            execute, probe, build = self._environment_lock_mocks(root, mdk_commit=mdk_commit, java=java)
            with patch("modport.handlers.urlopen", side_effect=AssertionError("handoff must precede network I/O")), \
                    execute, probe, build:
                result = LockEnvironmentHandler()(command)

            self.assertEqual("completed", result.status, result.detail)
            self.assertEqual("authenticated_artifact_only_handoff",
                             result.outputs["metadata_resolution"]["kind"])
            self.assertEqual("stored", result.outputs["metadata_resolution"]["cache_state"])
            self.assertEqual("old-run-id", result.outputs["metadata_resolution"]["source_run_id"])
            self.assertEqual(hashlib.sha256(metadata).hexdigest(),
                             result.outputs["metadata_resolution"]["metadata_sha256"])
            cache_files = list((root.parent / "environment-cache").glob(
                "neoforge-metadata-v1/*.json"
            ))
            self.assertEqual(1, len(cache_files))
            cache_record = json.loads(cache_files[0].read_text(encoding="utf-8"))
            self.assertEqual(hashlib.sha256(metadata).hexdigest(), cache_record["metadata_sha256"])
            self.assertEqual("old-run-id", cache_record["source"]["source_run_id"])

            execute, probe, build = self._environment_lock_mocks(root, mdk_commit=mdk_commit, java=java)
            with patch("modport.handlers.urlopen", side_effect=AssertionError("cache hit must be network-free")), \
                    execute, probe, build:
                cached = LockEnvironmentHandler()(command)
            self.assertEqual("completed", cached.status, cached.detail)
            self.assertEqual("authenticated_artifact_only_handoff",
                             cached.outputs["metadata_resolution"]["kind"])
            self.assertEqual("hit", cached.outputs["metadata_resolution"]["cache_state"])
            self.assertEqual("old-run-id", cached.outputs["metadata_resolution"]["source_run_id"])

            lock = json.loads((root / "artifacts/locked-manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(request["target_loader_version"], lock["neoforge_version"])
            self.assertEqual(source_commit, lock["source_commit"])
            self.assertEqual("25", lock["java_version"])
            self.assertEqual(mdk_commit, lock["mdk_commit"])
            self.assertEqual(
                "https://github.com/NeoForgeMDKs/MDK-26.1.2-ModDevGradle.git",
                lock["mdk_repository"],
            )
            self.assertEqual("0.7.0.dev0", lock["sdk_version"])
            self.assertEqual(25, lock["workflow_version"])
            self.assertEqual(java.relative_to(root / "toolchains/gradle-cache").as_posix(),
                             lock["java_toolchain"]["executable"])
            self.assertNotEqual("jdks/old/bin/java", lock["java_toolchain"]["executable"])
            self.assertEqual(hashlib.sha256(metadata).hexdigest(),
                             lock["checksums"]["neoforge_maven_metadata_sha256"])

    def test_environment_lock_rejects_handoff_xml_not_bound_by_old_lock(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw) / "new-run"
            command, _, _, mdk_commit, java, _ = self._metadata_handoff_fixture(
                root, alter_xml_after_lock=True
            )
            execute, probe, build = self._environment_lock_mocks(root, mdk_commit=mdk_commit, java=java)
            with patch("modport.handlers.urlopen", side_effect=AssertionError("rejected handoff must not fall through to network")) as live_get, \
                    execute as run_process, probe, build:
                result = LockEnvironmentHandler()(command)

            self.assertEqual("failed", result.status)
            self.assertEqual("version_resolution_failed", result.error_code)
            self.assertIn("does not authenticate the NeoForge metadata XML", result.detail)
            live_get.assert_not_called()
            run_process.assert_not_called()

    def test_environment_lock_revalidates_current_handoff_on_cache_hit(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw) / "new-run"
            command, _, _, mdk_commit, java, _ = self._metadata_handoff_fixture(root)
            execute, probe, build = self._environment_lock_mocks(root, mdk_commit=mdk_commit, java=java)
            with patch("modport.handlers.urlopen", side_effect=AssertionError("authenticated handoff is offline")), \
                    execute, probe, build:
                seeded = LockEnvironmentHandler()(command)
            self.assertEqual("completed", seeded.status, seeded.detail)

            metadata_ref = command.artifact_refs["handoff:toolchains/neoforge-maven-metadata.xml"]
            installed_metadata = root / metadata_ref["path"]
            installed_metadata.write_bytes(installed_metadata.read_bytes() + b"\n")
            execute, probe, build = self._environment_lock_mocks(root, mdk_commit=mdk_commit, java=java)
            with patch("modport.handlers.urlopen", side_effect=AssertionError("tampered handoff must fail before network")) as live_get, \
                    execute as run_process, probe, build:
                rejected = LockEnvironmentHandler()(command)
            self.assertEqual("failed", rejected.status)
            self.assertIn("authenticated metadata handoff was rejected", rejected.detail)
            self.assertIn("artifact size mismatch", rejected.detail)
            live_get.assert_not_called()
            run_process.assert_not_called()

    def test_environment_lock_fails_closed_on_handoff_request_identity_mismatch(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw) / "new-run"
            command, _, _, mdk_commit, java, _ = self._metadata_handoff_fixture(root)
            payload = dict(command.payload)
            request = dict(payload["request"])
            request["target_java"] = "24"
            payload["request"] = request
            command = replace(command, payload=payload)
            execute, probe, build = self._environment_lock_mocks(root, mdk_commit=mdk_commit, java=java)
            with patch("modport.handlers.urlopen", side_effect=AssertionError("identity mismatch must stop before network")) as live_get, \
                    execute as run_process, probe, build:
                result = LockEnvironmentHandler()(command)

            self.assertEqual("failed", result.status)
            self.assertEqual("version_resolution_failed", result.error_code)
            self.assertIn("target_java", result.detail)
            live_get.assert_not_called()
            run_process.assert_not_called()

    def test_environment_lock_keeps_live_metadata_as_primary_source(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw) / "new-run"
            command, _, _, mdk_commit, java, metadata = self._metadata_handoff_fixture(root)
            command = replace(command, artifact_refs={
                key: value for key, value in command.artifact_refs.items()
                if key != "artifact_handoff" and not key.startswith("handoff:")
            })
            execute, probe, build = self._environment_lock_mocks(root, mdk_commit=mdk_commit, java=java)
            response = Mock()
            response.__enter__ = Mock(return_value=response)
            response.__exit__ = Mock(return_value=False)
            response.geturl.return_value = MAVEN_METADATA
            response.read.return_value = metadata
            with patch("modport.handlers.urlopen", return_value=response) as live_get, \
                    patch("modport.handlers._neoforge_metadata_from_handoff") as fallback, \
                    execute, probe, build:
                result = LockEnvironmentHandler()(command)

            self.assertEqual("completed", result.status, result.detail)
            self.assertEqual("persistent_environment_cache", result.outputs["metadata_resolution"]["kind"])
            self.assertEqual("official_maven_metadata",
                             result.outputs["metadata_resolution"]["source"]["kind"])
            self.assertEqual("stored", result.outputs["metadata_resolution"]["cache_state"])
            self.assertEqual(MAVEN_METADATA, live_get.call_args.args[0])
            fallback.assert_not_called()

    def test_environment_lock_mirror_cold_miss_seeds_cache(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw) / "new-run"
            command, _, _, mdk_commit, java, metadata = self._metadata_handoff_fixture(root)
            command = replace(command, artifact_refs={
                key: value for key, value in command.artifact_refs.items()
                if key != "artifact_handoff" and not key.startswith("handoff:")
            })
            calls = []

            def fetch(url, **_kwargs):
                calls.append(url)
                if url == MAVEN_METADATA:
                    raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)
                self.assertEqual(MAVEN_METADATA_MIRROR, url)
                response = Mock()
                response.__enter__ = Mock(return_value=response)
                response.__exit__ = Mock(return_value=False)
                response.geturl.return_value = url
                response.read.return_value = metadata
                return response

            execute, probe, build = self._environment_lock_mocks(root, mdk_commit=mdk_commit, java=java)
            with patch("modport.handlers.urlopen", side_effect=fetch), execute, probe, build:
                result = LockEnvironmentHandler()(command)
            self.assertEqual("completed", result.status, result.detail)
            self.assertEqual([MAVEN_METADATA, MAVEN_METADATA_MIRROR], calls)
            self.assertEqual("stored", result.outputs["metadata_resolution"]["cache_state"])
            self.assertEqual("qlu_maven_mirror", result.outputs["metadata_resolution"]["source"]["kind"])

            execute, probe, build = self._environment_lock_mocks(root, mdk_commit=mdk_commit, java=java)
            with patch("modport.handlers.urlopen", side_effect=AssertionError("cache hit must be network-free")), \
                    execute, probe, build:
                cached = LockEnvironmentHandler()(command)
            self.assertEqual("completed", cached.status, cached.detail)
            self.assertEqual("hit", cached.outputs["metadata_resolution"]["cache_state"])
            self.assertEqual("qlu_maven_mirror", cached.outputs["metadata_resolution"]["source"]["kind"])

    def test_environment_lock_returns_current_handoff_on_cache_conflict(self):
        with tempfile.TemporaryDirectory() as raw:
            root_one = Path(raw) / "run-one"
            command_one, request_one, _, _, _, metadata_one = self._metadata_handoff_fixture(root_one)
            command_one = replace(command_one, artifact_refs={
                key: value for key, value in command_one.artifact_refs.items()
                if key != "artifact_handoff" and not key.startswith("handoff:")
            })
            calls = []

            def official_metadata(url, **_kwargs):
                calls.append(url)
                self.assertEqual(MAVEN_METADATA, url)
                response = Mock()
                response.__enter__ = Mock(return_value=response)
                response.__exit__ = Mock(return_value=False)
                response.geturl.return_value = url
                response.read.return_value = metadata_one
                return response

            with patch("modport.handlers.urlopen", side_effect=official_metadata):
                first, first_source = _resolve_neoforge_metadata(
                    root_one, command_one, request_one,
                )
            self.assertEqual(metadata_one, first)
            self.assertEqual("stored", first_source["cache_state"])

            metadata_two = (
                b"<metadata><!-- distinct authenticated handoff snapshot -->"
                b"<versioning><versions><version>26.1.2.105</version>"
                b"<version>26.1.2.106</version></versions></versioning></metadata>"
            )
            root_two = Path(raw) / "run-two"
            command_two, request_two, _, _, _, _ = self._metadata_handoff_fixture(
                root_two, metadata=metadata_two,
            )
            with patch("modport.handlers.urlopen", side_effect=AssertionError("valid handoff conflict must be offline")):
                selected, selected_source = _resolve_neoforge_metadata(
                    root_two, command_two, request_two,
                )
            self.assertEqual(metadata_two, selected)
            self.assertEqual("authenticated_artifact_only_handoff", selected_source["kind"])
            self.assertEqual("conflict", selected_source["cache_state"])
            self.assertEqual(hashlib.sha256(metadata_one).hexdigest(),
                             selected_source["cache_conflict"]["cached_metadata_sha256"])
            self.assertEqual(hashlib.sha256(metadata_two).hexdigest(),
                             selected_source["cache_conflict"]["handoff_metadata_sha256"])

    def test_unpinned_environment_lookup_reuses_mirror_until_refresh(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw) / "new-run"
            command, request, _, _, _, metadata = self._metadata_handoff_fixture(root)
            request = dict(request)
            request["target_loader_version"] = None
            next_root = Path(raw) / "next-run"
            next_command = replace(command, run_dir=str(next_root))
            mirror_metadata = [metadata]
            calls = []

            def fetch(url, **_kwargs):
                calls.append(url)
                if url == MAVEN_METADATA:
                    raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)
                self.assertEqual(MAVEN_METADATA_MIRROR, url)
                response = Mock()
                response.__enter__ = Mock(return_value=response)
                response.__exit__ = Mock(return_value=False)
                response.geturl.return_value = url
                response.read.side_effect = lambda *_args: mirror_metadata[0]
                return response

            with patch("modport.handlers.urlopen", side_effect=fetch):
                first = _resolve_neoforge_metadata(root, command, request)
                second = _resolve_neoforge_metadata(next_root, next_command, request)
                entry = next((root.parent / "environment-cache").glob(
                    "neoforge-unpinned-metadata-v1/*.json"))
                record = json.loads(entry.read_text(encoding="utf-8"))
                record["created_at_epoch"] = time.time() - NEOFORGE_UNPINNED_METADATA_TTL_SECONDS - 1
                record["record_sha256"] = hashlib.sha256(canonical_json({
                    key: value for key, value in record.items() if key != "record_sha256"
                }).encode("utf-8")).hexdigest()
                entry.write_text(canonical_json(record) + "\n", encoding="utf-8")
                mirror_metadata[0] = metadata.replace(b"26.1.2.106", b"26.1.2.107")
                refreshed = _resolve_neoforge_metadata(root, command, request)
                after_refresh = _resolve_neoforge_metadata(next_root, next_command, request)
            self.assertEqual(metadata, first[0])
            self.assertEqual(metadata, second[0])
            self.assertEqual(mirror_metadata[0], refreshed[0])
            self.assertEqual(mirror_metadata[0], after_refresh[0])
            self.assertEqual("26.1.2.107", refreshed[1]["resolved_version"])
            self.assertEqual("qlu_maven_mirror", first[1]["source"]["kind"])
            self.assertEqual("stored", first[1]["cache_state"])
            self.assertEqual("hit", second[1]["cache_state"])
            self.assertEqual("stored", refreshed[1]["cache_state"])
            self.assertEqual("hit", after_refresh[1]["cache_state"])
            self.assertEqual([MAVEN_METADATA, MAVEN_METADATA_MIRROR] * 2, calls)
            self.assertEqual(1, len(list((root.parent / "environment-cache").glob(
                "neoforge-unpinned-metadata-v1/*.json"))))

    def test_unpinned_environment_rejects_invalid_official_index_before_mirror(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw) / "new-run"
            command, request, _, _, _, metadata = self._metadata_handoff_fixture(root)
            request = {**request, "target_loader_version": None}
            calls = []

            def fetch(url, **_kwargs):
                calls.append(url)
                response = Mock()
                response.__enter__ = Mock(return_value=response)
                response.__exit__ = Mock(return_value=False)
                response.geturl.return_value = url
                response.read.return_value = (
                    b"<metadata><versioning><versions></versions></versioning></metadata>"
                    if url == MAVEN_METADATA else metadata
                )
                return response

            with patch("modport.handlers.urlopen", side_effect=fetch):
                selected, provenance = _resolve_neoforge_metadata(root, command, request)
            self.assertEqual(metadata, selected)
            self.assertEqual([MAVEN_METADATA, MAVEN_METADATA_MIRROR], calls)
            self.assertEqual("qlu_maven_mirror", provenance["source"]["kind"])
            self.assertEqual("stored", provenance["cache_state"])

            with patch("modport.handlers.urlopen", side_effect=AssertionError("cache hit must be offline")):
                cached, cache_provenance = _resolve_neoforge_metadata(root, command, request)
            self.assertEqual(metadata, cached)
            self.assertEqual("hit", cache_provenance["cache_state"])

    def test_environment_lock_fails_closed_on_corrupt_metadata_cache(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw) / "new-run"
            command, _, _, mdk_commit, java, _ = self._metadata_handoff_fixture(root)
            execute, probe, build = self._environment_lock_mocks(root, mdk_commit=mdk_commit, java=java)
            with patch("modport.handlers.urlopen", side_effect=AssertionError("handoff seeds cache without network")), \
                    execute, probe, build:
                seeded = LockEnvironmentHandler()(command)
            self.assertEqual("completed", seeded.status, seeded.detail)

            cache_entry = next((root.parent / "environment-cache").glob(
                "neoforge-metadata-v1/*.json"
            ))
            record = json.loads(cache_entry.read_text(encoding="utf-8"))
            record["metadata_sha256"] = "0" * 64
            cache_entry.write_text(json.dumps(record), encoding="utf-8")
            execute, probe, build = self._environment_lock_mocks(root, mdk_commit=mdk_commit, java=java)
            with patch("modport.handlers.urlopen", side_effect=AssertionError("corruption must stop before network")), \
                    execute as run_process, probe, build:
                rejected = LockEnvironmentHandler()(command)
            self.assertEqual("failed", rejected.status)
            self.assertEqual("version_resolution_failed", rejected.error_code)
            self.assertIn("identity or checksum is invalid", rejected.detail)
            run_process.assert_not_called()

    def test_environment_lock_rejects_symlinked_metadata_cache_entry(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw) / "new-run"
            command, _, _, mdk_commit, java, _ = self._metadata_handoff_fixture(root)
            execute, probe, build = self._environment_lock_mocks(root, mdk_commit=mdk_commit, java=java)
            with patch("modport.handlers.urlopen", side_effect=AssertionError("handoff seeds cache without network")), \
                    execute, probe, build:
                seeded = LockEnvironmentHandler()(command)
            self.assertEqual("completed", seeded.status, seeded.detail)

            cache_entry = next((root.parent / "environment-cache").glob(
                "neoforge-metadata-v1/*.json"
            ))
            destination = root.parent / "outside-cache-record.json"
            destination.write_bytes(cache_entry.read_bytes())
            cache_entry.unlink()
            cache_entry.symlink_to(destination)
            execute, probe, build = self._environment_lock_mocks(root, mdk_commit=mdk_commit, java=java)
            with patch("modport.handlers.urlopen", side_effect=AssertionError("symlink must stop before network")), \
                    execute as run_process, probe, build:
                rejected = LockEnvironmentHandler()(command)
            self.assertEqual("failed", rejected.status)
            self.assertEqual("version_resolution_failed", rejected.error_code)
            self.assertIn("symlinks are forbidden", rejected.detail)
            run_process.assert_not_called()

    def test_environment_metadata_cache_keeps_first_concurrent_provenance(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw) / "new-run"
            _, request, _, _, _, metadata = self._metadata_handoff_fixture(root)
            cache_location = _neoforge_metadata_cache_location(request)
            self.assertIsNotNone(cache_location)
            barrier = Barrier(2)
            sources = (
                {"kind": "official_maven_metadata", "source_url": MAVEN_METADATA,
                 "verified_at_epoch": time.time(),
                 "metadata_sha256": hashlib.sha256(metadata).hexdigest()},
                {"kind": "qlu_maven_mirror", "source_url": MAVEN_METADATA_MIRROR,
                 "verified_at_epoch": time.time(),
                 "metadata_sha256": hashlib.sha256(metadata).hexdigest()},
            )

            def publish(source):
                barrier.wait(timeout=5)
                return _publish_environment_metadata_cache(
                    *cache_location, metadata, source,
                )

            with ThreadPoolExecutor(max_workers=2) as pool:
                outcomes = list(pool.map(publish, sources))
            self.assertEqual(outcomes[0][0], outcomes[1][0])
            self.assertEqual(outcomes[0][1]["source"], outcomes[1][1]["source"])
            cache_entry = next((root.parent / "environment-cache").glob(
                "neoforge-metadata-v1/*.json"
            ))
            cache_record = json.loads(cache_entry.read_text(encoding="utf-8"))
            self.assertIn(cache_record["source"]["kind"], {
                "official_maven_metadata", "qlu_maven_mirror",
            })

    def test_concurrent_cold_or_expired_metadata_uses_one_mirror_fetch(self):
        for expired_unpinned in (False, True):
            with self.subTest(expired_unpinned=expired_unpinned), tempfile.TemporaryDirectory() as raw:
                root = Path(raw) / "first-run"
                command, request, _, _, _, metadata = self._metadata_handoff_fixture(root)
                command = replace(command, artifact_refs={
                    key: value for key, value in command.artifact_refs.items()
                    if key != "artifact_handoff" and not key.startswith("handoff:")
                })
                request = dict(request)
                if expired_unpinned:
                    request["target_loader_version"] = None
                    location = _neoforge_metadata_cache_location(request)
                    source = {"kind": "qlu_maven_mirror", "source_url": MAVEN_METADATA_MIRROR,
                              "verified_at_epoch": time.time(),
                              "metadata_sha256": hashlib.sha256(metadata).hexdigest()}
                    _publish_environment_metadata_cache(*location, metadata, source)
                    entry = location[1]
                    record = json.loads(entry.read_text(encoding="utf-8"))
                    record["created_at_epoch"] = time.time() - NEOFORGE_UNPINNED_METADATA_TTL_SECONDS - 1
                    record["record_sha256"] = hashlib.sha256(canonical_json({
                        key: value for key, value in record.items() if key != "record_sha256"
                    }).encode("utf-8")).hexdigest()
                    entry.write_text(canonical_json(record) + "\n", encoding="utf-8")

                next_root = Path(raw) / "second-run"
                next_command = replace(command, run_dir=str(next_root))
                first_thread = []
                first_fetch_started = Event()
                second_miss = Event()
                second_network_call = Event()
                calls = []
                calls_lock = Lock()

                def read_cache(*args):
                    cached = _read_environment_metadata_cache(*args)
                    if first_thread and get_ident() != first_thread[0] and cached is None:
                        second_miss.set()
                    return cached

                def fetch(url, **_kwargs):
                    with calls_lock:
                        calls.append(url)
                        position = len(calls)
                    if position == 1:
                        first_fetch_started.set()
                        self.assertTrue(second_miss.wait(timeout=5))
                        # Keep the first fetch in flight after both Runs have
                        # observed the miss. A second fetch would be a duplicate.
                        second_network_call.wait(timeout=0.2)
                    else:
                        second_network_call.set()
                    if url == MAVEN_METADATA:
                        raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)
                    response = Mock()
                    response.__enter__ = Mock(return_value=response)
                    response.__exit__ = Mock(return_value=False)
                    response.geturl.return_value = url
                    response.read.return_value = metadata
                    return response

                def first():
                    first_thread.append(get_ident())
                    return _resolve_neoforge_metadata(root, command, request)

                with patch("modport.handlers._read_environment_metadata_cache", side_effect=read_cache), \
                        patch("modport.handlers.urlopen", side_effect=fetch), \
                        ThreadPoolExecutor(max_workers=2) as pool:
                    first_result = pool.submit(first)
                    self.assertTrue(first_fetch_started.wait(timeout=5))
                    second_result = pool.submit(
                        _resolve_neoforge_metadata, next_root, next_command, request)
                    outcomes = [first_result.result(timeout=10), second_result.result(timeout=10)]
                self.assertEqual([MAVEN_METADATA, MAVEN_METADATA_MIRROR], calls)
                self.assertEqual([metadata, metadata], [result[0] for result in outcomes])
                self.assertEqual(["stored", "hit"],
                                 [result[1]["cache_state"] for result in outcomes])

    def test_registry_rejects_malformed_rubric_before_operation(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            command = self._command(
                root,
                "input_validation",
                payload={"source_repository": "https://example.invalid/repo.git"},
            )
            (root / "artifacts" / "acceptance-rubric.json").write_text(
                "{}\n", encoding="utf-8"
            )
            handler = build_registry()["modport.source"]
            with patch("modport.handlers._exec") as execute:
                result = handler(command)
            self.assertEqual(result.status, "failed")
            self.assertEqual(result.error_code, "acceptance_rubric_invalid")
            execute.assert_not_called()

    def test_source_evidence_updates_do_not_require_matching_digest(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            command = self._command(root, "environment_lock")
            source = root / "artifacts" / "source.json"
            source.write_text('{"source_commit":"' + "a" * 40 + '"}\n', encoding="utf-8")
            command = replace(
                command,
                artifact_refs={
                    **dict(command.artifact_refs),
                    "source_evidence": {
                        "path": "artifacts/source.json",
                        "sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
                        "media_type": "application/json",
                    },
                },
            )
            source.write_text('{"source_commit":"' + "b" * 40 + '"}\n', encoding="utf-8")
            path, _ = _read_artifact_ref(root, command, "source_evidence")
            self.assertEqual(json.loads(path.read_text())["source_commit"], "b" * 40)

    @staticmethod
    def _write_contract_fixture(root: Path, *, reviewer_id: str = "contract-review-agent", executor: str = "gametest") -> tuple[Path, dict]:
        baseline = root / "baseline" / ".modport"
        baseline.mkdir(parents=True, exist_ok=True)
        (root / "artifacts").mkdir(parents=True, exist_ok=True)
        rubric = acceptance_rubric()
        rubric_path = root / "artifacts" / "acceptance-rubric.json"
        rubric_path.write_text(canonical_json(rubric) + "\n", encoding="utf-8")
        contract = {
            "schema_version": 1,
            "contract_id": "modport.functional-contract.v1",
            "generator_id": "characterization-agent",
            "source_fingerprint": "immutable-source-commit",
            "rubric_id": rubric["rubric_id"],
            "rubric_version": rubric["rubric_version"],
            "rubric_sha256": rubric["rubric_sha256"],
            "baseline_gradle_tasks": ["test"],
            "baseline_evidence_files": [".modport/evidence/test-results.json"],
            "test_evidence": {
                "game_test.behavior_1": {
                    "path": ".modport/evidence/test-results.json",
                    "evidence_kind": "runtime",
                    "executor": executor,
                    "runtime_operations": ["start server", "invoke behavior", "observe state"],
                    "test_source_files": [".modport/tests/BehaviorTest.java"],
                }
            },
            "behaviors": [
                {
                    "id": "behavior-1",
                    "source_evidence": "src/main/java/Example.java",
                    "preconditions": ["the server is running"],
                    "action": ["apply the migration behavior"],
                    "assertions": ["the observable result is preserved"],
                    "side": "server",
                    "test_mapping": ["game_test.behavior_1"],
                }
            ],
        }
        source = baseline / "functional-contract.json"
        source.write_text(canonical_json(contract) + "\n", encoding="utf-8")
        test_source = baseline / "tests" / "BehaviorTest.java"
        test_source.parent.mkdir(parents=True, exist_ok=True)
        test_source.write_text(
            "final class BehaviorTest { void exercisesRuntimeBehavior() {} }\n",
            encoding="utf-8",
        )
        source_sha = hashlib.sha256(source.read_bytes()).hexdigest()
        evidence_file = baseline / "evidence" / "test-results.json"
        evidence_file.parent.mkdir(parents=True, exist_ok=True)
        evidence_record = {
            "test_id": "game_test.behavior_1",
            "evidence_kind": "runtime",
            "executor": executor,
            "source_fingerprint": "immutable-source-commit",
            "execution_inputs": ["Forge 1.20.1 baseline"],
            "runtime_operations": ["start server", "invoke behavior", "observe state"],
            "observations": {"assertion": "observable result preserved"},
            "status": "passed",
        }
        evidence_file.write_text(canonical_json(evidence_record) + "\n", encoding="utf-8")
        (root / "artifacts" / "baseline-contract-tests.json").write_text(
            canonical_json({"candidate_sha256": source_sha, "rubric_sha256": rubric["rubric_sha256"], "tasks": ["test"], "exit_code": 0, "log_sha256": "0" * 64, "evidence_files": {".modport/evidence/test-results.json": hashlib.sha256(evidence_file.read_bytes()).hexdigest()}}) + "\n",
            encoding="utf-8",
        )
        review = {
            "reviewer_id": reviewer_id,
            "generator_id": "characterization-agent",
            "review_id": "review-1",
            "verdict": "approved",
            "candidate_sha256": source_sha,
            "notes": "independent baseline review passed",
            "baseline_commands": ["./gradlew test"],
            "rubric_id": rubric["rubric_id"],
            "rubric_version": rubric["rubric_version"],
            "rubric_sha256": rubric["rubric_sha256"],
        }
        (baseline / "contract-review.json").write_text(canonical_json(review) + "\n", encoding="utf-8")
        (root / "artifacts" / "source.json").write_text(
            canonical_json({"source_commit": "immutable-source-commit"}) + "\n", encoding="utf-8"
        )
        java = root / "toolchains" / "gradle-cache" / "jdks" / "fake" / "bin" / "java"
        java.parent.mkdir(parents=True, exist_ok=True)
        java.write_bytes(b"fake-java-25")
        request = MigrationRequest("example", "https://example.invalid/repo.git", "1.20.1", "26.1.2", budget=Budget())
        base_manifest = LockedManifest(
            request, "26.1.2.101", source_commit="immutable-source-commit",
            java_toolchain={"executable": "jdks/fake/bin/java", "java_sha256": hashlib.sha256(java.read_bytes()).hexdigest()},
        )
        locked_manifest = LockedManifest.from_mapping({**base_manifest.to_dict(), "manifest_sha256": manifest_sha256(base_manifest)})
        (root / "artifacts" / "locked-manifest.json").write_text(canonical_json(locked_manifest.to_dict(include_hash=True)) + "\n", encoding="utf-8")
        return source, contract

    def test_freeze_requires_independent_approval_and_binds_an_immutable_lock(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source, _ = self._write_contract_fixture(root)
            command = self._command(root, "contract_review_freeze")

            result = FreezeContractHandler()(command)
            self.assertEqual(result.status, "completed")
            lock_path = root / "artifacts" / "functional-contract.lock.json"
            lock = json.loads(lock_path.read_text(encoding="utf-8"))
            lock_digest = lock.pop("lock_sha256")
            self.assertEqual(lock_digest, hashlib.sha256(canonical_json(lock).encode("utf-8")).hexdigest())
            self.assertEqual(lock["candidate_file_sha256"], hashlib.sha256(source.read_bytes()).hexdigest())

            typed = CharacterizationContract.from_mapping(lock["contract"])
            review_data = lock["review"]
            review = ReviewRecord(
                reviewer_id=review_data["reviewer_id"],
                generator_id=review_data["generator_id"],
                contract_sha256=review_data["contract_sha256"],
                status=review_data["status"],
            )
            frozen = freeze_contract(typed, review)
            self.assertTrue(frozen.verify())
            tampered = json.loads(canonical_json(lock))
            tampered["contract"]["entries"][0]["assertions"] = ["weaker assertion"]
            with self.assertRaises(FrozenContractError):
                frozen.assert_unchanged(tampered["contract"])

            # An approval by the generator itself is rejected as non-independent.
            self._write_contract_fixture(root, reviewer_id="characterization-agent")
            rejected = FreezeContractHandler()(command)
            self.assertEqual(rejected.status, "failed")
            self.assertEqual(rejected.error_code, "review_not_independent")

    def test_v17_freeze_uses_execution_observation_without_overwriting_old_lock(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            self._write_contract_fixture(root)
            old_lock = root / "artifacts" / "functional-contract.lock.json"
            old_lock.write_text('{"historical":true}\n', encoding="utf-8")
            command = self._command(root, "contract_freeze")
            command = replace(command, options={**command.options, "workflow_version": 17})
            result = FreezeContractHandler()(command)
            self.assertEqual(result.status, "completed", result.detail)
            self.assertEqual(old_lock.read_text(encoding="utf-8"), '{"historical":true}\n')
            ref = result.outputs["artifact_refs"]["functional_contract_lock"]
            observation = root / ref["path"]
            self.assertIn("artifacts/executions/command-1/", ref["path"])
            self.assertEqual(ref["sha256"], hashlib.sha256(observation.read_bytes()).hexdigest())
            self.assertEqual(result.outputs["contract_sha256"],
                             json.loads(observation.read_text())["lock_sha256"])

    def test_locked_artifact_verifier_rejects_tampering(self):
        from modport import Budget, LockedManifest, MigrationRequest, manifest_sha256

        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            self._write_contract_fixture(root)
            command = self._command(root, "contract_review_freeze")
            self.assertEqual(FreezeContractHandler()(command).status, "completed")
            locked = LockedManifest.from_mapping(json.loads((root / "artifacts" / "locked-manifest.json").read_text(encoding="utf-8")))
            lock = json.loads((root / "artifacts" / "functional-contract.lock.json").read_text(encoding="utf-8"))
            anchors = {"manifest_sha256": locked.manifest_sha256, "contract_lock_sha256": lock["lock_sha256"]}
            self.assertEqual(_verify_locked_artifacts(root, anchors)["manifest_sha256"], locked.manifest_sha256)
            data = json.loads((root / "artifacts" / "functional-contract.lock.json").read_text(encoding="utf-8"))
            data["contract"]["entries"][0]["assertions"] = ["weakened"]
            (root / "artifacts" / "functional-contract.lock.json").write_text(canonical_json(data) + "\n", encoding="utf-8")
            with self.assertRaises(ValueError):
                _verify_locked_artifacts(root, anchors)

    def test_build_sandbox_hides_codex_credentials(self):
        self._require_sandbox_namespaces()
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            worktree = root / "worktree"
            worktree.mkdir()
            private_home = shlex.quote(str(Path.home() / ".codex"))
            args = _sandboxed_build_command(root, worktree, ["bash", "-lc", f"test ! -e {private_home} && test ! -e /etc/shadow && test ! -e /etc/ssl/private && test \"$PWD\" = /workspace && java -version"])
            import subprocess
            self.assertEqual(subprocess.run(args, capture_output=True).returncode, 0)

    def test_codex_cli_uses_compatible_explicit_sandbox_flags(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            (root / "worktree").mkdir()
            (root / "artifacts").mkdir()
            command = self._command(root, "implementation", payload={"request": {"budget": {"max_agent_assignments": 2}}})
            import subprocess
            with patch("modport.handlers._exec", return_value=subprocess.CompletedProcess([], 0, "")) as execute:
                result = CodexStageHandler(prompt="edit only")(command)
            self.assertEqual(result.status, "completed")
            args = execute.call_args.args[0]
            self.assertIn("--sandbox", args)
            self.assertIn("--search", args)
            self.assertIn("--json", args)
            self.assertNotIn("-o", args)
            self.assertNotIn("--approve-for-me", args)
            self.assertEqual('-', args[-1])
            self.assertIn("Work autonomously", execute.call_args.kwargs['input_text'])
            self.assertIn("--permission-mode auto", execute.call_args.kwargs['input_text'])

    def test_v23_planner_launches_sol_high(self):
        from modport.prompt_compressor import PromptCompressor
        from modport.workflow import WORKFLOW_VERSION

        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            (root / "worktree").mkdir()
            command = self._command(root, "migration_plan")
            command = replace(command, options={**command.options,
                "workflow_version": WORKFLOW_VERSION,
                "model": "gpt-6-luna", "reasoning_effort": "max"})
            compressor = PromptCompressor(catalog={"models": [
                {"slug": "gpt-6-sol", "context_window": 1_000_000}]})
            with (
                patch("modport.handlers.PromptCompressor.from_environment", return_value=compressor),
                patch("modport.handlers._exec", return_value=subprocess.CompletedProcess([], 0, "")) as execute,
            ):
                result = CodexStageHandler(prompt="Prepare the migration plan")(command)
            self.assertEqual("completed", result.status, result.detail)
            args = execute.call_args.args[0]
            self.assertEqual("gpt-6-sol", args[args.index("-m") + 1])
            self.assertIn('model_reasoning_effort="high"', args)

    def test_codex_stage_fails_atomically_when_required_output_is_missing(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            (root / "baseline").mkdir()
            (root / "artifacts").mkdir()
            command = self._command(
                root,
                "contract_generation",
                payload={"request": {"budget": {"max_agent_assignments": 2}}},
            )
            with (
                patch("modport.handlers._exec", return_value=subprocess.CompletedProcess([], 0, "")),
                patch("modport.handlers._baseline_changes_are_isolated", return_value=(True, [])),
            ):
                result = CodexStageHandler(
                    prompt="generate contract",
                    baseline=True,
                    required_paths=(".modport/functional-contract.json",),
                )(command)
            self.assertEqual(result.status, "failed")
            self.assertEqual(result.error_code, "agent_output_missing")
            self.assertEqual(result.outputs["missing_required_paths"], [".modport/functional-contract.json"])

    def test_codex_timeout_preserves_partial_log_for_repair_evidence(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            (root / 'worktree').mkdir()
            (root / 'artifacts').mkdir()
            command = self._command(root, 'target_revise')
            def timeout(*args, **kwargs):
                kwargs['log'].write_text('planner handoff was read; partial coder output\n')
                raise subprocess.TimeoutExpired('codex', 1)
            with patch('modport.handlers._exec', side_effect=timeout):
                result = CodexStageHandler('repair')(command)
            self.assertEqual('agent_timeout', result.error_code)
            self.assertIn('partial coder output', (root / result.outputs['log']).read_text())

    def test_large_agent_prompt_is_delivered_and_archived(self):
        from modport.telemetry import run_process
        from modport.prompt_compressor import PromptCompressor
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            (root / 'worktree').mkdir()
            command = self._command(root, 'implementation')
            task = '完整修复历史\n' * 30000
            captured = {}
            def execute(args, **kwargs):
                self.assertEqual('-', args[-1])
                self.assertLess(max(map(len, args)), 4096)
                captured['prompt'] = kwargs['input_text']
                code = ('import json,sys; data=sys.stdin.buffer.read(); '
                        'print(json.dumps({"type":"item.completed", "item":'
                        '{"type":"agent_message", "text":str(len(data))}}))')
                return run_process([sys.executable, '-c', code], **kwargs)
            # This test exercises stdin transport and archival. Give the
            # compressor a deliberately larger model window so it does not
            # invoke a real summary agent in this transport-only fixture.
            compressor = PromptCompressor(catalog={"models": [{"slug": "gpt-6-luna", "context_window": 1_000_000}]})
            with patch('modport.handlers.PromptCompressor.from_environment', return_value=compressor), \
                 patch('modport.handlers._exec', side_effect=execute):
                result = CodexStageHandler(task)(command)
            self.assertEqual('completed', result.status, result.detail)
            received = (root / result.outputs['last_message']).read_text().strip()
            self.assertEqual(str(len(captured['prompt'].encode())), received)
            prompt_ref = result.outputs['artifact_refs']['agent_prompt']
            self.assertEqual(captured['prompt'], (root / prompt_ref['path']).read_text())

    def test_agent_launch_failure_is_explicit_and_retains_evidence(self):
        from modport.prompt_compressor import PromptCompressor

        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            (root / 'worktree').mkdir()
            command = self._command(root, 'implementation')
            shell_dir = root / 'artifacts' / 'executions' / command.command_id / 'opencode-shell'
            shell_dir.mkdir(parents=True)
            receipt_name = 'a' * 32 + '.json'
            (shell_dir / receipt_name).write_text('{"exit_code":0}')
            (shell_dir / 'latest-receipt.json').write_text(json.dumps({
                'schema_version': 1, 'command_id': command.command_id,
                'receipt': receipt_name,
            }))
            def execute(**kwargs):
                kwargs['log'].parent.mkdir(parents=True, exist_ok=True)
                kwargs['log'].write_text('launch failed: E2BIG')
                Path(str(kwargs['log']) + '.stdin.txt').write_text('redacted assignment')
                raise OSError(7, 'sensitive environment content must not be echoed')
            compressor = PromptCompressor(catalog={"models": [
                {"slug": "gpt-6-luna", "context_window": 1_000_000}]})
            with patch('modport.handlers.PromptCompressor.from_environment', return_value=compressor), \
                    patch('modport.opencode_agent.run_agent', side_effect=execute):
                result = CodexStageHandler('task')(command)
            self.assertEqual('failed', result.status, (result.error_code, result.detail))
            self.assertEqual('agent_launch_failed', result.error_code)
            self.assertNotIn('sensitive environment', result.detail)
            self.assertIn('prompt_compression_source', result.outputs['artifact_refs'])
            self.assertIn('project_command:' + 'a' * 32, result.outputs['artifact_refs'])
            self.assertNotIn('project_command:latest-receipt', result.outputs['artifact_refs'])
            self.assertTrue((root / result.outputs['log']).is_file())

    def test_codex_required_output_is_snapshotted_with_sha(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            baseline = root / "baseline"
            baseline.mkdir()
            command = self._command(root, "contract_generation")

            def execute(args, **_kwargs):
                output = baseline / ".modport" / "functional-contract.json"
                output.parent.mkdir()
                output.write_text('{"candidate":true}\n', encoding="utf-8")
                return subprocess.CompletedProcess(args, 0, json.dumps({
                    "type": "item.completed",
                    "item": {"type": "agent_message", "text": "generated"},
                }) + "\n")

            with (
                patch("modport.handlers._exec", side_effect=execute),
                patch(
                    "modport.handlers._baseline_changes_are_isolated",
                    return_value=(True, []),
                ),
            ):
                result = CodexStageHandler(
                    prompt="generate contract",
                    baseline=True,
                    required_paths=(".modport/functional-contract.json",),
                )(command)
            self.assertEqual(result.status, "completed")
            refs = result.outputs["artifact_refs"]
            artifact_id = "stage_output:contract_generation:.modport/functional-contract.json"
            self.assertIn(artifact_id, refs)
            snapshot = root / refs[artifact_id]["path"]
            self.assertEqual(
                hashlib.sha256(snapshot.read_bytes()).hexdigest(),
                refs[artifact_id]["sha256"],
            )
            self.assertIn("agent_log:contract_generation", refs)

    def test_sandbox_phase_caches_are_isolated_and_hide_host_sensitive_paths(self):
        self._require_sandbox_namespaces()
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            worktree = root / "worktree"
            worktree.mkdir()
            probe = (
                "test ! -e /root && test ! -e /etc/shadow && "
                "test ! -e /etc/ssl/private && "
                "touch /gradle-cache/phase-marker"
            )
            environment_args = _sandboxed_build_command(
                root, worktree, ["bash", "-lc", probe], cache_name="environment-gradle-cache"
            )
            contract_args = _sandboxed_build_command(
                root, worktree, ["bash", "-lc", probe], cache_name="contract-gradle-cache"
            )
            self.assertNotEqual(environment_args, contract_args)
            self.assertIn(str(root / "toolchains" / "environment-gradle-cache"), environment_args)
            self.assertIn(str(root / "toolchains" / "contract-gradle-cache"), contract_args)
            self.assertEqual(subprocess.run(environment_args, capture_output=True).returncode, 0)
            self.assertEqual(subprocess.run(contract_args, capture_output=True).returncode, 0)
            self.assertTrue((root / "toolchains" / "environment-gradle-cache" / "phase-marker").exists())
            self.assertTrue((root / "toolchains" / "contract-gradle-cache" / "phase-marker").exists())
            self.assertNotEqual(
                root / "toolchains" / "environment-gradle-cache",
                root / "toolchains" / "contract-gradle-cache",
            )

    def test_gradle_dependency_snapshot_is_read_only_and_override_is_reserved(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            worktree = root / "worktree"
            worktree.mkdir()
            snapshot = root / "environment-cache" / "snapshot"
            (snapshot / "modules-2").mkdir(parents=True)
            command = _sandboxed_build_command(
                root, worktree, ["/workspace/gradlew", "compileJava"],
                gradle_ro_cache=snapshot,
            )
            mount = command.index(str(snapshot))
            self.assertEqual("--ro-bind", command[mount - 1])
            self.assertEqual("/gradle-ro-cache", command[mount + 1])
            self.assertIn(["--setenv", "GRADLE_RO_DEP_CACHE", "/gradle-ro-cache"],
                          [command[index:index + 3] for index in range(len(command) - 2)])
            with self.assertRaisesRegex(ValueError, "invalid sandbox environment override"):
                _sandboxed_build_command(
                    root, worktree, ["/workspace/gradlew", "compileJava"],
                    environment={"GRADLE_RO_DEP_CACHE": "/tmp/override"},
                )

    def test_acceptance_rejects_forbidden_dependencies_before_external_checks(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            java = root / "worktree" / "src" / "main" / "java" / "Example.java"
            java.parent.mkdir(parents=True)
            java.write_text(
                "import net.minecraftforge.common.MinecraftForge;\n",
                encoding="utf-8",
            )
            acceptance = AcceptancePreflightHandler()
            result = acceptance(self._command(root, "acceptance"))
            self.assertEqual(result.status, "failed")
            self.assertEqual(result.error_code, "forbidden_dependency")

    def test_validate_input_isolates_worktrees_at_fixed_commit_and_records_tag_overlay(self):
        def run_git(*args, cwd):
            return subprocess.run(
                ["git", *args], cwd=cwd, check=True, text=True,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            ).stdout.strip()

        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source = root / "source"
            source.mkdir()
            run_git("init", "-q", cwd=source)
            run_git("config", "user.name", "ModPort Test", cwd=source)
            run_git("config", "user.email", "modport-test@example.invalid", cwd=source)
            (source / "marker.txt").write_text("fixed source\n", encoding="utf-8")
            run_git("add", "marker.txt", cwd=source)
            run_git("commit", "-qm", "initial source", cwd=source)
            commit = run_git("rev-parse", "HEAD", cwd=source)

            run_root = root / "run"
            request = {
                "source_repository": str(source),
                "source_revision": commit,
            }
            result = ValidateInputHandler()(self._command(run_root, "input_validation", payload=request))
            self.assertEqual(result.status, "completed")
            self.assertEqual(result.outputs["source_commit"], commit)
            self.assertEqual(
                result.outputs["baseline_overlay"],
                {
                    "kind": "local_git_tag",
                    "tag": "0.0.0-modport-baseline",
                    "reason": "source has no describe-compatible release tag",
                },
            )
            baseline = run_root / "baseline"
            worktree = run_root / "worktree"
            self.assertEqual(run_git("rev-parse", "HEAD", cwd=baseline), commit)
            self.assertEqual(run_git("rev-parse", "HEAD", cwd=worktree), commit)
            self.assertNotEqual(baseline.resolve(), worktree.resolve())
            self.assertIn("0.0.0-modport-baseline", run_git("tag", "--list", cwd=run_root / "repository.git").splitlines())

            # A file created in one checkout must not leak into the other.
            (worktree / "worktree-only.txt").write_text("migration edits\n", encoding="utf-8")
            self.assertFalse((baseline / "worktree-only.txt").exists())
            self.assertEqual((baseline / "marker.txt").read_text(encoding="utf-8"), "fixed source\n")

    def test_static_pseudo_runtime_evidence_is_rejected_before_gradle(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source, contract = self._write_contract_fixture(root)
            contract["test_evidence"]["game_test.behavior_1"] = {
                "path": ".modport/evidence/test-results.json",
                "evidence_kind": "runtime",
                "executor": "static_analysis",
                "runtime_operations": ["search source strings", "parse JSON"],
            }
            source.write_text(canonical_json(contract) + "\n", encoding="utf-8")
            command = self._command(root, "contract_review_freeze")
            with patch("modport.handlers._exec") as execute:
                result = BaselineContractVerificationHandler()(command)
            self.assertEqual(result.status, "failed")
            self.assertEqual(result.error_code, "baseline_evidence_invalid")
            self.assertIn("unsupported executor", result.detail)
            execute.assert_not_called()

    def test_v20_failed_entry_probe_does_not_launch_full_harness(self):
        for mode in ('failure', 'timeout'):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as raw:
                root = Path(raw)
                self._write_contract_fixture(root)
                command = self._command(root, 'contract_verify')
                command = replace(command, options={**command.options, 'workflow_version': 20})
                def execute(args, **kwargs):
                    self.assertIn('--dry-run', args)
                    self.assertLessEqual(kwargs['timeout'], 120)
                    if mode == 'timeout':
                        raise subprocess.TimeoutExpired(args, kwargs['timeout'])
                    return subprocess.CompletedProcess(args, 1, 'task missing')
                with patch('modport.handlers._exec', side_effect=execute) as run:
                    result = BaselineContractVerificationHandler()(command)
                self.assertEqual('harness_entry_probe_' + ('failed' if mode == 'failure' else 'timeout'),
                                 result.error_code, result.detail)
                self.assertFalse(result.outputs['harness_executed'])
                self.assertTrue(result.outputs['entry_probe_executed'])
                self.assertEqual(1, run.call_count)

    def test_v25_timed_out_entry_probe_preserves_bounded_log_for_review(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            self._write_contract_fixture(root)
            command = self._command(root, 'contract_verify')
            command = replace(command, options={**command.options,
                'workflow_version': 25, 'deadline_epoch': time.time() + 20})
            raw_log = (b'configuration started\n' + ('中' * 700000).encode('utf-8')
                       + b'\nlast progress before timeout\n')

            def execute(args, **kwargs):
                self.assertIn('--dry-run', args)
                self.assertLess(kwargs['timeout'], 16)
                self.assertGreater(kwargs['timeout'], 0)
                kwargs['log'].parent.mkdir(parents=True, exist_ok=True)
                kwargs['log'].write_bytes(raw_log)
                raise subprocess.TimeoutExpired(args, kwargs['timeout'])

            with patch('modport.handlers._exec', side_effect=execute) as run:
                result = BaselineContractVerificationHandler()(command)
            self.assertEqual('harness_entry_probe_timeout', result.error_code)
            self.assertEqual('unverified', result.outputs['acceptance_status'])
            self.assertFalse(result.outputs['harness_executed'])
            self.assertEqual(1, run.call_count)
            ref = result.outputs['artifact_refs']['baseline_entry_probe']
            excerpt = (root / ref['path']).read_bytes()
            self.assertTrue(excerpt.startswith(b'configuration started\n'))
            self.assertTrue(excerpt.endswith(b'last progress before timeout\n'))
            self.assertIn(b'[MODPORT LOG MIDDLE OMITTED]', excerpt)
            excerpt.decode('utf-8')
            self.assertLess(len(excerpt), 1024 * 1024 + 128)
            self.assertEqual(hashlib.sha256(excerpt).hexdigest(), ref['sha256'])
            self.assertEqual(len(raw_log), ref['metadata']['raw_log_bytes'])
            self.assertTrue(ref['metadata']['truncated'])
            self.assertEqual(result.outputs['probe_log'], ref['metadata']['raw_log'])

    def test_v25_entry_probe_log_capture_failure_keeps_original_timeout(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            self._write_contract_fixture(root)
            command = self._command(root, 'contract_verify')
            command = replace(command, options={**command.options, 'workflow_version': 25})
            with patch('modport.handlers._exec', side_effect=subprocess.TimeoutExpired('gradle', 1)):
                result = BaselineContractVerificationHandler()(command)
            self.assertEqual('harness_entry_probe_timeout', result.error_code)
            self.assertEqual('unverified', result.outputs['acceptance_status'])
            self.assertFalse(result.outputs['harness_executed'])
            self.assertEqual('FileNotFoundError', result.outputs['probe_log_capture_error'])
            self.assertNotIn('probe_log', result.outputs)
            self.assertNotIn('baseline_entry_probe', result.outputs['artifact_refs'])

    def test_v25_entry_probe_skips_launch_without_capture_budget(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            self._write_contract_fixture(root)
            command = self._command(root, 'contract_verify')
            command = replace(command, options={**command.options,
                'workflow_version': 25, 'deadline_epoch': time.time() + 4})
            with patch('modport.handlers._exec') as execute:
                result = BaselineContractVerificationHandler()(command)
            execute.assert_not_called()
            self.assertEqual('budget_exhausted', result.error_code)
            self.assertEqual('unverified', result.outputs['acceptance_status'])
            self.assertFalse(result.outputs['process_executed'])
            self.assertFalse(result.outputs['entry_probe_executed'])

    def test_v26_short_client_workload_records_host_window(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source, contract = self._write_contract_fixture(root, executor='client_smoke')
            contract['baseline_gradle_tasks'] = ['runClient']
            source.write_text(canonical_json(contract) + '\n', encoding='utf-8')
            from modport.client_harness import client_harness_support_files
            command = self._command(root, 'contract_verify')
            refs = dict(command.artifact_refs)
            for relative, contents in client_harness_support_files().items():
                path = root / 'artifacts/harness-support' / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(contents)
                refs['harness_support:' + relative] = {
                    'path': path.relative_to(root).as_posix(),
                    'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}
            command = replace(command, artifact_refs=refs, options={**command.options,
                'workflow_version': 26, 'deadline_epoch': time.time() + 20})
            def execute(args, *, log, **kwargs):
                self.assertIn('--dry-run', args)
                log.write_text('BUILD SUCCESSFUL\n')
                return subprocess.CompletedProcess(args, 0, 'BUILD SUCCESSFUL\n')

            with patch('modport.handlers._exec', side_effect=execute) as run:
                result = BaselineContractVerificationHandler()(command)
            self.assertEqual(run.call_count, 1)
            self.assertEqual(result.error_code, 'budget_exhausted')
            self.assertFalse(result.outputs['harness_executed'])
            self.assertLess(result.outputs['workload_budget']['launcher_seconds'], 20)
            self.assertIn('baseline_entry_probe', result.outputs['artifact_refs'])

    def test_v26_baseline_reports_remain_bound_to_each_verifier(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            self._write_contract_fixture(root)
            legacy_report = (root / 'artifacts/baseline-contract-tests.json').read_bytes()
            base = self._command(root, 'contract_verify')

            def execute(args, *, log, **_kwargs):
                if '--dry-run' in args:
                    log.write_text('task resolved\n', encoding='utf-8')
                    return subprocess.CompletedProcess(args, 0, 'BUILD SUCCESSFUL\n')
                log.write_text('fresh compile failure\n', encoding='utf-8')
                return subprocess.CompletedProcess(args, 1, 'fresh compile failure\n')

            results = []
            with patch('modport.handlers._exec', side_effect=execute):
                for number in (1, 2):
                    command = replace(base, command_id=f'verifier-{number}',
                        options={**base.options, 'workflow_version': 26,
                                 'deadline_epoch': time.time() + 120})
                    results.append(BaselineContractVerificationHandler()(command))

            refs = [result.outputs['artifact_refs']['baseline_contract_tests_candidate']
                    for result in results]
            self.assertEqual('failed', results[0].status)
            self.assertEqual('failed', results[1].status)
            self.assertNotEqual(refs[0]['path'], refs[1]['path'])
            self.assertEqual('artifacts/executions/verifier-1/baseline-contract-tests.json',
                             refs[0]['path'])
            for result, ref in zip(results, refs):
                report = _fresh_baseline_report(replace(base,
                    options={**base.options, 'workflow_version': 26},
                    upstream_results={'contract_verify': result.to_dict()}), root)
                self.assertEqual(root / ref['path'], report)
                self.assertEqual(ref['sha256'], file_digest(report))
            self.assertEqual(legacy_report,
                (root / 'artifacts/baseline-contract-tests.json').read_bytes())
            first_report = root / refs[0]['path']
            first_report.write_text('{}\n', encoding='utf-8')
            with self.assertRaisesRegex(ValueError, 'digest changed'):
                _fresh_baseline_report(replace(base,
                    options={**base.options, 'workflow_version': 26},
                    upstream_results={'contract_verify': results[0].to_dict()}), root)

    def test_v27_compile_package_runs_baseline_tests_and_retains_harness_compile_failure(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            self._write_contract_fixture(root)
            (root / 'baseline' / 'gradlew').write_text('#!/bin/sh\n', encoding='utf-8')
            (root / 'artifacts/source.json').write_text(
                canonical_json({'source_commit': 'immutable-source-commit'}) + '\n',
                encoding='utf-8')
            base = self._command(root, 'contract_verify')
            command = replace(base, options={**base.options, 'workflow_version': 27,
                'validation_policy': {'scope': 'compile_package'}})

            def execute(args, *, log, **_kwargs):
                if '--dry-run' in args:
                    output, exit_code = '> Task :test SKIPPED\n', 0
                else:
                    output = ('> Task :compileJava FAILED\n'
                              '/workspace/.modport/harness/Behavior.java:1: error: cannot find symbol\n')
                    exit_code = 1
                Path(log).parent.mkdir(parents=True, exist_ok=True)
                Path(log).write_text(output, encoding='utf-8')
                return subprocess.CompletedProcess(args, exit_code, output, '')

            with patch('modport.handlers._forge_baseline_init', return_value='/workspace/forge-init.gradle'), \
                    patch('modport.handlers._sandboxed_build_command',
                          side_effect=lambda _root, _worktree, gradle, **_kwargs: gradle), \
                    patch('modport.handlers._contract_asset_cache_context', return_value=None), \
                    patch('modport.handlers._exec', side_effect=execute) as run_gradle:
                result = BaselineContractVerificationHandler()(command)
            self.assertEqual(2, run_gradle.call_count)
            self.assertIn('test', run_gradle.call_args.args[0])
            self.assertEqual('failed', result.status)
            self.assertEqual('baseline_contract_failed', result.error_code)
            self.assertEqual('unverified', result.outputs['acceptance_status'])
            self.assertTrue(result.outputs['process_executed'])
            self.assertEqual(1, result.outputs['exit_code'])
            self.assertEqual('harness_compile', result.outputs['diagnostics']['category'])
            ref = result.outputs['artifact_refs']['baseline_contract_tests_candidate']
            report = json.loads((root / ref['path']).read_text(encoding='utf-8'))
            self.assertEqual(ref['sha256'], file_digest(root / ref['path']))
            self.assertEqual(1, report['exit_code'])

    def test_v27_compile_package_excludes_test_tasks_and_seals_target_jar(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            self._write_contract_fixture(root)
            baseline = root / 'baseline'
            baseline.mkdir(exist_ok=True)
            (baseline / 'gradlew').write_text('#!/bin/sh\n', encoding='utf-8')
            command = self._command(root, 'baseline_build')
            command = replace(command, options={**command.options, 'workflow_version': 27,
                'validation_policy': {'scope': 'compile_package'}})

            def passthrough(_root, _worktree, gradle, **_kwargs):
                return gradle

            with patch('modport.handlers._sandboxed_build_command', side_effect=passthrough), \
                    patch('modport.handlers._exec', return_value=subprocess.CompletedProcess([], 0, 'BUILD SUCCESSFUL')) as execute:
                build = GradleHandler(baseline=True, tasks=('clean', 'build', 'runGameTestServer'),
                                      name='baseline-build')(command)
            gradle_args = execute.call_args.args[0]
            self.assertEqual('completed', build.status)
            self.assertIn('build', gradle_args)
            self.assertEqual('runGameTestServer', gradle_args[-1])
            self.assertNotIn('-x', gradle_args)
            self.assertTrue(build.outputs['process_executed'])

            v28_build_command = replace(command, options={**command.options,
                'workflow_version': 28})
            with patch('modport.handlers._sandboxed_build_command', side_effect=passthrough), \
                    patch('modport.handlers._exec',
                          return_value=subprocess.CompletedProcess([], 0, 'BUILD SUCCESSFUL')) as execute:
                v28_baseline = GradleHandler(baseline=True,
                    tasks=('clean', 'build', 'runGameTestServer'), name='baseline-build')(
                        v28_build_command)
            self.assertEqual('completed', v28_baseline.status)
            self.assertEqual(['clean', 'build'], v28_baseline.outputs['gradle_tasks'])
            self.assertIn(['-x', 'test'], [execute.call_args.args[0][i:i + 2]
                for i in range(len(execute.call_args.args[0]) - 1)])

            worktree_root = root / 'worktree'
            worktree_root.mkdir(parents=True)
            (worktree_root / 'gradlew').write_text('#!/bin/sh\n', encoding='utf-8')
            target_command = replace(command, stage_id='target_build', task_id='target_build',
                                     command_id='target-build-gradle')
            with patch('modport.handlers._sandboxed_build_command', side_effect=passthrough), \
                    patch('modport.handlers._exec',
                          return_value=subprocess.CompletedProcess([], 0, 'BUILD SUCCESSFUL')) as execute:
                target_gradle = GradleHandler(baseline=False,
                    tasks=('clean', 'build', 'runGameTestServer'), name='target-build')(target_command)
            target_args = execute.call_args.args[0]
            self.assertEqual('completed', target_gradle.status)
            self.assertEqual(['clean', 'build'], target_gradle.outputs['gradle_tasks'])
            self.assertEqual(['check', 'test', 'runGameTestServer'],
                             target_gradle.outputs['excluded_gradle_tasks'])
            for excluded in ('check', 'test', 'runGameTestServer'):
                self.assertIn(['-x', excluded], [target_args[i:i + 2]
                                                  for i in range(len(target_args) - 1)])

            v28_target_command = replace(target_command,
                options={**target_command.options, 'workflow_version': 28})
            with patch('modport.handlers._sandboxed_build_command', side_effect=passthrough), \
                    patch('modport.handlers._exec',
                          return_value=subprocess.CompletedProcess([], 0, 'BUILD SUCCESSFUL')):
                v28_target = GradleHandler(baseline=False,
                    tasks=('clean', 'runData', 'build', 'runGameTestServer'),
                    name='target-build')(v28_target_command)
            self.assertEqual(['clean', 'build'], v28_target.outputs['gradle_tasks'])

            from modport.handlers import BuildAndBehaviorHandler
            worktree = root / 'worktree' / 'build' / 'libs'
            worktree.mkdir(parents=True)
            jar = worktree / 'example.jar'
            jar.write_bytes(b'fresh packaged mod')
            target = replace(command, stage_id='target_build', command_id='target-build-exec')
            build_result = OperationResult('completed', target.run_id, target.task_id,
                target.stage_id, target.command_id, outputs={
                    'process_executed': True,
                    'gradle_tasks': ['clean', 'runData', 'build'],
                    'excluded_gradle_tasks': ['check', 'test', 'runGameTestServer'],
                })
            verification = Mock()
            combined = BuildAndBehaviorHandler(Mock(return_value=build_result), verification)(target)
            self.assertEqual('completed', combined.status)
            self.assertEqual('deferred_by_user', combined.outputs['verification_status'])
            self.assertFalse(combined.outputs['verification_executed'])
            self.assertEqual(1, len(combined.outputs['package_receipt']['artifacts']))
            self.assertIn('target_package_receipt', combined.outputs['artifact_refs'])
            verification.assert_not_called()

            from modport.handlers import DeliveryHandler
            (root / 'baseline').mkdir(exist_ok=True)
            (root / 'artifacts/source.json').write_text(
                canonical_json({'source_commit': 'immutable-source-commit'}) + '\n',
                encoding='utf-8')
            delivery = self._command(root, 'delivery')
            delivery = replace(delivery, options={**delivery.options,
                'workflow_version': 27, 'validation_policy': {'scope': 'compile_package'}},
                upstream_results={
                    'target_build': combined.to_dict(),
                    'contract_verify': OperationResult('completed', delivery.run_id,
                        'contract_verify', 'contract_verify', 'baseline-contract-exec',
                        outputs={
                            'process_executed': True,
                            'exit_code': 0,
                            'executor_provenance': {'game_test.behavior_1': {}},
                            'evidence_records': {'game_test.behavior_1': {}},
                            'record_errors': [],
                            'no_source_tasks': [],
                        }).to_dict(),
                })

            def git_result(args, *, cwd, **_kwargs):
                if args[1] == 'status':
                    return subprocess.CompletedProcess(args, 0, '')
                if args[1:3] == ['rev-parse', 'HEAD']:
                    commit = ('immutable-source-commit' if Path(cwd).name == 'baseline'
                              else 'migrated-commit')
                    return subprocess.CompletedProcess(args, 0, commit + '\n')
                if args[1:3] == ['merge-base', '--is-ancestor']:
                    return subprocess.CompletedProcess(args, 0, '')
                if args[1:3] == ['rev-list', '--count']:
                    return subprocess.CompletedProcess(args, 0, '1\n')
                raise AssertionError(args)

            with patch('modport.handlers._exec', side_effect=git_result):
                delivered = DeliveryHandler()(delivery)
            self.assertEqual('completed', delivered.status, delivered.detail)
            self.assertEqual('passed', delivered.outputs['required_checks'][
                'source_baseline_behavior_tests']['status'])
            self.assertEqual('passed', delivered.outputs['required_checks']['target_compile']['status'])
            self.assertEqual('passed', delivered.outputs['required_checks']['target_package']['status'])

            missing_baseline = replace(delivery, command_id='delivery-missing-baseline',
                upstream_results={'target_build': combined.to_dict()})
            with patch('modport.handlers._exec', side_effect=git_result):
                rejected_baseline = DeliveryHandler()(missing_baseline)
            self.assertEqual('failed', rejected_baseline.status)
            self.assertEqual('failed', rejected_baseline.outputs['required_checks'][
                'source_baseline_behavior_tests']['status'])

            v28_verifier = replace(command, stage_id='contract_verify', task_id='contract_verify',
                command_id='v28-identity-only', options={**command.options, 'workflow_version': 28})
            with patch('modport.handlers._exec') as run_gradle:
                deferred = BaselineContractVerificationHandler()(v28_verifier)
            run_gradle.assert_not_called()
            self.assertEqual('completed', deferred.status)
            self.assertFalse(deferred.outputs['process_executed'])
            self.assertEqual('deferred_by_user', deferred.outputs['behavior_tests_status'])

            target_commit = 'b' * 40

            def git_result_v28(args, *, cwd, **_kwargs):
                if args[1:3] == ['rev-parse', 'HEAD'] and Path(cwd).name == 'worktree':
                    return subprocess.CompletedProcess(args, 0, target_commit + '\n')
                return git_result(args, cwd=cwd)

            from modport.handlers import _target_package_receipt
            v28_target_command = replace(target, options={**target.options,
                'workflow_version': 28})
            with patch('modport.handlers._exec', side_effect=git_result_v28):
                receipt, receipt_ref = _target_package_receipt(v28_target_command)
            self.assertEqual('passed', receipt['status'])
            self.assertEqual(target_commit, receipt['target_commit'])
            v28_outputs = {**combined.outputs,
                'package_receipt': receipt,
                'verification_candidate_id': 'host-candidate-id',
                'verification_binding': 'host_observed_clean_candidate',
                'artifact_refs': {**combined.outputs['artifact_refs'],
                                  'target_package_receipt': receipt_ref}}
            v28_target_result = replace(combined, outputs=v28_outputs)
            v28_delivery = replace(missing_baseline, command_id='v28-delivery',
                options={**delivery.options, 'workflow_version': 28},
                upstream_results={'target_build': v28_target_result.to_dict()})
            with patch('modport.handlers._exec', side_effect=git_result_v28):
                delivered_v28 = DeliveryHandler()(v28_delivery)
            self.assertEqual('completed', delivered_v28.status, delivered_v28.detail)
            self.assertNotIn('source_baseline_behavior_tests', delivered_v28.outputs['required_checks'])
            self.assertIn('source_baseline_behavior_tests', delivered_v28.outputs['deferred_checks'])
            self.assertEqual(target_commit,
                delivered_v28.outputs['required_checks']['target_package']['target_commit'])

            def changed_head(args, *, cwd, **kwargs):
                if args[1:3] == ['rev-parse', 'HEAD'] and Path(cwd).name == 'worktree':
                    return subprocess.CompletedProcess(args, 0, 'c' * 40 + '\n')
                return git_result_v28(args, cwd=cwd, **kwargs)

            with patch('modport.handlers._exec', side_effect=changed_head):
                changed_delivery = DeliveryHandler()(
                    replace(v28_delivery, command_id='v28-changed-commit'))
            self.assertEqual('failed', changed_delivery.status)
            self.assertEqual('failed', changed_delivery.outputs['required_checks']['target_package']['status'])

            unbound_target = replace(v28_target_result,
                outputs={**v28_outputs, 'verification_candidate_id': None})
            with patch('modport.handlers._exec', side_effect=git_result_v28):
                unbound_delivery = DeliveryHandler()(replace(v28_delivery,
                    command_id='v28-unbound-candidate',
                    upstream_results={'target_build': unbound_target.to_dict()}))
            self.assertEqual('failed', unbound_delivery.status)
            self.assertEqual('failed', unbound_delivery.outputs['required_checks']['target_package']['status'])

            jar.write_bytes(b'tampered after the package receipt')
            delivery = replace(delivery, command_id='delivery-after-tamper')
            with patch('modport.handlers._exec', side_effect=git_result):
                rejected = DeliveryHandler()(delivery)
            self.assertEqual('failed', rejected.status)
            self.assertEqual('failed', rejected.outputs['required_checks']['target_package']['status'])

    def test_v21_entry_probe_includes_and_archives_additive_harness_sources(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            self._write_contract_fixture(root)
            harness = root / 'baseline/.modport/harness'
            harness.mkdir()
            (harness / 'Smoke.java').write_text('class Smoke {}')
            authored = harness.parent / 'characterization.init.gradle'
            authored.write_text('// launch flags without source registration')
            command = self._command(root, 'contract_verify')
            command = replace(command, options={**command.options, 'workflow_version': 21})
            def execute(args, **kwargs):
                self.assertIn('/workspace/.modport/characterization.init.gradle', args)
                self.assertIn('/modport-wiring/characterization-sources.init.gradle', args)
                self.assertIn('--dry-run', args)
                return subprocess.CompletedProcess(args, 1, 'stop fixture before full launch')
            with patch('modport.handlers._exec', side_effect=execute):
                result = BaselineContractVerificationHandler()(command)
            refs = result.outputs['artifact_refs']
            self.assertIn('baseline_harness_wiring', refs)
            self.assertIn('baseline_harness_wiring:1', refs)
            self.assertEqual('// launch flags without source registration', authored.read_text())
            self.assertFalse((authored.parent / 'characterization-sources.init.gradle').exists())

    def test_constant_runtime_shaped_evidence_fails_fresh_nonce_binding(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            self._write_contract_fixture(root)
            command = self._command(root, "contract_review_freeze")

            def execute(_args, *, log, **_kwargs):
                evidence = root / "baseline" / ".modport" / "evidence" / "test-results.json"
                evidence.write_text(
                    canonical_json(
                        {
                            "test_id": "game_test.behavior_1",
                            "evidence_kind": "runtime",
                            "executor": "gametest",
                            "source_fingerprint": "immutable-source-commit",
                            "execution_nonce": "hard-coded",
                            "execution_inputs": ["Forge baseline"],
                            "runtime_operations": ["start server", "invoke behavior", "observe state"],
                            "observations": {"assertion": "constant"},
                            "status": "passed",
                        }
                    )
                    + "\n",
                    encoding="utf-8",
                )
                log.write_text("executed\n", encoding="utf-8")
                return subprocess.CompletedProcess([], 0, "BUILD SUCCESSFUL")

            with patch("modport.handlers._exec", side_effect=execute):
                result = BaselineContractVerificationHandler()(command)
            self.assertEqual(result.status, "failed")
            self.assertEqual(result.error_code, "baseline_contract_failed")
            self.assertTrue(
                any("not verifier-bound" in error for error in result.outputs["record_errors"])
            )

    def test_fresh_nonce_without_operation_witnesses_is_rejected(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            self._write_contract_fixture(root)
            command = self._command(root, "contract_review_freeze")

            def execute(args, *, log, **_kwargs):
                nonce = args[args.index("MODPORT_EVIDENCE_NONCE") + 1]
                fingerprints = json.loads(
                    args[args.index("MODPORT_EXECUTOR_FINGERPRINTS") + 1]
                )
                evidence = root / "baseline" / ".modport" / "evidence" / "test-results.json"
                evidence.write_text(
                    canonical_json(
                        {
                            "test_id": "game_test.behavior_1",
                            "evidence_kind": "runtime",
                            "executor": "gametest",
                            "executor_fingerprint": fingerprints["game_test.behavior_1"],
                            "source_fingerprint": "immutable-source-commit",
                            "execution_nonce": nonce,
                            "execution_inputs": ["Forge baseline"],
                            "runtime_operations": ["start server", "invoke behavior", "observe state"],
                            "observations": {"assertion": "fabricated"},
                            "status": "passed",
                        }
                    )
                    + "\n",
                    encoding="utf-8",
                )
                log.write_text("executed\n", encoding="utf-8")
                return subprocess.CompletedProcess([], 0, "BUILD SUCCESSFUL")

            with patch("modport.handlers._exec", side_effect=execute):
                result = BaselineContractVerificationHandler()(command)
            self.assertEqual(result.status, "failed")
            self.assertEqual(result.error_code, "baseline_contract_failed")
            self.assertTrue(
                any(
                    "operation-level witnesses" in error
                    for error in result.outputs["record_errors"]
                )
            )

    def test_runtime_witnesses_need_no_file_hash_or_digest_marker(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            self._write_contract_fixture(root)
            command = self._command(root, "contract_review_freeze")

            def execute(args, *, log, **_kwargs):
                nonce = args[args.index("MODPORT_EVIDENCE_NONCE") + 1]
                operations = ["start server", "invoke behavior", "observe state"]
                witnesses = [
                    {
                        "operation": operation,
                        "event_index": index,
                        "invocation": f"runtime-event-{index}",
                        "observations": {"observed": True, "index": index},
                        "execution_nonce": nonce,
                    }
                    for index, operation in enumerate(operations)
                ]
                record = {
                    "test_id": "game_test.behavior_1",
                    "evidence_kind": "runtime",
                    "executor": "gametest",
                    "source_fingerprint": "immutable-source-commit",
                    "execution_nonce": nonce,
                    "execution_inputs": ["Forge baseline"],
                    "runtime_operations": operations,
                    "runtime_witnesses": witnesses,
                    "observations": {"assertion": "runtime result"},
                    "status": "passed",
                }
                evidence = root / "baseline" / ".modport" / "evidence" / "test-results.json"
                evidence.write_text(canonical_json(record) + "\n", encoding="utf-8")
                marker = f"MODPORT_RUNTIME_WITNESS {nonce} {record['test_id']}"
                log.write_text(marker + "\n", encoding="utf-8")
                return subprocess.CompletedProcess([], 0, marker + "\nBUILD SUCCESSFUL\n")

            with patch("modport.handlers._exec", side_effect=execute):
                result = BaselineContractVerificationHandler()(command)
            self.assertEqual(result.status, "completed", result.detail)
            self.assertEqual(result.outputs["record_errors"], [])
            self.assertEqual(
                result.outputs["evidence_records"]["game_test.behavior_1"][
                    "runtime_witness_count"
                ],
                3,
            )

    def test_acceptance_checks_actual_versions_without_content_hash_gates(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            self._write_contract_fixture(root)
            command = self._command(root, "contract_review_freeze")
            self.assertEqual(FreezeContractHandler()(command).status, "completed")
            manifest_path = root / "artifacts" / "locked-manifest.json"
            locked = LockedManifest.from_mapping(json.loads(manifest_path.read_text(encoding="utf-8")))
            contract_lock = json.loads((root / "artifacts" / "functional-contract.lock.json").read_text(encoding="utf-8"))
            anchors = {"manifest_sha256": locked.manifest_sha256, "contract_lock_sha256": contract_lock["lock_sha256"]}
            acceptance_command = self._command(root, "acceptance", payload={"locked_artifacts": anchors})
            worktree = root / "worktree"
            worktree.mkdir()
            (worktree / "gradle.properties").write_text("minecraft_version=26.1.2\nneo_version=26.1.2.101\n", encoding="utf-8")
            (worktree / "build.gradle").write_text("java.toolchain.languageVersion = JavaLanguageVersion.of(25)\n", encoding="utf-8")

            acceptance = AcceptancePreflightHandler()
            self.assertEqual(acceptance(acceptance_command).status, "completed")

            manifest_data = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest_data["neoforge_version"] = "26.1.2.102"
            manifest_path.write_text(canonical_json(manifest_data) + "\n", encoding="utf-8")
            tampered_manifest = acceptance(acceptance_command)
            self.assertEqual(tampered_manifest.status, "failed")
            self.assertEqual(tampered_manifest.error_code, "target_toolchain_mismatch")

            manifest_path.write_text(canonical_json(locked.to_dict(include_hash=True)) + "\n", encoding="utf-8")
            contract_path = root / "artifacts" / "functional-contract.lock.json"
            contract_data = json.loads(contract_path.read_text(encoding="utf-8"))
            contract_data["contract"]["entries"][0]["notes"] = "clarified evidence description"
            contract_data["contract"]["behaviors"][0]["notes"] = "clarified evidence description"
            contract_path.write_text(canonical_json(contract_data) + "\n", encoding="utf-8")
            tampered_contract = acceptance(acceptance_command)
            self.assertEqual(tampered_contract.status, "completed", tampered_contract.detail)

    def test_previous_required_output_cannot_satisfy_new_attempt(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            output = root / "worktree/.modport/migration-plan.md"
            output.parent.mkdir(parents=True)
            output.write_text("stale plan")
            command = self._command(root, "migration_plan")
            with patch("modport.handlers._exec", return_value=subprocess.CompletedProcess([], 0, "")):
                result = CodexStageHandler("plan", required_paths=(".modport/migration-plan.md",))(command)
            self.assertEqual(result.error_code, "agent_output_missing")
            previous = root / "artifacts/executions/command-1/previous-outputs/.modport/migration-plan.md"
            self.assertEqual(previous.read_text(), "stale plan")

    def test_v17_missing_requested_output_is_diagnostic_not_a_stage_gate(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            (root / "worktree").mkdir()
            command = self._command(root, "migration_plan")
            command = replace(command, options={**command.options, "workflow_version": 17})
            with patch("modport.handlers._exec", return_value=subprocess.CompletedProcess([], 0, "")):
                result = CodexStageHandler("plan", required_paths=(".modport/migration-plan.md",))(command)
            self.assertEqual(result.status, "completed", result.detail)
            self.assertEqual(result.outputs["acceptance_status"], "unverified")
            self.assertEqual(result.outputs["missing_required_paths"], [".modport/migration-plan.md"])

    def test_v17_approval_wrapper_runs_candidate_and_composite_runs_both_checks(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            command = self._command(root, "acceptance_build")
            command = replace(command, options={**command.options, "workflow_version": 17})
            candidate = Mock(return_value=_result(command, "completed", outputs={"candidate": True}))
            self.assertEqual(ApprovedCandidateHandler(candidate)(command).status, "completed")
            candidate.assert_called_once_with(command)

            build = Mock(return_value=_result(command, "failed", outputs={"build": "failed"},
                                               detail="compile failed", error_code="gradle_failed"))
            verify = Mock(return_value=_result(command, "failed", outputs={"tests": "failed"},
                                                detail="tests failed", error_code="target_contract_failed"))
            result = BuildAndBehaviorHandler(build, verify)(command)
            build.assert_called_once_with(command)
            verify.assert_called_once_with(command)
            self.assertEqual(result.status, "failed")
            self.assertEqual(result.error_code, verify.return_value.error_code)
            self.assertEqual(result.outputs["acceptance_status"], "unverified")
            self.assertEqual(result.outputs["build_status"], "failed")
            self.assertEqual(result.outputs["verification_status"], "failed")
            self.assertEqual(result.outputs["build"], "failed")
            self.assertEqual(result.outputs["tests"], "failed")

    def test_stage_prompt_uses_verified_shared_rule_references(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            (root / "worktree").mkdir()
            command = self._command(root, "implementation")
            with patch("modport.handlers._exec", return_value=subprocess.CompletedProcess([], 0, "")) as execute:
                result = CodexStageHandler("implement")(command)
            self.assertEqual(result.status, "completed")
            prompt = execute.call_args.kwargs['input_text']
            self.assertNotIn(canonical_json(acceptance_rubric()), prompt)
            self.assertIn(command.artifact_refs["agent_rules"]["sha256"], prompt)
            self.assertIn("artifacts/executions/command-1/input.json", prompt)
            self.assertFalse((root / "artifacts/budget.json").exists())

    def test_independent_review_is_archived_and_rejection_is_business_result(self):
        for verdict in ("approved", "rejected"):
            with self.subTest(verdict=verdict), tempfile.TemporaryDirectory() as raw:
                root = Path(raw)
                worktree = root / "worktree"
                (worktree / ".modport").mkdir(parents=True)
                command = self._command(root, "code_review")
                rubric = acceptance_rubric()
                def execute(args, **kwargs):
                    (worktree / ".modport/code-review.json").write_text(canonical_json({
                        "reviewer_id": "code-review-agent", "review_id": "review-1",
                        "verdict": verdict, "findings": [],
                        **{key: rubric[key] for key in ("rubric_id", "rubric_version", "rubric_sha256")},
                    }))
                    return subprocess.CompletedProcess(args, 0, "")
                with patch("modport.handlers._exec", side_effect=execute):
                    result = ReviewHandler(baseline=False)(command)
                self.assertEqual(result.status, "completed", result.detail)
                self.assertEqual(result.outputs["verdict"], verdict)
                self.assertIn("code_review", result.outputs["artifact_refs"])
                self.assertFalse((worktree / ".modport/code-review.json").exists())

    def test_independent_review_requires_report_even_when_source_changes(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            worktree = root / "worktree"
            worktree.mkdir()
            source = worktree / "Example.java"
            source.write_text("original")
            command = self._command(root, "code_review")
            def execute(args, **kwargs):
                source.write_text("reviewer changed the implementation")
                return subprocess.CompletedProcess(args, 0, "")
            with patch("modport.handlers._exec", side_effect=execute):
                result = ReviewHandler(baseline=False)(command)
            self.assertEqual(result.error_code, "agent_output_missing")

    def test_reworked_code_cannot_reuse_old_build_approval(self):
        from modport.rework_tools import session_directory
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            worktree = root / 'worktree'
            (worktree / '.modport').mkdir(parents=True)
            command = self._command(root, 'code_review')
            response = session_directory(root, command.command_id) / 'responses/request.json'
            response.parent.mkdir(parents=True)
            response.write_text(json.dumps({'request_id': 'request', 'status': 'failed', 'updates': [
                {'stage': 'coder', 'target_agent': 'coder-a', 'result': {'status': 'completed', 'outputs': {}}}]}))
            def execute(args, **kwargs):
                (worktree / '.modport/code-review.json').write_text('MODPORT_DECISION: approved')
                return subprocess.CompletedProcess(args, 0, '')
            with patch('modport.handlers._exec', side_effect=execute):
                result = ReviewHandler(baseline=False)(command)
            self.assertEqual('review_invalid', result.error_code)
            self.assertIn('missing fresh target_build', result.detail)

    def test_contract_review_requires_fresh_baseline_and_independent_identity(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            self._write_contract_fixture(root)
            command = self._command(root, "contract_review")
            path = root / "baseline/.modport/contract-review.json"
            review = json.loads(path.read_text())
            review["review_id"] = "fresh-review"
            review["findings"] = []
            def execute(args, **kwargs):
                path.write_text(canonical_json(review))
                return subprocess.CompletedProcess(args, 0, "")
            with patch("modport.handlers._exec", side_effect=execute), patch("modport.handlers._baseline_changes_are_isolated", return_value=(True, [])):
                result = ReviewHandler(baseline=True)(command)
            self.assertEqual(result.status, "completed", result.detail)
            self.assertEqual(result.outputs["verdict"], "approved")
            candidate = root / "baseline/.modport/functional-contract.json"
            candidate.write_text(candidate.read_text() + " ")
            with patch("modport.handlers._exec", side_effect=execute) as rerun, patch("modport.handlers._baseline_changes_are_isolated", return_value=(True, [])):
                refreshed = ReviewHandler(baseline=True)(command)
            self.assertEqual(refreshed.status, "completed", refreshed.detail)
            rerun.assert_called_once()

    def test_acceptance_uses_review_verdict_without_candidate_digest(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            command = self._command(root, "acceptance_build")
            rubric = acceptance_rubric()
            path = root / "artifacts/code-review.json"
            path.write_text(canonical_json({"verdict": "approved", "reviewer_id": "code-review-agent", "candidate_sha256": "old", **{key: rubric[key] for key in ("rubric_id", "rubric_version", "rubric_sha256")}}))
            command = replace(command, artifact_refs={**command.artifact_refs, "code_review": {"path": "artifacts/code-review.json", "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}})
            operation = Mock(return_value=_result(command, "completed"))
            result = ApprovedCandidateHandler(operation)(command)
            self.assertEqual(result.status, "completed", result.detail)
            operation.assert_called_once_with(command)
            rejected = json.loads(path.read_text())
            rejected["verdict"] = "rejected"
            path.write_text(canonical_json(rejected))
            operation.reset_mock()
            result = ApprovedCandidateHandler(operation)(command)
            self.assertEqual(result.error_code, "code_review_stale")
            operation.assert_not_called()

    def _target_contract_fixture(self, root, *, executor="gametest"):
        self._write_contract_fixture(root, executor=executor)
        command = self._command(root, "contract_freeze")
        frozen = FreezeContractHandler()(command)
        self.assertEqual(frozen.status, "completed", frozen.detail)
        worktree = root / "worktree"
        shutil.copytree(root / "baseline/.modport/tests", worktree / ".modport/tests")
        (worktree / ".modport/evidence").mkdir()
        # A target-local contract must never replace the frozen declaration.
        (worktree / ".modport/functional-contract.json").write_text('{"test_evidence":{}}')
        (worktree / ".modport/tests/BehaviorTest.java").write_text("final class BehaviorTest { void targetRuntimeBehavior() {} }")
        (worktree / ".modport/characterization.init.gradle").write_text("// target harness wiring")
        return self._command(root, "target_build", payload={"locked_artifacts": frozen.outputs["locked_artifacts"]})

    def test_target_behavior_verification_uses_frozen_declarations_and_fresh_witnesses(self):
        for mode in ("valid", "wrong_nonce", "missing_witnesses", "candidate_changed", "invalid_json", "empty_record", "timeout", "runtime_outputs", "smoke_valid", "audio_only", "diagnostic_only"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as raw:
                root = Path(raw)
                client = mode in {"smoke_valid", "audio_only", "diagnostic_only"}
                executor = "client_smoke" if client else "gametest"
                command = self._target_contract_fixture(root, executor=executor)
                if client:
                    from modport.client_harness import client_harness_support_files
                    refs = dict(command.artifact_refs)
                    for relative, contents in client_harness_support_files().items():
                        path = root / "artifacts/harness-support" / relative
                        path.parent.mkdir(parents=True, exist_ok=True)
                        path.write_text(contents)
                        refs["harness_support:" + relative] = {"path": path.relative_to(root).as_posix(),
                            "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
                    command = replace(command, stage_id="client_smoke", artifact_refs=refs)
                baseline_report = (root / "artifacts/baseline-contract-tests.json").read_bytes()
                seen_args = []
                def execute(args, *, log, **kwargs):
                    seen_args.extend(args)
                    nonce = args[args.index("MODPORT_EVIDENCE_NONCE") + 1]
                    fingerprint = json.loads(args[args.index("MODPORT_EXECUTOR_FINGERPRINTS") + 1])["game_test.behavior_1"]
                    operations = ["start server", "invoke behavior", "observe state"]
                    witnesses = [{"operation": operation, "event_index": index,
                                  "invocation": "target-event-" + str(index), "observations": {"observed": True},
                                  "execution_nonce": nonce} for index, operation in enumerate(operations)]
                    record = {"test_id": "game_test.behavior_1", "evidence_kind": "runtime", "executor": executor,
                              "executor_fingerprint": fingerprint, "source_fingerprint": "immutable-source-commit",
                              "execution_nonce": "old-nonce" if mode == "wrong_nonce" else nonce,
                              "execution_inputs": ["NeoForge target"], "runtime_operations": operations,
                              "runtime_witnesses": [] if mode == "missing_witnesses" else witnesses,
                              "observations": {"assertion": "target behavior preserved"}, "status": "passed"}
                    raw_record = "{malformed" if mode == "invalid_json" else ("" if mode == "empty_record" else canonical_json(record))
                    (root / "worktree/.modport/evidence/test-results.json").write_text(raw_record)
                    witness_sha = hashlib.sha256(canonical_json({"test_id": record["test_id"], "execution_nonce": nonce,
                        "executor_fingerprint": fingerprint, "runtime_witnesses": witnesses}).encode()).hexdigest()
                    marker = f"MODPORT_RUNTIME_WITNESS {nonce} {record['test_id']} {witness_sha}"
                    if mode == "candidate_changed":
                        (root / "worktree/changed.java").write_text("unexpected edit")
                    if mode == "runtime_outputs":
                        for relative in (".modport/run-client/options.txt", ".modport/harness/build/classes/Test.class"):
                            generated = root / "worktree" / relative
                            generated.parent.mkdir(parents=True, exist_ok=True)
                            generated.write_text("generated during the test")
                    if mode in {"audio_only", "diagnostic_only"}:
                        (root / "worktree/.modport/evidence/test-results.json").unlink()
                        marker = ("OpenAL initialized; Sound engine started; Narrator library example" if mode == "audio_only"
                                  else 'MODPORT_CLIENT_STATE {"state":"client_ready","acceptance_evidence":false}')
                    log.write_text(marker)
                    if mode == "timeout":
                        raise subprocess.TimeoutExpired(args, 1, output=marker.encode())
                    return subprocess.CompletedProcess(args, 0, marker + "\nBUILD SUCCESSFUL")
                with patch("modport.handlers._exec", side_effect=execute):
                    handler = ClientSmokeHandler() if client else BaselineContractVerificationHandler(baseline=False)
                    result = handler(command)
                if mode in {"audio_only", "diagnostic_only"}:
                    self.assertEqual(result.status, "failed")
                    self.assertEqual(result.outputs["evidence_records"], {})
                    self.assertTrue(result.outputs["record_errors"])
                    continue
                if client:
                    self.assertIn("/modport-support/launch.py", seen_args)
                self.assertEqual((root / "artifacts/baseline-contract-tests.json").read_bytes(), baseline_report)
                self.assertIn("/workspace/.modport/characterization.init.gradle", seen_args)
                self.assertFalse(any("forge-baseline" in arg for arg in seen_args))
                self.assertIn("--rerun-tasks", seen_args)
                self.assertIn("target_contract_tests_candidate", result.outputs["artifact_refs"])
                refs = result.outputs["artifact_refs"]
                archived = root / refs["target_runtime_evidence:game_test.behavior_1"]["path"]
                original_bytes = (root / "worktree/.modport/evidence/test-results.json").read_bytes()
                self.assertEqual(archived.read_bytes(), original_bytes)
                if mode not in {"invalid_json", "empty_record"}:
                    record = json.loads(archived.read_text())
                    self.assertIn("observations", record)
                    self.assertEqual(len(record["runtime_witnesses"]), 0 if mode == "missing_witnesses" else 3)
                executor_archive = root / refs["target_executor_source:game_test.behavior_1:.modport/tests/BehaviorTest.java"]["path"]
                self.assertIn("targetRuntimeBehavior", executor_archive.read_text())
                (root / "worktree/.modport/evidence/test-results.json").write_text("next attempt overwrites evidence")
                (root / "worktree/.modport/tests/BehaviorTest.java").write_text("next attempt edits tests")
                self.assertEqual(archived.read_bytes(), original_bytes)
                self.assertIn("targetRuntimeBehavior", executor_archive.read_text())
                self.assertEqual(result.outputs["tasks"], ["test"])
                self.assertNotIn("candidate_sha256", result.outputs)
                if mode in {"valid", "candidate_changed", "runtime_outputs", "smoke_valid"}:
                    self.assertEqual(result.status, "completed", result.detail)
                    self.assertEqual(result.outputs["record_errors"], [])
                    self.assertEqual(result.outputs["evidence_records"]["game_test.behavior_1"]["runtime_witness_count"], 3)
                else:
                    self.assertEqual(result.error_code, "target_contract_timeout" if mode == "timeout" else "target_contract_failed")
                    if mode != "timeout":
                        self.assertTrue(result.outputs["record_errors"])

    def test_client_gate_rejects_contract_without_executable_startup_mapping(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            command = self._target_contract_fixture(root)
            with patch("modport.handlers._exec") as execute:
                result = ClientSmokeHandler()(command)
            self.assertEqual(result.error_code, "client_mapping_missing")
            execute.assert_not_called()
            baseline = self._command(root, "contract_verify")
            with patch("modport.handlers._exec") as execute:
                result = BaselineContractVerificationHandler(require_client_evidence=True)(baseline)
            self.assertEqual(result.error_code, "client_mapping_missing")
            execute.assert_not_called()

    def test_target_behavior_missing_harness_fails_before_gradle(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            command = self._target_contract_fixture(root)
            (root / "worktree/.modport/tests/BehaviorTest.java").unlink()
            with patch("modport.handlers._exec") as execute:
                result = BaselineContractVerificationHandler(baseline=False)(command)
            self.assertEqual(result.error_code, "target_evidence_invalid")
            execute.assert_not_called()

    def test_build_and_behavior_preserve_both_results_artifact_refs(self):
        with tempfile.TemporaryDirectory() as raw:
            command = self._command(Path(raw), "target_build")
            build = Mock(return_value=self._stage_result(command, "completed", outputs={"artifact_refs": {"build_log": {"sha256": "build"}}}))
            verify = Mock(return_value=OperationResult(status="failed", outputs={"artifact_refs": {"behavior_report": {"sha256": "behavior"}}}, error_code="target_contract_failed"))
            result = BuildAndBehaviorHandler(build, verify)(command)
            self.assertEqual(result.status, "failed")
            self.assertEqual(set(result.outputs["artifact_refs"]), {"build_log", "behavior_report"})
            build.assert_called_once_with(command)
            verify.assert_called_once_with(command)

    def test_exec_timeout_keeps_child_output_as_text(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            log = root / "timeout.log"
            with self.assertRaises(subprocess.TimeoutExpired):
                _exec([sys.executable, "-c", "import time; print('diagnostic before timeout', flush=True); time.sleep(5)"],
                      cwd=root, log=log, timeout=0.15)
            text = log.read_text()
            self.assertIn("diagnostic before timeout", text)
            self.assertIn("TIMEOUT after", text)

    def test_exec_passes_workspace_lock_to_direct_child(self):
        from modport.evidence import current_lock_fds, workspace_lock
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            with workspace_lock(root):
                fd = current_lock_fds()[-1]
                result = _exec([sys.executable, "-c", "import os, sys; os.fstat(int(sys.argv[1])); print('inherited')", str(fd)],
                               cwd=root, log=root / "inherited.log", timeout=2)
            self.assertEqual(result.returncode, 0, result.stdout)
            self.assertIn("inherited", result.stdout)

    def test_target_verifier_does_not_archive_symlink_evidence(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            command = self._target_contract_fixture(root)
            outside = root / "outside.json"
            outside.write_text("private bytes")
            (root / "worktree/.modport/evidence/test-results.json").symlink_to(outside)
            with patch("modport.handlers._exec") as execute:
                result = BaselineContractVerificationHandler(baseline=False)(command)
            self.assertEqual(result.error_code, "target_evidence_invalid")
            self.assertNotIn("target_runtime_evidence:game_test.behavior_1", result.outputs.get("artifact_refs", {}))
            execute.assert_not_called()


if __name__ == "__main__":
    unittest.main()
