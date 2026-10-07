"""Portable defaults and production routing without running artifact operations."""
from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from modport import cli, skill_runtime, storage_lifecycle, user_paths
from modport.contracts import OperationInput
from modport.models import MigrationRequest
from modport.workflow import WORKFLOW_VERSION


class UserPathsTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.home = self.root / "home"
        self.addCleanup(patch.stopall)
        patch.dict(os.environ, {"HOME": str(self.home)}, clear=True).start()
        patch("modport.user_paths.Path.home", return_value=self.home).start()
        patch("modport.user_paths.sys.platform", "linux").start()

    def run_args(self, command="run"):
        return [command, "--mod-id", "sample", "--source-repository", "https://example.org/sample",
                "--source-revision", "v1", "--source-minecraft", "1.20.1",
                "--target-minecraft", "1.21.1", "--source-loader-version", "47.3.0",
                "--target-loader-version", "21.1.0", "--source-java", "17", "--target-java", "21"]

    def skill_command(self, store=None):
        request = {"workflow_mode": "skill_generation", "skill_kind": "java",
                   "source_java": "17", "target_java": "21"}
        if store is not None:
            request["skill_store"] = str(store)
        return OperationInput("run", "lookup", "skill_lookup", "lookup", str(self.root),
                              payload={"request": request})

    def test_platform_defaults_and_absolute_xdg_override(self):
        fallback = self.home / ".local" / "share" / "modport"
        self.assertEqual(user_paths.data_root(), fallback)
        os.environ["XDG_DATA_HOME"] = "relative-data"
        self.assertEqual(user_paths.data_root(), fallback)
        os.environ["XDG_DATA_HOME"] = str(self.root / "xdg")
        self.assertEqual(user_paths.data_root(), self.root / "xdg" / "modport")
        with patch("modport.user_paths.sys.platform", "darwin"):
            self.assertEqual(user_paths.data_root(), self.home / "Library" / "Application Support" / "ModPort")
        with patch("modport.user_paths.sys.platform", "win32"):
            self.assertEqual(user_paths.data_root(), self.home / "AppData" / "Local" / "ModPort")
            os.environ["LOCALAPPDATA"] = str(self.root / "local-app-data")
            self.assertEqual(user_paths.data_root(), self.root / "local-app-data" / "ModPort")
        self.assertFalse(self.home.exists(), "resolving defaults must not create directories")

    def test_shared_data_override_and_individual_override_precedence(self):
        os.environ["MODPORT_DATA_ROOT"] = "~/modport-data"
        base = self.home / "modport-data"
        self.assertEqual(user_paths.runs_root(), base / "runs")
        self.assertEqual(user_paths.skill_store(), base / "migration-skills")
        self.assertEqual(user_paths.archives_root(), base / "archives")
        for env, resolver in (("MODPORT_OUTPUT_ROOT", user_paths.runs_root),
                              ("MODPORT_SKILL_STORE", user_paths.skill_store),
                              ("MODPORT_ARCHIVE_ROOT", user_paths.archives_root)):
            with self.subTest(env=env):
                os.environ[env] = "~/individual"
                self.assertEqual(resolver(), self.home / "individual")
                self.assertEqual(resolver("~/explicit"), self.home / "explicit")

    def test_parent_relative_cli_and_environment_paths_are_normalized_lexically(self):
        expected = Path(os.path.abspath("../skill store"))
        os.environ["MODPORT_SKILL_STORE"] = "../skill store"
        self.assertEqual(user_paths.skill_store(), expected)
        args = cli.parser().parse_args(self.run_args() + ["--skill-store", "../skill store"])
        self.assertEqual(args.skill_store, str(expected))
        self.assertEqual(skill_runtime.resolve_skill_inputs(self.skill_command(args.skill_store))["store"], str(expected))
        self.assertEqual(skill_runtime.resolve_skill_inputs(self.skill_command())["store"], str(expected))

    def test_cli_defaults_refresh_and_new_request_records_skill_store(self):
        os.environ["MODPORT_DATA_ROOT"] = str(self.root / "first")
        first = cli.parser().parse_args(self.run_args())
        request = cli._request(first)
        self.assertEqual(request.output_root, str(self.root / "first" / "runs"))
        self.assertEqual(request.skill_store, str(self.root / "first" / "migration-skills"))
        os.environ["MODPORT_DATA_ROOT"] = str(self.root / "second")
        for command in ("run", "compile"):
            args = cli.parser().parse_args(self.run_args(command))
            self.assertEqual(args.output_root, str(self.root / "second" / "runs"))
            self.assertEqual(args.skill_store, str(self.root / "second" / "migration-skills"))
        skill = cli.parser().parse_args(["skill-generate", "--kind", "java"])
        web = cli.parser().parse_args(["web", "--password-file", "password.txt"])
        storage = cli.parser().parse_args(["storage-maintain", "--run-dir", str(self.root)])
        self.assertEqual(skill.output_root, str(self.root / "second" / "runs"))
        self.assertEqual(skill.skill_store, str(self.root / "second" / "migration-skills"))
        self.assertEqual(web.runs_root, str(self.root / "second" / "runs"))
        self.assertEqual(storage.archive_root, str(self.root / "second" / "archives"))
        frozen = OperationInput("run", "lookup", "skill_lookup", "lookup", str(self.root),
                                payload={"request": request.to_dict()})
        self.assertEqual(skill_runtime.resolve_skill_inputs(frozen)["store"], request.skill_store)

    def test_cli_flags_override_environment_and_expand_home(self):
        os.environ.update(MODPORT_OUTPUT_ROOT="~/env-runs", MODPORT_SKILL_STORE="~/env-skills",
                          MODPORT_ARCHIVE_ROOT="~/env-archives", MODPORT_DEPENDENCY_CACHE="~/env-cache")
        args = cli.parser().parse_args(self.run_args())
        self.assertEqual(args.output_root, str(self.home / "env-runs"))
        self.assertEqual(args.skill_store, str(self.home / "env-skills"))
        self.assertEqual(cli._request(args).dependency_cache, str(self.home / "env-cache"))
        args = cli.parser().parse_args(self.run_args() + ["--output-root", "~/flag-runs",
                                                        "--skill-store", "~/flag-skills"])
        self.assertEqual(args.output_root, str(self.home / "flag-runs"))
        self.assertEqual(args.skill_store, str(self.home / "flag-skills"))
        with patch("modport.cli.MigrationOperations"), patch("modport.web.serve", return_value=0) as serve:
            self.assertEqual(cli.main(["web", "--password-file", "password.txt",
                                       "--runs-root", "~/flag-web"]), 0)
            self.assertEqual(serve.call_args.args[0], str(self.home / "flag-web"))
        with patch("modport.cli.MigrationOperations"), patch(
                "modport.storage_lifecycle.retention_checkpoint", return_value={}) as checkpoint, redirect_stdout(io.StringIO()):
            self.assertEqual(cli.main(["storage-maintain", "--run-dir", str(self.root),
                                       "--archive-root", "~/flag-archives"]), 0)
            self.assertEqual(checkpoint.call_args.kwargs["archive_root"], str(self.home / "flag-archives"))

    def test_skill_input_default_reaches_selection_and_frozen_store_wins(self):
        os.environ["MODPORT_SKILL_STORE"] = "~/selected-skills"
        command = self.skill_command()
        before = command.to_dict()
        inputs = skill_runtime.resolve_skill_inputs(command)
        self.assertEqual(inputs["store"], str(self.home / "selected-skills"))
        with patch("modport.skill_runtime._cache_records", return_value=[]) as records, patch(
                "modport.skill_runtime._workspace_record", return_value=None):
            self.assertIsNone(skill_runtime._select_skill(self.root, inputs, "java"))
            self.assertEqual(records.call_args.args[0]["store"], str(self.home / "selected-skills"))
        self.assertEqual(command.to_dict(), before)
        explicit = self.skill_command("~/frozen-skills")
        os.environ["MODPORT_SKILL_STORE"] = "~/different-skills"
        self.assertEqual(skill_runtime.resolve_skill_inputs(explicit)["store"], str(self.home / "frozen-skills"))
        os.environ.pop("MODPORT_SKILL_STORE")
        os.environ["MODPORT_DATA_ROOT"] = str(self.root / "data")
        self.assertEqual(skill_runtime.resolve_skill_inputs(command)["store"], str(self.root / "data" / "migration-skills"))

    def test_automatic_retention_uses_same_archive_policy_as_explicit_checkpoint(self):
        plan = {"candidates": [{"status": "eligible"}]}
        with patch("modport.storage_lifecycle.settled_segments", return_value=[]), patch(
                "modport.storage_lifecycle.plan_retention", return_value=plan), patch(
                "modport.storage_lifecycle.apply_retention", return_value={"status": "archived"}) as apply, patch(
                "modport.storage_lifecycle._storage_directory"), patch("modport.storage_lifecycle.atomic_json"):
            os.environ["MODPORT_DATA_ROOT"] = str(self.root / "data")
            self.assertEqual(storage_lifecycle.automatic_retention(self.root), {"status": "archived"})
            self.assertEqual(apply.call_args.args[2], self.root / "data" / "archives")
            os.environ["MODPORT_ARCHIVE_ROOT"] = "~/env-archives"
            storage_lifecycle.automatic_retention(self.root)
            self.assertEqual(apply.call_args.args[2], self.home / "env-archives")
            storage_lifecycle.retention_checkpoint(self.root, archive_root="~/explicit-archives")
            self.assertEqual(apply.call_args.args[2], self.home / "explicit-archives")

    def test_public_submit_freezes_skill_path_before_preflight_without_mutating_request(self):
        from modport.operations import MigrationOperations

        class PreflightReached(Exception):
            pass

        os.environ["MODPORT_SKILL_STORE"] = "~/public-skills"
        for configured, expected in ((None, self.home / "public-skills"),
                                     ("../explicit skill store", Path(os.path.abspath("../explicit skill store")))):
            with self.subTest(configured=configured):
                request = MigrationRequest("sample", "https://example.org/sample", "1.20.1", "1.21.1",
                                           skill_store=configured)
                before = request.to_dict()
                with patch("modport.operations.check_storage_budget"), patch(
                        "modport.skill_runtime.validate_requested_skills", side_effect=PreflightReached) as preflight:
                    with self.assertRaises(PreflightReached):
                        MigrationOperations().submit(request, run_dir=self.root / "new-run",
                                                     model_policy={"default": {"model": "test", "reasoning_effort": "low"}})
                self.assertEqual(preflight.call_args.args[0]["skill_store"], str(expected))
                self.assertEqual(request.to_dict(), before)
                self.assertFalse((self.root / "new-run").exists())

    def test_desktop_submission_and_persistent_environment_restore_share_paths(self):
        from modport.desktop_driver import PersistentSupervisor, restore_host_environment
        from modport.desktop_service import DesktopApplication

        body = {"project_name": "Path test", "source_repository": "https://github.com/example/sample",
                "source_minecraft": "1.20.1", "target_minecraft": "1.21.1",
                "source_loader_version": "47.3.0", "target_loader_version": "21.1.0",
                "max_seconds": 600, "max_tokens": 10000,
                "model_config": {"default": {"model": "test", "reasoning_effort": "low"}}}
        captured = []

        def submit(request, *, run_dir, run_id, model_policy):
            # Replace only SDK submission; the service, registry, launch snapshot
            # and filtered restore run through their production implementations.
            run_dir.mkdir()
            frozen = {"run_id": run_id, "run_dir": str(run_dir), "request": request.to_dict(),
                      "definition": {"workflow_version": WORKFLOW_VERSION}, "started_at": time.time()}
            (run_dir / "run.json").write_text(json.dumps(frozen), encoding="utf-8")
            captured.append((request, (run_dir / "run.json").read_bytes()))
            return SimpleNamespace(snapshot={"state": "running", "tasks": {}})

        def execute(argv, **kwargs):
            if 'enable' in argv and any('watchdog' in item for item in argv):
                from modport.evidence import atomic_json
                from modport.platform_runtime import process_birth
                instance_id = next(item.removeprefix('modport-').removesuffix('-watchdog.service')
                                   for item in argv if item.endswith('-watchdog.service'))
                atomic_json(application.state.run_dir(instance_id) / 'desktop-watchdog-state.json',
                    {'instance': instance_id, 'pid': os.getpid(), 'birth': process_birth(os.getpid()),
                     'at': time.time(), 'state': 'running'})
            return subprocess.CompletedProcess(argv, 0, "active\n" if "is-active" in argv else "", "")

        original_cwd = Path.cwd()
        try:
            os.chdir(self.root)
            for configured in (None, "../relative skill store"):
                with self.subTest(configured=configured):
                    os.environ["MODPORT_DATA_ROOT"] = "ignored-service-env"
                    os.environ["MODPORT_ARCHIVE_ROOT"] = "../relative archives"
                    os.environ["MODPORT_OUTPUT_ROOT"] = "relative runs"
                    os.environ["UNRELATED_PRIVATE_VALUE"] = "exclude-from-host-snapshot"
                    if configured is None:
                        os.environ.pop("MODPORT_SKILL_STORE", None)
                    else:
                        os.environ["MODPORT_SKILL_STORE"] = configured
                    application = DesktopApplication(self.root / ("default-app" if configured is None else "override-app"),
                                                     operations=SimpleNamespace(submit=submit))
                    application.supervisor = PersistentSupervisor(application.state, platform_name="Linux", execute=execute)
                    expected_store = (application.state.root / "migration-skills" if configured is None
                                      else Path(os.path.abspath(configured)))
                    expected_archives = Path(os.path.abspath("../relative archives"))
                    expected_runs = Path(os.path.abspath("relative runs"))
                    with patch.object(application, "environment", return_value={"ready": True}):
                        result = application.create_run(body)
                    request, frozen_bytes = captured[-1]
                    self.assertEqual(request.skill_store, str(expected_store))
                    private = application.state.root / "host-runtime" / (result["id"] + ".json")
                    saved = json.loads(private.read_text(encoding="utf-8"))
                    self.assertNotIn("UNRELATED_PRIVATE_VALUE", saved)
                    self.assertEqual(saved["MODPORT_DATA_ROOT"], str(application.state.root))
                    self.assertEqual(saved["MODPORT_ARCHIVE_ROOT"], str(expected_archives))
                    self.assertEqual(saved["MODPORT_OUTPUT_ROOT"], str(expected_runs))
                    os.chdir(application.state.root)
                    os.environ["MODPORT_DATA_ROOT"] = "changed-data"
                    os.environ["MODPORT_SKILL_STORE"] = "changed-skills"
                    os.environ["MODPORT_ARCHIVE_ROOT"] = "changed-archives"
                    restore_host_environment(application.state, result["id"])
                    self.assertEqual(user_paths.data_root(), application.state.root)
                    self.assertEqual(user_paths.skill_store(), expected_store)
                    self.assertEqual(user_paths.archives_root(), expected_archives)
                    self.assertEqual(user_paths.runs_root(), expected_runs)
                    self.assertEqual(skill_runtime.resolve_skill_inputs(self.skill_command(request.skill_store))["store"], str(expected_store))
                    self.assertEqual((application.state.run_dir(result["id"]) / "run.json").read_bytes(), frozen_bytes)
                    os.chdir(self.root)
        finally:
            os.chdir(original_cwd)


if __name__ == "__main__":
    unittest.main()
