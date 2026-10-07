"""Current producer/publication/consumer and display-routing regressions."""
from copy import deepcopy
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest

from modport.author_contracts import characterization_evidence_schema
from modport.client_harness import requires_client_display
from modport.handlers import _test_evidence_declarations, _validated_characterization_contract
from modport.opencode_shell_mcp import AssertionIdentityError, _dynamic_contract_session
from modport.report_dialogue import materialize_report, prepare_dialogue
from modport.report_schemas import report_contract
from modport.rubric import acceptance_rubric
from modport.test_selection_execution import build_selected_test_execution


def command(*, rework=False):
    return SimpleNamespace(
        stage_id="contract_draft", command_id="current-draft", task_id="contract_draft",
        options={"workflow_version": 31}, artifact_refs={},
        payload={"reviewer_rework": {"request_id": "repair"}} if rework else {},
    )


class CharacterizationHandoffTests(unittest.TestCase):
    def test_current_wire_publication_reaches_selected_execution_with_all_bindings(self):
        protocol = characterization_evidence_schema(workflow_version=31)
        contract = deepcopy(protocol["examples"]["contract"])
        contract["contract_id"] = "current-characterization"
        wire = deepcopy(contract)
        wire["test_evidence"] = [
            {"test_id": test_id, "declaration": declaration}
            for test_id, declaration in contract["test_evidence"].items()
        ]
        with TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "baseline"
            workspace.mkdir()
            authored_path = workspace / ".modport/functional-contract.json"
            authored_path.parent.mkdir()
            authored_path.write_text(json.dumps(wire))
            dynamic = _dynamic_contract_session({
                "dynamic_contract": True, "root": str(root),
                "contract_path": "baseline/.modport/functional-contract.json",
            }, ["example.test"], "selected")
            self.assertEqual(contract["test_evidence"]["example.test"]["result_identity"],
                             dynamic["test_cases"]["example.test"]["result_identity"])
            self.assertEqual(["example.result"], dynamic["test_cases"]["example.test"]["assertion_ids"])
            self.assertEqual(wire, json.loads(authored_path.read_text()))
            dialogue = prepare_dialogue(command(), root, "Author the original suite.")
            schema = json.loads(dialogue["schema_path"].read_text())
            behavior_shape = schema["properties"]["behaviors"]["items"]
            declaration_shape = schema["properties"]["test_evidence"]["items"]["properties"]["declaration"]
            self.assertIn("assertion_contracts", behavior_shape["required"])
            self.assertEqual(protocol["assertion_contract_schema"],
                             behavior_shape["properties"]["assertion_contracts"]["items"])
            self.assertIn("result_identity", declaration_shape["required"])
            self.assertEqual(protocol["result_identity_schema"], declaration_shape["properties"]["result_identity"])
            self.assertEqual({"const": "junit"}, declaration_shape["properties"]["executor"])
            self.assertEqual([], materialize_report(dialogue, workspace, json.dumps(wire)))
            consumed = json.loads((workspace / ".modport/functional-contract.json").read_text())
        self.assertEqual(contract, consumed)
        typed = _validated_characterization_contract(consumed, workflow_version=31)
        self.assertEqual("example.result", typed.entries[0].assertion_contracts[0].assertion_id)
        rubric = acceptance_rubric()
        consumed.update(rubric_id=rubric["rubric_id"], rubric_version=rubric["rubric_version"])
        declarations = _test_evidence_declarations(consumed, rubric, workflow_version=31,
                                                   gradle_tasks=consumed["baseline_gradle_tasks"])
        selected = build_selected_test_execution(consumed, list(declarations))
        self.assertEqual((":test",), selected.gradle_tasks)
        self.assertIn("example.ExampleTest.returnsExpectedValue", selected.gradle_init_script)

    def test_rework_without_handoff_ref_keeps_repaired_file_and_archives_reply(self):
        repaired = deepcopy(characterization_evidence_schema(workflow_version=31)["examples"]["contract"])
        repaired["provenance"] = {"historical_assertion": "retained original observation"}
        with TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "baseline"
            path = workspace / ".modport/functional-contract.json"
            path.parent.mkdir(parents=True)
            path.write_text(json.dumps(repaired))
            dialogue = prepare_dialogue(command(rework=True), root, "Repair the parser.")
            self.assertIsNone(dialogue["contract"]["output_path"])
            self.assertIsNone(dialogue["schema_path"])
            reply = "Parser repaired; incomplete cases remain unverified."
            self.assertEqual([], materialize_report(dialogue, workspace, reply))
            self.assertEqual(repaired, json.loads(path.read_text()))
            self.assertEqual(reply, (dialogue["directory"] / "final-report.txt").read_text())

    def test_rework_wire_file_normalization_preserves_all_fields_and_original_archive(self):
        repaired = deepcopy(characterization_evidence_schema(workflow_version=31)["examples"]["contract"])
        repaired["provenance"] = {"original_observation": "preserved"}
        wire = deepcopy(repaired)
        wire["test_evidence"] = [{"test_id": k, "declaration": v}
                                 for k, v in repaired["test_evidence"].items()]
        with TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "baseline"
            path = workspace / ".modport/functional-contract.json"
            path.parent.mkdir(parents=True)
            path.write_text(json.dumps(wire))
            dialogue = prepare_dialogue(command(rework=True), root, "Repair the parser.")
            self.assertEqual([], materialize_report(dialogue, workspace, "Repair summary."))
            self.assertEqual(repaired, json.loads(path.read_text()))
            archive = dialogue["directory"] / "agent-written/.modport/functional-contract.json"
            self.assertEqual(wire, json.loads(archive.read_text()))

    def test_dynamic_wire_decode_still_rejects_missing_and_duplicate_identities(self):
        contract = deepcopy(characterization_evidence_schema(workflow_version=31)["examples"]["contract"])
        wire = deepcopy(contract)
        wire["test_evidence"] = [{"test_id": k, "declaration": v}
                                 for k, v in contract["test_evidence"].items()]
        with TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "baseline/.modport/functional-contract.json"
            path.parent.mkdir(parents=True)
            session = {"dynamic_contract": True, "root": str(root),
                       "contract_path": "baseline/.modport/functional-contract.json"}
            wire["test_evidence"][0]["declaration"].pop("result_identity")
            path.write_text(json.dumps(wire))
            with self.assertRaisesRegex(AssertionIdentityError, "no exact safe JUnit identity"):
                _dynamic_contract_session(session, ["example.test"], "selected")
            wire["test_evidence"].append(deepcopy(wire["test_evidence"][0]))
            path.write_text(json.dumps(wire))
            with self.assertRaisesRegex(AssertionIdentityError, "duplicate test_id"):
                _dynamic_contract_session(session, ["example.test"], "selected")

    def test_display_routing_survives_invalid_declarations_and_hidden_client_dependency(self):
        invalid = {"behaviors": [{"id": "startup", "side": "client"}],
                   "test_evidence": {"startup": {"executor": "junit"}}}
        self.assertTrue(requires_client_display(invalid, ["test"]))
        server = {"behaviors": [{"id": "stats", "side": "server"}]}
        self.assertTrue(requires_client_display(server, ["test"],
                                               ":client:runClient SKIPPED\n:test SKIPPED\n"))
        self.assertFalse(requires_client_display(server, ["test"],
                                                "diagnostic mentioning :runClient SKIPPED\n:test SKIPPED\n"))
        self.assertFalse(requires_client_display(server, ["runServer"], ":runServer SKIPPED\n"))
        self.assertTrue(requires_client_display(server, [":client:runClient"]))


if __name__ == "__main__":
    unittest.main()
