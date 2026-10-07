"""Independent model settings on the current workflow."""
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from modport.contracts import OperationInput
from modport.handlers import _agent_model_policy
from modport.prompt_compressor import PromptCompressor
from modport.workflow import (DEFAULT_AGENT_MODEL, DEFAULT_REASONING_EFFORT,
                              V31_PLANNER_STAGES, WORKFLOW_VERSION,
                              WorkflowDefinition, agent_model_policy)


class ModelPolicyTests(unittest.TestCase):
    @staticmethod
    def request():
        from modport.models import MigrationRequest
        return MigrationRequest("example", "https://example.invalid/mod.git",
                                "1.20.1", "26.1.2").to_dict()

    def command(self, **options):
        return OperationInput(
            "run", "task", "stage", "run:task:1", "/tmp/run",
            options=options,
        )

    def test_current_run_cannot_override_its_versioned_agent_policy(self):
        self.assertEqual(33, WORKFLOW_VERSION)
        options = {
            "model": "gpt-6-astra",
            "reasoning_effort": "low",
            "workflow_version": WORKFLOW_VERSION,
        }
        self.assertEqual(agent_model_policy(WORKFLOW_VERSION),
                         _agent_model_policy(self.command(**options)))

    def test_command_without_version_uses_current_uniform_defaults(self):
        self.assertEqual(
            (DEFAULT_AGENT_MODEL, DEFAULT_REASONING_EFFORT),
            _agent_model_policy(self.command()),
        )

    def test_environment_cannot_override_summary_model_policy(self):
        with patch.dict("os.environ", {
            "MODPORT_PROMPT_SUMMARY_MODEL": "gpt-6-astra",
            "MODPORT_PROMPT_SUMMARY_REASONING": "low",
        }):
            compressor = PromptCompressor.from_environment(summary_backend=object())
        self.assertEqual(DEFAULT_AGENT_MODEL, compressor.summary_model)
        self.assertEqual(DEFAULT_REASONING_EFFORT, compressor.summary_reasoning)

    def test_carried_v33_policy_remains_frozen_without_independent_settings(self):
        self.assertEqual(("gpt-6.1-sol", "high"), agent_model_policy(33, "supervisor"))
        self.assertEqual((DEFAULT_AGENT_MODEL, DEFAULT_REASONING_EFFORT),
                         agent_model_policy(33, "coder"))
        v33 = WorkflowDefinition(self.request(), version=33).to_dict()
        self.assertNotIn("supervisor", v33["agent_model_policy"]["stage_overrides"])
        self.assertNotIn("coder", v33["agent_model_policy"]["stage_overrides"])
        self.assertEqual({stage: {"model": "gpt-6.1-sol", "reasoning_effort": "high"}
                          for stage in V31_PLANNER_STAGES},
                         v33["agent_model_policy"]["stage_overrides"])

        from modport.model_policy import load_model_config, ROLE_STAGES, resolve_model
        policy = load_model_config()
        for stage in set().union(*ROLE_STAGES.values()) | {"goal_prepare", "code_review"}:
            with self.subTest(stage=stage):
                command = replace(self.command(workflow_version=WORKFLOW_VERSION,
                    model="ignored", reasoning_effort="low", model_policy=policy), stage_id=stage)
                self.assertEqual(resolve_model(policy, stage), _agent_model_policy(command))

    def test_execution_plan_defaults_and_validation_use_selected_coder_policy(self):
        from modport.development import validate_plan
        from modport.execution_plan import normalize_execution_plan

        from modport.model_policy import load_model_config
        policy = load_model_config()
        report = {"tasks": [{"id": "fix", "objective": "Update the implementation",
                              "dependencies": [], "owned_paths": ["src/example/"],
                              "complexity": "simple"}]}
        plan = normalize_execution_plan(report, base_commit="a" * 40,
                                        workflow_version=WORKFLOW_VERSION, model_policy=policy)
        self.assertEqual(("gpt-6.1-sol", "high"),
                         (plan["tasks"][0]["model"], plan["tasks"][0]["reasoning_effort"]))
        validated = validate_plan(plan, workflow_version=WORKFLOW_VERSION, model_policy=policy)
        self.assertEqual("gpt-6.1-sol", validated["tasks"][0]["model"])
        unversioned = normalize_execution_plan(report, base_commit="a" * 40)
        self.assertEqual((DEFAULT_AGENT_MODEL, DEFAULT_REASONING_EFFORT),
                         (unversioned["tasks"][0]["model"],
                          unversioned["tasks"][0]["reasoning_effort"]))

    def test_current_submission_dispatches_versioned_role_policy_to_worker(self):
        from modport.models import MigrationRequest
        from modport.operations import MigrationOperations
        from modport.payload_storage import unpack_input

        with tempfile.TemporaryDirectory() as raw:
            operations = MigrationOperations()
            run = operations.submit(MigrationRequest("example", "https://example.invalid/mod.git",
                "1.20.1", "26.1.2"), run_dir=Path(raw) / "run")
            header = run.snapshot["input"]
            self.assertEqual(WORKFLOW_VERSION, header["definition"]["workflow_version"])
            from modport.model_policy import ROLE_STAGES, resolve_model
            policy = header["model_policy"]
            self.assertEqual(WorkflowDefinition(header["request"]).to_dict(), header["definition"])
            for stage in (set().union(*ROLE_STAGES.values()) - {"prompt_summary"}) | {"code_review"}:
                with self.subTest(stage=stage):
                    decisions = operations._schedule(run.snapshot, header,
                        operations._new_application(), stage, dependencies=[],
                        extra_options={"model_policy": {"stale": True}, "model": "ignored"})
                    wire = next(item["command"]["payload"] for item in decisions
                                if item["kind"] == "add_task")
                    command = OperationInput.from_dict(unpack_input(run.run_dir, wire))
                    self.assertEqual(policy, command.options["model_policy"])
                    expected = resolve_model(policy, stage)
                    self.assertEqual(expected, (command.options["model"], command.options["reasoning_effort"]))
                    self.assertEqual(expected, _agent_model_policy(command))

    def test_current_workflow_uses_managed_agent_backend(self):
        from modport.models import MigrationRequest

        request = MigrationRequest("example", "https://example.invalid/mod.git",
                                   "1.20.1", "26.1.2").to_dict()
        definition = WorkflowDefinition(request, version=WORKFLOW_VERSION).to_dict()
        self.assertEqual(WORKFLOW_VERSION, definition["workflow_version"])
        self.assertEqual({
            "backend": "opencode", "version": "1.18.32",
            "transport": "managed_loopback_server", "model_provider": "openai",
        }, definition["agent_backend_policy"])

    def test_current_definition_remains_the_same_for_configuration_continuation(self):
        from modport.workflow_upgrade import validate_upgrade_definition
        source = WorkflowDefinition(self.request()).to_dict()
        target = validate_upgrade_definition({"request": self.request(), "definition": source})
        self.assertEqual(source, target)
        self.assertEqual(33, target["workflow_version"])


if __name__ == "__main__":
    unittest.main()
