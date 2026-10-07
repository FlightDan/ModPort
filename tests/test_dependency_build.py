"""Frozen dependency seeds stay isolated across runs and build phases."""
from hashlib import sha256
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from modport.cli import _request, parser
from modport.dependency_build import dependency_mounts, prepare_dependency_seed, gradle_failure_kind
from modport.dependency_cache import publish_artifact
from modport.handlers import _sandboxed_build_command


class DependencyBuildTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.store = self.root / "shared"
        self.source = self.root / "original.jar"
        self.source.write_bytes(b"original pinned jar bytes")
        self.record = publish_artifact(self.store, "test.group:library:1.0", self.source,
            sha256(self.source.read_bytes()).hexdigest(), "https://mirror.example/library.jar",
            no_transitive_dependencies=True)

    def run_seed(self, name):
        root = self.root / name
        (root / "artifacts").mkdir(parents=True)
        refs = prepare_dependency_seed(self.store, root)
        (root / "run.json").write_text(json.dumps({"initial_refs": refs,
            "request": {"dependency_cache": str(self.store)}}))
        return root

    def test_two_runs_reuse_independent_frozen_copies_without_network(self):
        with patch("urllib.request.build_opener", side_effect=AssertionError("unexpected download")):
            first = self.run_seed("first")
            second = self.run_seed("second")
        relative = self.record["jar"]["path"]
        first_jar = first / "artifacts/dependency-repository" / relative
        second_jar = second / "artifacts/dependency-repository" / relative
        self.assertEqual(first_jar.read_bytes(), second_jar.read_bytes())
        self.assertNotEqual(first_jar.stat().st_ino, second_jar.stat().st_ino)
        self.assertNotEqual(first_jar.stat().st_ino, (self.store / relative).stat().st_ino)
        (self.store / relative).write_bytes(b"bad shared storage")
        # The snapshot remains usable even if shared state is later corrupted.
        mounts, _ = dependency_mounts(first, ["bash", "/workspace/gradlew", "build"])
        self.assertTrue(mounts)
        with self.assertRaisesRegex(ValueError, "integrity"):
            self.run_seed("third")

    def test_all_gradle_phases_get_read_only_seed_and_private_cache(self):
        root = self.run_seed("run")
        workspace = root / "worktree"
        workspace.mkdir()
        caches = ("gradle-cache", "baseline-gradle-cache", "target-gradle-cache",
                  "baseline-contract-gradle-cache", "acceptance-gradle-cache",
                  "acceptance-contract-gradle-cache", "acceptance-client-gradle-cache",
                  "independent-test-gradle-cache")
        with patch("modport.handlers.shutil.which", return_value="/usr/bin/bwrap"):
            for cache in caches:
                with self.subTest(cache=cache):
                    args = _sandboxed_build_command(root, workspace,
                        ["bash", "/workspace/gradlew", "--no-daemon", "build"], cache_name=cache)
                    pos = args.index("/modport-dependencies")
                    self.assertEqual("--ro-bind", args[pos - 2])
                    self.assertNotIn(str(self.store), args)
                    self.assertIn(str(root / "toolchains" / cache), args)
                    self.assertIn("/modport-dependency.init.gradle", args[args.index("--chdir"):])

    def test_client_launcher_receives_init_flag_at_gradle_boundary(self):
        root = self.run_seed("run")
        mounts, args = dependency_mounts(root, ["python3", "-I", "/modport-support/launch.py",
            "--timeout", "30", "--", "bash", "/workspace/gradlew", "runClient"])
        self.assertTrue(mounts)
        offset = args.index("/workspace/gradlew")
        self.assertEqual(args[offset + 1:offset + 3], ["--init-script", "/modport-dependency.init.gradle"])
        self.assertNotIn("--init-script", args[:offset])

    def test_tampered_manifest_script_or_jar_is_rejected_before_launch(self):
        for name, relative in (("manifest", "manifest.json"), ("jar", self.record["jar"]["path"]),
                               ("script", None)):
            with self.subTest(name=name):
                root = self.run_seed(name)
                path = root / ("artifacts/dependency-repository.init.gradle" if relative is None
                               else "artifacts/dependency-repository/" + relative)
                path.write_bytes(path.read_bytes() + b"tampered")
                with self.assertRaisesRegex(RuntimeError, "validation failed"):
                    dependency_mounts(root, ["bash", "/workspace/gradlew", "build"])

    def test_configured_seed_cannot_silently_disappear(self):
        root = self.run_seed("run")
        (root / "run.json").write_text(json.dumps({"request": {"dependency_cache": str(self.store)}}))
        with self.assertRaisesRegex(RuntimeError, "not frozen"):
            dependency_mounts(root, ["bash", "/workspace/gradlew", "build"])

    def test_empty_store_needs_no_network_or_mount(self):
        root = self.root / "empty-run"
        (root / "artifacts").mkdir(parents=True)
        refs = prepare_dependency_seed(self.root / "missing", root)
        (root / "run.json").write_text(json.dumps({"initial_refs": refs}))
        args = ["bash", "/workspace/gradlew", "build"]
        self.assertEqual(([], args), dependency_mounts(root, args))
        self.assertFalse((self.root / "missing").exists())

    def test_cli_freezes_shared_location_and_has_explicit_opt_out(self):
        base = ["compile", "--mod-id", "example", "--source-repository", "https://example.invalid/mod.git",
                "--source-revision", "a" * 40, "--source-minecraft", "1.20.1",
                "--target-minecraft", "26.1.2", "--output-root", "/tmp/cache-test/runs"]
        with patch.dict("os.environ", {}, clear=True):
            request = _request(parser().parse_args(base))
            self.assertEqual("/tmp/cache-test/dependency-cache", request.dependency_cache)
            self.assertIsNone(_request(parser().parse_args(base + ["--no-dependency-cache"])).dependency_cache)
        with self.assertRaises(ValueError):
            type(request).from_mapping({**request.to_dict(), "dependency_cache": "../shared"}).validate()

    def test_transport_errors_remain_distinct_from_compiler_errors(self):
        self.assertEqual("dependency_rate_limited", gradle_failure_kind(
            "Could not GET 'https://mirror/file.pom'. Received status code 429 from server: Too Many Requests"))
        self.assertEqual("dependency_resolution_failed", gradle_failure_kind(
            "Could not resolve all files for configuration ':compileClasspath'. java.net.UnknownHostException: mirror.example"))
        self.assertEqual("gradle_failed", gradle_failure_kind(
            "Could not resolve all files for configuration ':compileClasspath'. No matching variant was found."))
        self.assertEqual("gradle_failed", gradle_failure_kind("Thing.java:32: error: cannot find symbol"))

    def test_transport_stops_repair_but_incompatible_variant_does_not(self):
        from modport.diagnostics import classify_characterization_failure
        from modport.operations import MigrationOperations
        from modport.contracts import OperationResult
        operations = MigrationOperations()
        app = operations._new_application()
        result = OperationResult("failed", "run", "target_build", "target_build", "target_build:1",
                                 error_code="dependency_rate_limited")
        changes = operations._repair_failure({}, {}, app, "target_build", result, "target_build:1")
        self.assertEqual([{"kind": "finish", "state": "failed"}], changes)
        self.assertEqual(0, app["agent_assignments"])
        variant = classify_characterization_failure(
            "Could not resolve all files for configuration ':compileClasspath'. No matching variant was found.",
            exit_code=1, timed_out=False, phase="target")
        self.assertTrue(variant["repair_allowed"])
        transport = classify_characterization_failure("Received status code 429 from server: Too Many Requests",
            exit_code=1, timed_out=False, phase="target")
        self.assertFalse(transport["repair_allowed"])


if __name__ == "__main__":
    unittest.main()
