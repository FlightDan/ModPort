"""Frozen v24 compatibility and v25 coder revival workflow policy."""

import hashlib
import json
import unittest

from modport.models import MigrationRequest
from modport.workflow import (
    AGENT_STAGES,
    CODER_REVIVAL_STAGE,
    DEFAULT_AGENT_MODEL,
    DEFAULT_REASONING_EFFORT,
    PLANNER_STAGES,
    WORKFLOW_VERSION,
    WorkflowDefinition,
    agent_model_policy,
)


class CoderRevivalWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.request = MigrationRequest(
            "example", "https://example.invalid/source.git", "1.20.1", "26.1.2"
        ).to_dict()

    @staticmethod
    def definition_hash(definition):
        payload = json.dumps(
            definition, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()

    def test_v24_serialized_definition_remains_exact(self):
        # Captured from the clean v24 workflow source before the v25 edit.
        frozen_v24_sha256 = "05a7eeee2755480f3c946bbf0ec865aa8af7d73abd15965b0424e95c1b4620d0"
        definition = WorkflowDefinition(self.request, version=24).to_dict()
        self.assertEqual(frozen_v24_sha256, self.definition_hash(definition))
        self.assertNotIn(CODER_REVIVAL_STAGE, {row["stage_id"] for row in definition["stages"]})
        self.assertNotIn("revival_policy", definition)
        self.assertNotIn(CODER_REVIVAL_STAGE, definition["agent_model_policy"]["stage_overrides"])

    def test_v25_adds_only_the_planner_mediated_revival_contract(self):
        self.assertEqual(29, WORKFLOW_VERSION)
        v24 = WorkflowDefinition(self.request, version=24).to_dict()
        v25 = WorkflowDefinition(self.request, version=25).to_dict()
        stage = next(row for row in v25["stages"] if row["stage_id"] == CODER_REVIVAL_STAGE)
        self.assertEqual({
            "stage_id": CODER_REVIVAL_STAGE,
            "handler_id": "modport.coder_revival_plan",
            "depends_on": [],
            "agent": True,
        }, stage)
        self.assertIn(CODER_REVIVAL_STAGE, AGENT_STAGES)
        self.assertNotIn(CODER_REVIVAL_STAGE, v25["main_stages"])
        self.assertEqual({
            "mode": "planner_requests",
            "trigger": "dependency_settled",
            "planner_stage": CODER_REVIVAL_STAGE,
            "per_task_attempt_limit": None,
            "reset_budget": False,
            "host_owns": ["run_identity", "stopped_state", "budgets", "deduplication"],
        }, v25["revival_policy"])
        self.assertEqual(
            {"model": "gpt-6-sol", "reasoning_effort": "high"},
            v25["agent_model_policy"]["stage_overrides"][CODER_REVIVAL_STAGE],
        )

        # Removing only the v25 additions reconstructs the v24 snapshot exactly.
        v25_without_revival = json.loads(json.dumps(v25))
        v25_without_revival["workflow_version"] = 24
        v25_without_revival.pop("revival_policy")
        v25_without_revival["agent_model_policy"]["stage_overrides"].pop(CODER_REVIVAL_STAGE)
        v25_without_revival["stages"] = [
            row for row in v25_without_revival["stages"]
            if row["stage_id"] != CODER_REVIVAL_STAGE
        ]
        self.assertEqual(v24, v25_without_revival)

    def test_revival_planner_model_is_new_only_in_v25(self):
        self.assertEqual(
            (DEFAULT_AGENT_MODEL, DEFAULT_REASONING_EFFORT),
            agent_model_policy(24, CODER_REVIVAL_STAGE),
        )
        self.assertEqual(
            ("gpt-6-sol", "high"),
            agent_model_policy(25, CODER_REVIVAL_STAGE),
        )
        for version in (23, 24, 25):
            for stage in PLANNER_STAGES:
                with self.subTest(version=version, stage=stage):
                    self.assertEqual(("gpt-6-sol", "high"), agent_model_policy(version, stage))

    def test_explicit_upgrade_accepts_exact_v24_and_builds_current(self):
        from modport.workflow_upgrade import validate_upgrade_definition

        source = WorkflowDefinition(self.request, version=24).to_dict()
        target = validate_upgrade_definition({"request": self.request, "definition": source})
        self.assertEqual(WORKFLOW_VERSION, target["workflow_version"])
        self.assertEqual(CODER_REVIVAL_STAGE, target["revival_policy"]["planner_stage"])
        self.assertEqual(24, source["workflow_version"])
        self.assertNotIn("revival_policy", source)

        altered = json.loads(json.dumps(source))
        altered["agent_backend_policy"]["version"] = "1.18.31"
        with self.assertRaisesRegex(ValueError, "exact supported upgrade source"):
            validate_upgrade_definition({"request": self.request, "definition": altered})

    def test_explicit_upgrade_accepts_exact_v27_compile_package_definition(self):
        from modport.workflow_upgrade import validate_upgrade_definition
        from modport.models import MigrationRequest

        request = MigrationRequest.from_mapping({**self.request,
                                                 'validation_scope': 'compile_package'}).to_dict()
        frozen = WorkflowDefinition(request, version=27).to_dict()
        upgraded = validate_upgrade_definition({'request': request, 'definition': frozen})
        self.assertEqual(WORKFLOW_VERSION, upgraded['workflow_version'])
        self.assertIn('contract_review', frozen['early_stages'])
        self.assertNotIn('contract_review', upgraded['early_stages'])


if __name__ == "__main__":
    unittest.main()
