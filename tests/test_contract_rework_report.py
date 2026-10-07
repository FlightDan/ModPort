"""The current handoff must keep an inherited contract edited by its author."""

import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from modport.contracts import OperationInput
from modport.report_dialogue import materialize_report, prepare_dialogue


class InheritedContractReworkReportTests(unittest.TestCase):
    def test_rework_prompt_and_consumer_keep_complete_agent_file(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            worktree = root / "worktree"
            path = worktree / ".modport/functional-contract.json"
            path.parent.mkdir(parents=True)
            inherited = {
                "schema_version": 1, "contract_id": "contract-1",
                "source_fingerprint": "source-1", "metadata": {"owner": "baseline"},
                "behaviors": [{"id": "behavior-1", "behavior_source": "source-1"}],
            }
            repaired = {**inherited, "behaviors": [
                {"id": "behavior-1", "behavior_source": "source-2"}]}
            for stage in ("contract_draft", "contract_revise"):
                with self.subTest(stage=stage):
                    path.write_text(json.dumps(inherited), encoding="utf-8")
                    command = OperationInput(
                        "run-1", "task-1", stage, stage, str(root),
                        payload={"reviewer_rework": {"request_id": "rework-1"}},
                        options={"workflow_version": 25},
                        artifact_refs={"inherited_harness": {"path": "baseline"}},
                    )
                    dialogue = prepare_dialogue(command, root, "Repair the source link.",
                        required_paths=(".modport/functional-contract.json",))
                    self.assertIsNone(dialogue["schema_path"])
                    self.assertIsNone(dialogue["contract"]["output_path"])
                    self.assertIn("Edit the existing .modport/functional-contract.json in place",
                                  dialogue["execution_task"])
                    path.write_text(json.dumps(repaired), encoding="utf-8")
                    self.assertEqual([], materialize_report(dialogue, worktree,
                        "Updated the behavior source link."))
                    self.assertEqual(repaired, json.loads(path.read_text(encoding="utf-8")))
                    self.assertEqual("Updated the behavior source link.",
                        (dialogue["directory"] / "final-report.txt").read_text(encoding="utf-8"))
                    self.assertFalse((dialogue["directory"] / "agent-written" /
                                      ".modport/functional-contract.json").exists())

    def test_fresh_and_frozen_draft_keep_characterization_protocol(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            for version, rework in ((25, False), (20, True)):
                with self.subTest(version=version, rework=rework):
                    command = OperationInput(
                        "run-1", "task-1", "contract_draft", "draft-1", str(root),
                        payload={"reviewer_rework": {"request_id": "rework-1"}} if rework else {},
                        options={"workflow_version": version},
                        artifact_refs={"inherited_harness": {"path": "baseline"}},
                    )
                    contract = prepare_dialogue(command, root, "Draft the contract.")["contract"]
                    self.assertEqual(".modport/functional-contract.json", contract["output_path"])
                    self.assertEqual("characterization", contract["transform"])
                    self.assertIsInstance(contract["schema"], dict)


if __name__ == "__main__":
    unittest.main()
