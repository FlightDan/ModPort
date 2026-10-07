"""Focused reviewer-rework context and workspace regressions."""
from dataclasses import replace
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from modport.contracts import OperationInput, OperationResult
from modport.evidence import atomic_json
from modport.gap_research import ResearchReviewHandler
from modport.independent_tests import (
    _assemble,
    _load_snapshot,
    _validate_reworked_review_workspace,
)
from modport.rework_effects import AuthorReworkHandler
from tests import test_independent_tests as independent_fixture


class ReworkedTestReviewContextTests(unittest.TestCase):
    def setUp(self):
        self.fixture = independent_fixture.IndependentTestTests(methodName="runTest")
        self.fixture.setUp()
        self.addCleanup(self.fixture.temp.cleanup)

    def test_reworked_suite_is_bound_to_the_paused_reviewers_source(self):
        fixture = self.fixture
        initial = fixture.modern_design()
        review = fixture.command(
            "test_review",
            refs={**fixture.refs, **initial.outputs["artifact_refs"]},
            workspace=initial.outputs["workspace"],
        )
        review = replace(review, options={**review.options, "workflow_version": 13})
        reviewer_workspace = _assemble(
            review, fixture.root, initial.outputs["workspace"]
        )

        revised = fixture.modern_design(
            value=independent_fixture.suite(),
            mutation=lambda workspace: (
                workspace / ".modport/independent-tests/Independent.java"
            ).write_text(
                "class Independent { void check() { "
                "assert new example.build.Logic().value() != 3; } }\n"
            ),
        )
        rework_command = replace(
            fixture.command(
                "test_design",
                refs={**fixture.refs, **revised.outputs["artifact_refs"]},
                workspace=revised.outputs["workspace"],
            ),
            payload={
                **fixture.payload,
                "reviewer_rework": {"request_id": "request-1"},
                "reviewer_workspace": reviewer_workspace.relative_to(
                    fixture.root
                ).as_posix(),
            },
        )
        AuthorReworkHandler(lambda _: revised)(rework_command)
        refreshed = replace(
            review,
            artifact_refs={**fixture.refs, **revised.outputs["artifact_refs"]},
            options={**review.options, "workspace": revised.outputs["workspace"]},
        )
        _, snapshot, _ = _load_snapshot(
            refreshed,
            fixture.root,
            reviewer_workspace,
            revised.outputs["workspace"],
        )

        _validate_reworked_review_workspace(
            refreshed, fixture.root, reviewer_workspace, snapshot
        )
        source = reviewer_workspace / ".modport/independent-tests/Independent.java"
        self.assertIn("!= 3", source.read_text())

        installed_suite = source.read_text()
        source.write_text("class Stale {}\n")
        with self.assertRaisesRegex(ValueError, "was not installed"):
            _validate_reworked_review_workspace(
                refreshed, fixture.root, reviewer_workspace, snapshot
            )
        source.write_text(installed_suite)
        (reviewer_workspace / "src/main/java/example/build/Logic.java").write_text(
            "package example.build; class Changed {}\n"
        )
        with self.assertRaisesRegex(ValueError, "changed the source"):
            _validate_reworked_review_workspace(
                refreshed, fixture.root, reviewer_workspace, snapshot
            )


class ReworkedResearchReviewContextTests(unittest.TestCase):
    def test_review_validates_refreshed_gap_artifact_identity_and_producer_kinds(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            old_ref = {"path": "artifacts/old.md"}
            new_ref = {"path": "artifacts/new.md"}
            (root / "artifacts").mkdir()
            (root / old_ref["path"]).write_text("old evidence")
            (root / new_ref["path"]).write_text("new evidence")
            command = OperationInput(
                "run",
                "research_review",
                "research_review",
                "review-1",
                str(root),
                options={"workflow_version": 13},
                payload={
                    "project_research_gaps": [{"gap_id": "knowledge:old"}],
                    "research_kinds": ["java"],
                },
                artifact_refs={"gap_research": old_ref},
            )
            producer = OperationResult(
                "completed",
                "run",
                "agent-rework.request-1",
                "gap_research",
                "research-2",
                outputs={
                    "artifact_refs": {"gap_research": new_ref},
                    "research_kinds": ["platform"],
                },
            ).to_dict()
            atomic_json(
                root / "artifacts/rework-tools/review-1/responses/request-1.json",
                {
                    "request_id": "request-1",
                    "sequence": 1,
                    "current_context": {
                        "project_research_gaps": [{"gap_id": "knowledge:new"}],
                        "gap_obligations": [],
                    },
                    "updates": [
                        {
                            "stage": "gap_research",
                            "target_agent": "gap_research",
                            "result": producer,
                        }
                    ],
                },
            )

            def agent(_):
                atomic_json(
                    root / "baseline/.modport/research-review.json",
                    {
                        "verdict": "approved",
                        "findings": [],
                        "approved_generic_knowledge_entries": {},
                        "approved_gap_resolutions": [
                            {
                                "gap_id": "knowledge:new",
                                "project_status": "resolved",
                                "evidence_artifact_ids": ["gap_research"],
                            }
                        ],
                        "verification_requirements": [],
                    },
                )
                return OperationResult("completed")

            with patch("modport.handlers.CodexStageHandler") as handler:
                handler.return_value.side_effect = agent
                result = ResearchReviewHandler()(command)

        self.assertEqual("completed", result.status, result.detail)
        self.assertEqual(
            "knowledge:new",
            result.outputs["approved_gap_resolutions"][0]["gap_id"],
        )


if __name__ == "__main__":
    unittest.main()
