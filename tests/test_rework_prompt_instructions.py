"""Agent prompts make explicit rework calls only during execution."""
from dataclasses import replace
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from modport.contracts import OperationInput
from modport.prompts import STAGE_PROMPTS, build_prompt
from modport.report_dialogue import phase_command, prepare_dialogue
from modport.rework_tools import (is_interactive_review, opencode_tool_config,
                                  prepare_session, rework_targets, tool_prompt)


TARGET = {"target_agent": "coder-task", "execution_id": "coder-execution",
          "stage": "coder", "description": "Repair the adapter"}


class ReworkPromptInstructionTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def command(self, targets, *, phase=None):
        options = {"workflow_version": 17,
                   "agent_dialogue_policy": {"version": 1,
                                             "turns": ["plan", "execute"]}}
        if phase is not None:
            options["dialogue_phase"] = phase
        return OperationInput("run", "review-task", "code_review", "review-execution",
            str(self.root), payload={"review_rework_targets": targets}, options=options)

    def packet(self, phase):
        path = self.root / ("artifacts/executions/review-execution/"
                            "task-instructions." + phase + ".json")
        return json.loads(path.read_text())

    def test_v28_handoff_prompt_defers_behavior_evidence_and_binds_source_identity(self):
        refs = {'artifact_handoff': {'path': 'artifacts/handoff.json', 'sha256': 'a' * 64},
                'inherited_harness': {'path': 'artifacts/harness.json', 'sha256': 'b' * 64}}
        base = OperationInput('run', 'background', 'background', 'background-execution',
            str(self.root), options={'workflow_version': 28,
                'validation_policy': {'scope': 'compile_package'}}, artifact_refs=refs)
        current = build_prompt(STAGE_PROMPTS['background'], base, self.root, {}, {})
        task_path = self.root / 'artifacts/executions/background-execution/task-instructions.json'
        task = json.loads(task_path.read_text())['task']
        self.assertIn('The selected source and contract identities must reach', task)
        self.assertIn('Independent contract review is deferred', task)
        self.assertIn('recorded behavior assertions remain deferred', task)
        self.assertIn('baseline runtime/game tests', current)
        build_prompt(STAGE_PROMPTS['background'], replace(base,
            options={'workflow_version': 27,
                     'validation_policy': {'scope': 'compile_package'}}), self.root, {}, {})
        legacy_task = json.loads(task_path.read_text())['task']
        self.assertEqual(STAGE_PROMPTS['background'], legacy_task)

    def test_two_turn_archive_prohibits_plan_calls_and_requires_real_execution_calls(self):
        command = self.command([TARGET])
        dialogue = prepare_dialogue(command, self.root, "Review the current candidate.")
        plan_command = phase_command(command, "plan")
        execute_command = phase_command(command, "execute")

        plan_prompt = build_prompt(dialogue["planning_task"], plan_command,
                                   self.root, {}, {})
        execute_prompt = build_prompt(dialogue["execution_task"], execute_command,
                                      self.root, {}, {})
        plan_packet = self.packet("plan")
        execute_packet = self.packet("execute")

        self.assertIn("do not perform the review", plan_packet["task"])
        self.assertIn("Do not call list_rework_targets or request_rework",
                      plan_packet["tool_instructions"])
        self.assertIn("must not call upstream rework tools", plan_prompt)
        self.assertNotIn("first call list_rework_targets, then call request_rework",
                         plan_packet["tool_instructions"])

        instructions = execute_packet["tool_instructions"]
        self.assertIn("finds concrete defects in upstream work that require correction", instructions)
        self.assertIn("first call list_rework_targets, then call request_rework", instructions)
        self.assertIn("explicit reviewer decision", instructions)
        self.assertIn("do not issue a final answer while that call is still in progress",
                      instructions)
        self.assertIn("explicit session-deadline error", instructions)
        self.assertIn("Read that returned result", instructions)
        self.assertIn("updated artifacts", instructions)
        self.assertIn("prose does not invoke the author", instructions)
        self.assertIn("not an automatic response to a rejected report", instructions)
        self.assertNotIn("before you can finish this assignment", instructions)
        self.assertIn('"target_agent": "coder-task"', instructions)
        self.assertIn("make the actual list_rework_targets and request_rework calls",
                      execute_packet["task"])
        self.assertIn("make the explicit list_rework_targets and request_rework calls",
                      execute_prompt)

    def test_no_targets_are_reported_as_unavailable_in_prompt_and_archive(self):
        command = self.command([])
        dialogue = prepare_dialogue(command, self.root, "Review the current candidate.")
        plan_prompt = build_prompt(dialogue["planning_task"], phase_command(command, "plan"),
                                   self.root, {}, {})
        execute_prompt = build_prompt(dialogue["execution_task"],
                                      phase_command(command, "execute"), self.root, {}, {})

        for phase in ("plan", "execute"):
            instructions = self.packet(phase)["tool_instructions"]
            self.assertIn("No upstream rework targets are available", instructions)
            self.assertIn("cannot be used", instructions)
            self.assertIn("do not claim", instructions.lower())
            self.assertNotIn("You have the modport_rework MCP tools", instructions)
            self.assertNotIn("Allowed targets:", instructions)

        self.assertIn("must not call upstream rework tools", plan_prompt)
        self.assertIn("No callable upstream rework target is available", execute_prompt)
        self.assertIn("no targets are listed", self.packet("execute")["task"])
        self.assertIn("preserve the failure", self.packet("execute")["task"])

    def test_single_turn_prompt_with_targets_contains_the_same_explicit_workflow(self):
        command = self.command([TARGET])
        plain = replace(command, options={"workflow_version": 17})
        prompt = build_prompt("Review the current candidate.", plain, self.root, {}, {})
        packet = json.loads((self.root / "artifacts/executions/review-execution/"
                             "task-instructions.json").read_text())

        self.assertIn("During this assignment", packet["tool_instructions"])
        self.assertIn("first call list_rework_targets, then call request_rework",
                      packet["tool_instructions"])
        self.assertIn("make the explicit list_rework_targets and request_rework calls", prompt)
        self.assertNotIn("must not call upstream rework tools", prompt)

    def test_tool_prompt_does_not_expose_nested_author_rework(self):
        command = self.command([TARGET])
        nested = replace(command, payload={**command.payload, "reviewer_rework": {
            "source_execution_id": "coder-execution"}})
        self.assertEqual("", tool_prompt(nested))

    def test_v25_revival_planner_cannot_open_rework_transport(self):
        command = OperationInput(
            "run", "revival-task", "coder_revival_plan", "revival-execution",
            str(self.root), payload={"review_rework_targets": [TARGET]},
            options={"workflow_version": 25, "gate_policy": "downstream_toolcall",
                     "agent_dialogue_policy": {"version": 1,
                                               "turns": ["plan", "execute"]}},
        )

        self.assertEqual([], rework_targets({"run_id": "run", "tasks": {}}, command))
        self.assertFalse(is_interactive_review(command))
        self.assertIsNone(prepare_session(command, self.root, 30))
        self.assertEqual({}, opencode_tool_config(None, 30))
        dialogue = prepare_dialogue(command, self.root, "Return the revival decision JSON.")
        self.assertNotIn("request_rework", dialogue["execution_task"])
        for phase in ("plan", "execute"):
            phase_input = phase_command(command, phase)
            self.assertEqual("", tool_prompt(phase_input))
            build_prompt(dialogue["planning_task" if phase == "plan" else "execution_task"],
                         phase_input, self.root, {}, {})
            packet = json.loads((self.root / "artifacts/executions/revival-execution/"
                                 f"task-instructions.{phase}.json").read_text())
            self.assertNotIn("request_rework", packet["task"])
            self.assertNotIn("request_rework", packet["tool_instructions"])
        self.assertFalse((self.root / "artifacts/rework-tools/revival-execution").exists())

    def test_contract_rework_packet_assigns_fresh_witness_to_host_verifier(self):
        command = OperationInput(
            "run", "contract-task", "contract_draft", "contract-execution",
            str(self.root),
            payload={"reviewer_rework": {
                "source_execution_id": "earlier-contract-execution",
                "instructions": "Add difficulty assertions; verify with fresh runClient witnesses.",
            }},
            options={"workflow_version": 26, "agent_dialogue_policy": {
                "version": 1, "turns": ["plan", "execute"]}},
        )
        dialogue = prepare_dialogue(command, self.root, STAGE_PROMPTS["contract_draft"])
        build_prompt(dialogue["execution_task"], phase_command(command, "execute"),
                     self.root, {}, {})
        packet = json.loads((self.root / "artifacts/executions/contract-execution/"
                             "task-instructions.execute.json").read_text())

        self.assertIn("subsequent deterministic contract verifier", packet["task"])
        self.assertIn("not to a free-form modport_sandbox_run_project_command", packet["task"])
        self.assertIn("let the host verifier run and record it", packet["task"])
        self.assertIn("Add difficulty assertions; verify with fresh runClient witnesses.",
                      packet["tool_instructions"])
        self.assertIn("The host runs the fresh nonce-bound contract verifier",
                      packet["tool_instructions"])
        self.assertIn("without that verifier wiring", packet["tool_instructions"])


if __name__ == "__main__":
    unittest.main()
