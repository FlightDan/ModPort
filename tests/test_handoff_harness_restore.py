"""Regression coverage for explicit harness restore from artifact-only handoffs."""

from dataclasses import replace
import json
import shutil
import subprocess
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import test_artifact_handoff as fixture
import test_handlers as handler_fixture
from fixtures_modport import registry
from modport.artifact_handoff import prepare_handoff
from modport.contracts import OperationInput
from modport.evidence import atomic_json, file_digest, verified_path
from modport.handlers import CodexStageHandler, ValidateInputHandler
from modport.harness_snapshot import RestoreHarnessHandler
from modport.models import MigrationRequest
from modport.operations import MigrationOperations


class VersionedValidateInputHandler(ValidateInputHandler):
    __execution_kernel_revision__ = "handoff-harness-validate-fixture-v1"


class VersionedRestoreHarnessHandler(RestoreHarnessHandler):
    __execution_kernel_revision__ = "handoff-harness-restore-fixture-v1"


class HandoffHarnessRestoreTests(unittest.TestCase):
    CONTRACT = {
        "contract_id": "original-contract-id",
        "entries": [
            {"entry_id": "original-server-behavior-id"},
            {"entry_id": "original-client-behavior-id"},
        ],
    }
    INCLUDED = {
        "baseline/.modport/functional-contract.json":
            json.dumps(CONTRACT, sort_keys=True, separators=(",", ":")).encode("utf-8") + b"\n",
        "baseline/.modport/init.gradle": b"// original init\r\n",
        # A source package named build remains source once a src boundary was crossed.
        "baseline/.modport/characterization/src/main/java/example/build/Probe.java":
            b"package example.build;\nfinal class Probe {}\n",
        "baseline/.modport/fixtures/input.bin": b"\x00original fixture\xff",
    }
    EXCLUDED = {
        "baseline/.modport/evidence/passed.json": b'{"accepted":true}\n',
        "baseline/.modport/runtime/server/state.dat": b"runtime state",
        "baseline/.modport/characterization/build/classes/Probe.class": b"compiled output",
        "baseline/.modport/contract-review.json": b'{"verdict":"approved"}\n',
    }

    def setUp(self):
        self.fixture = fixture.ArtifactHandoffTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.request = MigrationRequest(
            "example",
            "https://example.invalid/mod.git",
            "1.20.1",
            "1.21.1",
            source_revision=self.fixture.source_commit,
        )
        header = json.loads((self.fixture.root / "run.json").read_text(encoding="utf-8"))
        header["request"] = self.request.to_dict()
        atomic_json(self.fixture.root / "run.json", header)
        for relative, data in {**self.INCLUDED, **self.EXCLUDED}.items():
            path = self.fixture.root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        self.package = self.fixture.base / "harness-package"
        prepare_handoff(
            self.fixture.root,
            self.package,
            [*self.INCLUDED, *self.EXCLUDED],
        )
        self.ops = MigrationOperations(handlers=registry(), isolation_mode="thread")

    def submit(self, name="fresh", *, inherit_harness=True, operations=None, package=None):
        root = self.fixture.base / name
        run = (operations or self.ops).submit(
            self.request,
            run_dir=root,
            run_id=name,
            artifact_handoff=package or self.package,
            inherit_harness=inherit_harness,
        )
        return root, run

    def command(self, root, run, *, stage="source", refs=None):
        header = run.snapshot["input"]
        return OperationInput(
            run.run_id,
            stage,
            stage,
            f"{run.run_id}:{stage}:1",
            str(root),
            payload={"request": self.request.to_dict()},
            artifact_refs=header["initial_refs"] if refs is None else refs,
            options={"acceptance_rubric_sha256": header["rubric_sha256"]},
        )

    def validate_then_restore(self, root, run):
        source = ValidateInputHandler()(self.command(root, run))
        self.assertEqual("completed", source.status, source.detail)
        refs = {
            **run.snapshot["input"]["initial_refs"],
            **source.outputs["artifact_refs"],
        }
        restored = RestoreHarnessHandler()(
            self.command(root, run, stage="contract_restore", refs=refs)
        )
        return source, restored

    def run_contract_author(self, name, *, workflow_version=21,
                            inherited=True, reviewer_rework=True):
        root = self.fixture.base / name
        contract = root / "baseline/.modport/functional-contract.json"
        contract.parent.mkdir(parents=True)
        original = self.INCLUDED["baseline/.modport/functional-contract.json"]
        contract.write_bytes(original)
        command = handler_fixture.HandlerTests._command(
            root,
            "contract_draft",
            payload={
                "request": {"budget": {"max_agent_assignments": 2}},
                **({"reviewer_rework": {
                    "request_id": "repair-restored-contract",
                    "instructions": "Repair one assertion without replacing the contract.",
                }} if reviewer_rework else {}),
            },
        )
        source = root / "artifacts/source.json"
        atomic_json(source, {"source_commit": self.fixture.source_commit})
        refs = {
            **command.artifact_refs,
            "source_evidence": {
                "path": source.relative_to(root).as_posix(),
                "sha256": file_digest(source),
            },
        }
        if inherited:
            inherited_path = root / "artifacts/inherited-harness.json"
            atomic_json(inherited_path, {"requires_fresh_verification": True})
            refs["inherited_harness"] = {
                "path": inherited_path.relative_to(root).as_posix(),
                "sha256": file_digest(inherited_path),
            }
        options = dict(command.options)
        if workflow_version is not None:
            options["workflow_version"] = workflow_version
        command = replace(command, artifact_refs=refs, options=options)
        seen_at_model_call = []

        def execute(**kwargs):
            seen_at_model_call.append(contract.read_bytes() if contract.is_file() else None)
            if not contract.exists():
                contract.write_bytes(b'{"replacement":true}\n')
            stdout = json.dumps({
                "type": "item.completed",
                "item": {"type": "agent_message", "text": "repaired"},
            }) + "\n"
            kwargs["log"].parent.mkdir(parents=True, exist_ok=True)
            kwargs["log"].write_text(stdout, encoding="utf-8")
            return subprocess.CompletedProcess(["opencode"], 0, stdout)

        def compress(text, **_kwargs):
            return SimpleNamespace(text=text, metadata={"compressed": False})

        with (
            patch("modport.opencode_agent.run_agent", side_effect=execute),
            patch("modport.handlers.PromptCompressor.from_environment") as compressor,
            patch("modport.handlers._baseline_changes_are_isolated", return_value=(True, [])),
        ):
            compressor.return_value.compress.side_effect = compress
            result = CodexStageHandler(
                "repair the restored contract",
                baseline=True,
                required_paths=(".modport/functional-contract.json",),
            )(command)
        self.assertEqual("completed", result.status, result.detail)
        self.assertEqual(1, len(seen_at_model_call))
        return original, seen_at_model_call[0]

    def test_explicit_restore_is_fresh_authenticated_and_source_only(self):
        root, run = self.submit()
        header = run.snapshot["input"]

        self.assertEqual(0, run.snapshot["revision"])
        self.assertEqual({}, run.snapshot["tasks"])
        self.assertIsNone(run.snapshot["application_state"])
        self.assertIsNone(header["parent_run_id"])
        self.assertEqual([], header["prior_findings"])
        self.assertTrue(header["inherit_harness"])
        self.assertEqual("unverified", header["artifact_handoff"]["acceptance_status"])
        self.assertNotIn("failure_packet", header["initial_refs"])
        self.assertFalse(any(key.startswith("parent:") for key in header["initial_refs"]))

        inherited = {
            key.removeprefix("inherited_harness:")
            for key in header["initial_refs"]
            if key.startswith("inherited_harness:")
        }
        expected = {relative.removeprefix("baseline/") for relative in self.INCLUDED}
        self.assertEqual(expected, inherited)
        for relative in self.INCLUDED:
            harness_key = "inherited_harness:" + relative.removeprefix("baseline/")
            handoff_key = "handoff:" + relative
            self.assertEqual(header["initial_refs"][handoff_key],
                             header["initial_refs"][harness_key])
            self.assertEqual(self.INCLUDED[relative],
                             verified_path(root, header["initial_refs"][harness_key]).read_bytes())
        for relative in self.EXCLUDED:
            self.assertIn("handoff:" + relative, header["initial_refs"])
            self.assertNotIn("inherited_harness:" + relative.removeprefix("baseline/"),
                             header["initial_refs"])

        snapshot = json.loads(verified_path(
            root, header["initial_refs"]["inherited_harness"]).read_text(encoding="utf-8"))
        self.assertEqual("unverified", snapshot["acceptance_status"])
        self.assertFalse(snapshot["scheduler_history_imported"])
        self.assertTrue(snapshot["requires_fresh_verification"])

        source, restored = self.validate_then_restore(root, run)
        self.assertEqual("completed", restored.status, restored.detail)
        self.assertFalse(source.outputs["artifact_handoff"]["scheduler_history_imported"])
        baseline = root / "baseline"
        for relative, expected_bytes in self.INCLUDED.items():
            self.assertEqual(expected_bytes,
                             (baseline / relative.removeprefix("baseline/")).read_bytes())
        restored_contract = json.loads(
            (baseline / ".modport/functional-contract.json").read_text(encoding="utf-8")
        )
        self.assertEqual(self.CONTRACT["contract_id"], restored_contract["contract_id"])
        self.assertEqual(
            [entry["entry_id"] for entry in self.CONTRACT["entries"]],
            [entry["entry_id"] for entry in restored_contract["entries"]],
        )
        for relative in self.EXCLUDED:
            self.assertFalse((baseline / relative.removeprefix("baseline/")).exists())

    def test_default_artifact_handoff_remains_refs_only(self):
        root, run = self.submit("nonoptin", inherit_harness=False)
        header = run.snapshot["input"]
        self.assertFalse(header["inherit_harness"])
        self.assertFalse(any(key == "inherited_harness" or key.startswith("inherited_harness:")
                             for key in header["initial_refs"]))
        for relative in [*self.INCLUDED, *self.EXCLUDED]:
            self.assertIn("handoff:" + relative, header["initial_refs"])

        result = ValidateInputHandler()(self.command(root, run))
        self.assertEqual("completed", result.status, result.detail)
        self.assertFalse((root / "baseline/.modport/functional-contract.json").exists())

    def test_missing_contract_is_rejected_before_root_creation(self):
        package = self.fixture.base / "missing-contract-package"
        prepare_handoff(
            self.fixture.root,
            package,
            [relative for relative in self.INCLUDED if not relative.endswith("functional-contract.json")],
        )
        root = self.fixture.base / "missing-contract-run"
        with self.assertRaisesRegex(ValueError, "requires a selected baseline contract"):
            self.ops.submit(
                self.request,
                run_dir=root,
                run_id="missing-contract",
                artifact_handoff=package,
                inherit_harness=True,
            )
        self.assertFalse(root.exists())

    def test_tampered_handoff_contract_hash_is_rejected_before_root_creation(self):
        package = self.fixture.base / "tampered-contract-package"
        shutil.copytree(self.package, package)
        manifest = json.loads((package / "manifest.json").read_text(encoding="utf-8"))
        contract = next(item for item in manifest["artifacts"]
                        if item["source_path"] == "baseline/.modport/functional-contract.json")
        (package / contract["path"]).write_bytes(b'{"contract_id":"forged"}\n')

        root = self.fixture.base / "tampered-contract-run"
        with self.assertRaisesRegex(ValueError, "checksum|size"):
            self.ops.submit(
                self.request,
                run_dir=root,
                run_id="tampered-contract",
                artifact_handoff=package,
                inherit_harness=True,
            )
        self.assertFalse(root.exists())

    def test_workflow_routes_restore_directly_to_fresh_contract_verify(self):
        handlers = registry()
        handlers["modport.source"] = VersionedValidateInputHandler()
        handlers["modport.contract_restore"] = VersionedRestoreHarnessHandler()
        operations = MigrationOperations(handlers=handlers, isolation_mode="thread")
        root, run = self.submit("routing", operations=operations)

        state = run.snapshot
        with operations.session(root, run.run_id) as (_, header, runtime, sdk):
            for _ in range(100):
                runtime.reap()
                sdk.sync()
                state = operations.tick(sdk, header)
                if "contract_verify" in state["tasks"]:
                    break
                sdk.flush()
                runtime.run_once()
            else:
                self.fail(f"contract_verify was not scheduled: {state.get('tasks', {}).keys()}")

        self.assertNotIn("contract_draft", state["tasks"])
        self.assertIn("contract_restore", state["tasks"])
        restore = state["tasks"]["contract_restore"]["attempts"][0]
        self.assertEqual("succeeded", restore["state"])
        verify = state["tasks"]["contract_verify"]["attempts"][0]["command"]
        self.assertEqual(restore["command"]["execution_id"], verify["causation_id"])
        self.assertIn("contract_restore", verify["payload"]["upstream_results"])

    def test_explicit_v21_restored_contract_repair_reaches_agent_intact(self):
        original, seen = self.run_contract_author("codex-restored-repair")
        self.assertEqual(original, seen)

    def test_current_v24_restored_contract_repair_reaches_opencode_intact(self):
        original, seen = self.run_contract_author(
            "opencode-v24-restored-repair", workflow_version=24)
        self.assertEqual(original, seen)

    def test_contract_author_keeps_pre_v21_and_regular_draft_clearing(self):
        cases = (
            ("v20-restored-repair", 20, True, True),
            ("v21-no-inherited-harness", 21, False, True),
            ("v21-regular-draft", 21, True, False),
            ("legacy-default", None, True, True),
        )
        for name, version, inherited, reviewer_rework in cases:
            with self.subTest(name=name):
                _, seen = self.run_contract_author(
                    "codex-" + name,
                    workflow_version=version,
                    inherited=inherited,
                    reviewer_rework=reviewer_rework,
                )
                self.assertIsNone(seen)

    def test_cli_parses_inherit_harness_and_rejects_it_without_handoff(self):
        from modport.cli import main, parser

        argv = [
            "run",
            "--mod-id", "example",
            "--source-repository", "https://example.invalid/mod.git",
            "--source-revision", self.fixture.source_commit,
            "--source-minecraft", "1.20.1",
            "--target-minecraft", "1.21.1",
            "--inherit-harness",
        ]
        parsed = parser().parse_args(argv)
        self.assertTrue(parsed.inherit_harness)
        self.assertIsNone(parsed.handoff)
        with patch("modport.cli.MigrationOperations") as operations, \
                patch("builtins.print") as output:
            self.assertEqual(1, main(argv))
        operations.return_value.run.assert_not_called()
        operations.return_value.submit.assert_not_called()
        self.assertIn("--inherit-harness requires --handoff",
                      " ".join(str(value) for value in output.call_args.args))


if __name__ == "__main__":
    unittest.main()
