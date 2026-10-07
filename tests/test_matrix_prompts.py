"""v31 prompts preserve baseline evidence and carry the shared test matrix protocol."""
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from modport.contracts import OperationInput
from modport.prompts import STAGE_PROMPTS, build_prompt
from modport.report_dialogue import phase_command, prepare_dialogue


class MatrixPromptTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def command(self, stage):
        return OperationInput(
            "run", "task-" + stage, stage, "execution-" + stage, str(self.root),
            options={
                "workflow_version": 31,
                "validation_policy": {"scope": "compile_package"},
                "agent_dialogue_policy": {"version": 1, "turns": ["plan", "execute"]},
            },
        )

    def packet(self, stage, phase):
        path = (self.root / "artifacts" / "executions" / ("execution-" + stage) /
                f"task-instructions.{phase}.json")
        return json.loads(path.read_text(encoding="utf-8"))

    def test_v31_baseline_author_prompt_carries_protocol_and_allows_source_execution(self):
        command = self.command("contract_draft")
        dialogue = prepare_dialogue(command, self.root, STAGE_PROMPTS["contract_draft"])
        plan_prompt = build_prompt(dialogue["planning_task"],
                                   phase_command(command, "plan"), self.root, {}, {})
        execute_prompt = build_prompt(dialogue["execution_task"],
                                      phase_command(command, "execute"), self.root, {}, {})
        plan = self.packet("contract_draft", "plan")
        execute = self.packet("contract_draft", "execute")
        protocol_path = (self.root / "artifacts" / "executions" / "execution-contract_draft" /
                         "test-matrix-protocol.json")
        protocol = json.loads(protocol_path.read_text(encoding="utf-8"))

        self.assertIn("Workflow v31 shared test-matrix protocol", plan["task"])
        self.assertIn("task-instructions.plan.json", plan_prompt)
        self.assertIn("task-instructions.execute.json", execute_prompt)
        self.assertIn("Workflow v31 contract requirements", execute["task"])
        self.assertIn("MODPORT_SELECTED_TEST_IDS", execute["task"])
        self.assertIn("heuristically", execute["task"])
        self.assertIn("mocked item logic", execute["task"])
        self.assertIn("baseline harness runs its discovery suite", execute["task"])
        self.assertIn("runs its discovery suite", execute["task"])
        self.assertNotIn("Do not launch, author, repair, or require baseline runtime/game tests",
                         execute_prompt)
        self.assertIn("Target GameTests", execute_prompt)
        self.assertIn("target acceptance", execute_prompt)
        self.assertIn("test-matrix-protocol.json", plan["task"])
        self.assertIn("test-matrix-protocol.json", execute["task"])
        self.assertIn("matrix_schema", protocol)
        self.assertIn("assessment_schema", protocol)
        self.assertIn("guidance", protocol)

    def test_v31_review_plan_and_execute_keep_assessment_separate_and_tool_rework_explicit(self):
        command = self.command("contract_review")
        dialogue = prepare_dialogue(command, self.root, STAGE_PROMPTS["contract_review"])
        plan_prompt = build_prompt(dialogue["planning_task"],
                                   phase_command(command, "plan"), self.root, {}, {})
        execute_prompt = build_prompt(dialogue["execution_task"],
                                      phase_command(command, "execute"), self.root, {}, {})
        plan = self.packet("contract_review", "plan")
        execute = self.packet("contract_review", "execute")

        self.assertIn("completed original-mod execution evidence", plan["task"])
        self.assertIn("do not perform the review", plan["task"])
        self.assertIn(".modport/test-assessment.json", execute["task"])
        self.assertIn(".modport/contract-review.json", execute["task"])
        self.assertIn("diagnostic outputs rather than downstream approval gates", execute["task"])
        self.assertIn("actual list_rework_targets and request_rework calls", execute["task"])
        self.assertIn("Cases without observed execution", execute["task"])
        self.assertIn("separate report", execute["task"])
        self.assertIn("intended-behavior assertion fails", execute["task"])
        self.assertIn("reproduction assertion passes", execute["task"])
        self.assertIn("defect_assertion_ids", execute["task"])
        self.assertIn("request_rework", dialogue["execution_task"])
        self.assertIn("task-instructions.plan.json", plan_prompt)

    def test_v31_target_prompt_uses_locked_selection_and_preserves_source_defects(self):
        command = self.command("implementation")
        prompt = build_prompt(STAGE_PROMPTS["implementation"], command,
                              self.root, {}, {})
        packet = json.loads((self.root / "artifacts" / "executions" /
                             "execution-implementation" / "task-instructions.json"
                             ).read_text(encoding="utf-8"))

        for required in (
            "functional_contract_lock.contract", "source_contract", "source_defects",
            "uncovered_assertion_ids", "MODPORT_SELECTED_TEST_IDS",
            "failed, skipped and missing", "Do not weaken target assertions",
        ):
            with self.subTest(required=required):
                self.assertIn(required, packet["task"])
        self.assertIn("Read task-instructions.json", prompt)

if __name__ == "__main__":
    unittest.main()
