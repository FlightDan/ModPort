import json
from pathlib import Path
import subprocess
import tempfile
import unittest

from tests import test_artifact_handoff as handoff_fixture
from tests.test_artifact_required_behavior import ArtifactStageFixture, ReportFixture
from modport.artifact_handoff import prepare_handoff
from modport.evidence import atomic_json, file_digest, verified_path
from modport.models import MigrationRequest
from modport.operations import MigrationOperations
from modport.workflow import (
    ARTIFACT_VERIFICATION_STAGES,
    WORKFLOW_VERSION,
    agent_model_policy,
    compile_migration_workflow,
)


def git(directory: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(directory), *args], text=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
    )
    if result.returncode:
        raise AssertionError(result.stderr)
    return result.stdout.strip()


class ArtifactVerificationWorkflowTests(unittest.TestCase):
    def test_mode_compiles_to_a_sequential_non_migration_route(self):
        request = MigrationRequest(
            "example", "https://example.invalid/mod.git", "1.20.1", "1.21.1",
            workflow_mode="artifact_verification",
        )
        definition = compile_migration_workflow(request).to_dict()

        self.assertEqual(WORKFLOW_VERSION, definition['workflow_version'])
        self.assertTrue(definition['progress_supervision_policy']['enabled'])
        self.assertEqual("modport.artifact_verification", definition["workflow_type"])
        self.assertEqual(list(ARTIFACT_VERIFICATION_STAGES), definition["main_stages"])
        self.assertEqual([], definition["early_stages"])
        self.assertEqual(
            dict(zip(ARTIFACT_VERIFICATION_STAGES, ARTIFACT_VERIFICATION_STAGES[1:])),
            definition["next_stage"],
        )
        self.assertEqual("source_baseline_behavior_tests",
                         definition["validation_policy"]["required_checks"][0])
        self.assertEqual("unverified", definition["validation_policy"]["acceptance_status"])
        self.assertTrue(definition['validation_policy']['required_behavior_completion'])
        self.assertEqual(("gpt-6.1-sol", "high"),
                         agent_model_policy(WORKFLOW_VERSION, "contract_review"))
        stage_ids = {row["stage_id"] for row in definition["stages"]}
        self.assertFalse({"migration_inventory", "migration_plan", "implementation",
                          "coder", "target_build"} & stage_ids)

    def test_public_sdk_submit_and_dispatch_use_only_the_handed_off_artifact_route(self):
        fixture = handoff_fixture.ArtifactHandoffTests()
        fixture.setUp()
        self.addCleanup(fixture.tearDown)

        original = MigrationRequest(
            "example", "https://example.invalid/mod.git", "1.20.1", "1.21.1",
            source_revision=fixture.source_commit,
        )
        source_header = json.loads((fixture.root / "run.json").read_text())
        source_header["request"] = original.to_dict()
        atomic_json(fixture.root / "run.json", source_header)

        jar = fixture.worktree / "build" / "libs" / "example.jar"
        jar.parent.mkdir(parents=True)
        jar.write_bytes(b"delivered-test-jar")
        git(fixture.worktree, "add", "build/libs/example.jar")
        git(fixture.worktree, "commit", "-m", "deliver target jar")
        target_commit = git(fixture.worktree, "rev-parse", "HEAD")
        receipt_path = fixture.root / "artifacts" / "target-package-receipt.json"
        atomic_json(receipt_path, {
            "status": "passed",
            "target_clean": True,
            "run_id": "old-run-id",
            "target_commit": target_commit,
            "artifacts": [{
                "path": "worktree/build/libs/example.jar",
                "sha256": file_digest(jar),
                "size": jar.stat().st_size,
            }],
        })
        package = fixture.base / "artifact-verification-handoff"
        prepare_handoff(fixture.root, package, [
            "worktree/build/libs/example.jar",
            "artifacts/target-package-receipt.json",
        ])

        request = MigrationRequest(
            original.mod_id, original.source_repository,
            original.source_minecraft, original.target_minecraft,
            source_revision=original.source_revision,
            workflow_mode="artifact_verification",
        )
        run_root = fixture.base / "artifact-verification-run"
        fixture_handler = ArtifactStageFixture()
        handlers = {'modport.' + stage: fixture_handler for stage in ARTIFACT_VERIFICATION_STAGES}
        handlers['modport.artifact_test_report'] = ReportFixture()
        operations = MigrationOperations(handlers=handlers, isolation_mode="thread")
        submitted = operations.submit(
            request, run_dir=run_root, run_id="artifact-check",
            artifact_handoff=package,
        )

        header = submitted.snapshot["input"]
        self.assertEqual("artifact_verification", header["request"]["workflow_mode"])
        self.assertEqual("unverified", header["artifact_handoff"]["acceptance_status"])
        artifact_input = header["initial_refs"]["artifact_input"]
        descriptor = json.loads(verified_path(run_root, artifact_input).read_text())
        self.assertEqual(target_commit, descriptor["target_commit"])
        self.assertEqual(file_digest(jar), descriptor["jar_ref"]["sha256"])

        completed = operations.execute(submitted)
        self.assertEqual("succeeded", completed.status)
        application = completed.snapshot["application_state"]
        self.assertEqual("unverified", application["acceptance_status"])
        self.assertEqual(list(ARTIFACT_VERIFICATION_STAGES),
                         [row["stage"] for row in application["history"]])
        self.assertFalse({"migration_inventory", "migration_plan", "implementation",
                          "coder", "target_build"} & set(completed.snapshot["tasks"]))


if __name__ == "__main__":
    unittest.main()
