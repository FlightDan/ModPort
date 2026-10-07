from copy import deepcopy
from pathlib import Path
import tempfile
import unittest

from modport.analysis_stages import validate_analysis
from modport.evidence import atomic_json, read_json
from modport.project_gaps import gap_id, project_gap_rows, write_gap_files


class ProjectGapTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "Source.java").write_text("class Source { Input event; }\n")
        self.scan = {"skills": {"platform": {"report": {"findings": [], "known_gaps": [{"id": "gap.input"}]}}}}
        self.row = {"entry_id": "gap.input", "gap_id": "knowledge:gap.input", "kind": "knowledge",
                    "status": "unresolved", "applicable": True, "resolution_stage": "mod_analysis",
                    "closure_criteria": ["Verify exact input API signature"], "affected_tasks": ["input"],
                    "question": "Which target input signature preserves event behavior?", "existing_answer": "Source uses Input",
                    "missing_information": "Target declaration", "evidence": ["Source.java:1"],
                    "usage_locations": [{"path": "Source.java", "line": 1, "symbol": "Input"}]}

    def validate(self, row=None):
        return validate_analysis({"schema_version": 2, "candidates": [], "gap_assessments": [row or self.row]},
                                 self.scan, self.root, strict=True)

    def test_entry_identity_survives_reordering(self):
        self.assertEqual(self.validate(), [self.row])
        self.assertEqual(gap_id(self.row), "knowledge:gap.input")
        self.row["gap_id"] = "knowledge:wrong"
        with self.assertRaisesRegex(ValueError, "kind:entry_id"):
            self.validate()

    def test_references_must_exist_and_symbol_must_match_line(self):
        for location in ({"path": "Source.java", "line": 2}, {"path": "../Source.java", "line": 1},
                         {"path": "Source.java", "line": 1, "symbol": "Menu"}):
            with self.subTest(location=location), self.assertRaises(ValueError):
                self.validate({**self.row, "usage_locations": [location]})

    def test_absence_needs_scope_and_cannot_hide_actual_use_or_dispute(self):
        absent = {**self.row, "applicable": False, "status": "not_applicable", "usage_locations": []}
        with self.assertRaisesRegex(ValueError, "checked_scope"):
            self.validate(absent)
        absent["checked_scope"] = ["Source.java: all input consumers"]
        self.assertEqual(self.validate(absent), [])
        for changes in ({"used": True}, {"usage_locations": self.row["usage_locations"]},
                        {"applicability_disputed": True}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.validate({**absent, **changes})

    def test_root_license_header_issue_does_not_trigger_research(self):
        self.row["issue_type"] = "license_header_conflict"
        self.row["root_license"] = {"path": "LICENSE"}
        self.assertEqual(self.validate(), [])
        self.assertEqual(project_gap_rows([self.row]), ([], []))

    def test_projection_is_host_owned_and_pending_does_not_pass(self):
        research, verification = project_gap_rows([self.row, {**self.row, "kind": "verification", "gap_id": "verification:gap.input"}])
        self.assertEqual(verification[0]["verification_status"], "pending")
        app = {"project_research_gaps": {research[0]["gap_id"]: research[0]},
               "project_verification_gaps": {verification[0]["gap_id"]: verification[0]}}
        before = deepcopy(app)
        write_gap_files(self.root, app, "run")
        atomic_json(self.root / "artifacts/research-gaps.json", {"gaps": [{"project_status": "resolved"}]})
        write_gap_files(self.root, app, "run")
        self.assertEqual(app, before)
        self.assertEqual(read_json(self.root / "artifacts/research-gaps.json")["gaps"][0]["project_status"], "unresolved")

    def test_new_project_questions_and_known_answers_can_add_fine_grained_gaps(self):
        optional = {**self.row, 'entry_id': 'rule.input.signature', 'gap_id': 'knowledge:rule.input.signature'}
        self.scan['skills']['platform']['report']['knowledge_entries'] = [{'id': 'rule.input.signature'}]
        data = {'schema_version': 2, 'candidates': [], 'gap_assessments': [self.row, optional]}
        self.assertEqual(len(validate_analysis(data, self.scan, self.root, strict=True)), 2)
        optional.update(entry_id='project.custom_input', gap_id='knowledge:project.custom_input', new_entry=True)
        self.assertEqual(len(validate_analysis(data, self.scan, self.root, strict=True)), 2)
        del optional['new_entry']
        with self.assertRaises(ValueError):
            validate_analysis(data, self.scan, self.root, strict=True)

    def test_composite_gap_does_not_make_menu_and_chat_used_when_only_input_is_used(self):
        self.scan['skills']['platform']['report']['known_gaps'] = ['menu/input/chat protocol details']
        rows = []
        for topic in ('menu', 'input', 'chat'):
            row = {**self.row, 'entry_id': 'gap.' + topic, 'gap_id': 'knowledge:gap.' + topic}
            if topic != 'input':
                row.update(status='not_applicable', applicable=False, usage_locations=[], checked_scope=['Source.java'])
            rows.append(row)
        data = {'schema_version': 2, 'candidates': [], 'gap_assessments': rows}
        self.assertEqual(validate_analysis(data, self.scan, self.root, strict=True), [rows[1]])
