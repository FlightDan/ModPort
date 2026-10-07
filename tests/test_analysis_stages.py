from pathlib import Path
import json
import tempfile
import unittest
from unittest.mock import patch

from modport.analysis_contract import CANDIDATE_STATUSES, GAP_STATUSES
from modport.contracts import OperationInput, OperationResult
from modport.evidence import atomic_json, file_digest
from modport.prompts import STAGE_PROMPTS

from modport.analysis_stages import AnalysisStageHandler, validate_analysis, validate_preparation


class AnalysisTests(unittest.TestCase):
    def test_preparation_malformed_wire_types_have_actionable_diagnostics(self):
        for document, diagnostic in (
            ([], "preparation: expected JSON object"),
            ({"schema_version": True}, "schema_version 1"),
            ({"schema_version": 1, "version_evidence": {}}, "version_evidence: expected array"),
            ({"schema_version": 1, "version_evidence": ["source line"]},
             r"version_evidence\[0\]: expected object"),
        ):
            with self.subTest(document=document), self.assertRaisesRegex(ValueError, diagnostic):
                validate_preparation(document, Path("unused"), {})

    def test_source_versions_require_matching_source_lines_and_request(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "gradle.properties").write_text("forge_version=47.0.1\njava_version=17\n")
            data = {"schema_version": 1, "source_loader_version": "47.0.1", "source_java": "17",
                    "version_evidence": [{"field": field, "value": value, "path": "gradle.properties", "line": line}
                        for field, value, line in (("source_loader_version", "47.0.1", 1), ("source_java", "17", 2))]}
            self.assertEqual(validate_preparation(data, root, {}), data)
            with self.assertRaisesRegex(ValueError, "conflicts"):
                validate_preparation(data, root, {"source_java": "21"})
            data["version_evidence"][0]["line"] = 2
            with self.assertRaisesRegex(ValueError, "matching"):
                validate_preparation(data, root, {})

    def test_triage_requires_all_candidates_and_keeps_knowledge_gaps_pending(self):
        scan = {"skills": {"platform": {"report": {"findings": [{}], "known_gaps": ["unknown behavior"]}},
                           "java": {"report": {"findings": [], "known_gaps": []}}}}
        gap = {"skill": "platform", "index": 0, "applicable": True, "status": "unresolved", "evidence": ["uses affected API"],
               "kind": "knowledge", "resolution_stage": "mod_analysis", "closure_criteria": ["pin the target declaration"],
               "affected_tasks": ["state migration"]}
        data = {"schema_version": 2, "candidates": [{"skill": "platform", "index": 0, "status": "candidate", "evidence": ["source line"]}],
                "gap_assessments": [gap]}
        self.assertEqual(validate_analysis(data, scan), [gap])
        gap["status"] = "not_applicable"
        with self.assertRaisesRegex(ValueError, "applicable"):
            validate_analysis(data, scan)
        gap["status"] = "resolved"
        self.assertEqual(validate_analysis(data, scan), [])
        data["candidates"] = []
        with self.assertRaisesRegex(ValueError, "omitted"):
            validate_analysis(data, scan)

    def test_candidate_vocabulary_and_precise_diagnostics(self):
        scan = {"skills": {"platform": {"report": {"findings": [{}], "known_gaps": []}}}}
        row = {"skill": "platform", "index": 0, "status": "confirmed", "evidence": ["build.gradle:111"]}
        data = {"schema_version": 2, "candidates": [row], "gap_assessments": []}
        for status in CANDIDATE_STATUSES:
            row["status"] = status
            self.assertEqual(validate_analysis(data, scan), [])
        self.assertIn(" | ".join(CANDIDATE_STATUSES), STAGE_PROMPTS["mod_analysis"])
        self.assertIn(" | ".join(GAP_STATUSES), STAGE_PROMPTS["mod_analysis"])
        for invalid in ("confirmed_change", None, [], {}):
            row["status"] = invalid
            with self.assertRaises(ValueError) as caught:
                validate_analysis(data, scan)
            self.assertIn("candidates[0] (skill='platform', index=0).status", str(caught.exception))
            self.assertIn(repr(invalid), str(caught.exception))
            self.assertIn("allowed: " + ", ".join(CANDIDATE_STATUSES), str(caught.exception))
        data["candidates"] = [None]
        with self.assertRaisesRegex(ValueError, "expected object"):
            validate_analysis(data, scan)

    def test_format_failure_retains_outputs_but_untrusted_scan_is_not_repairable(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / "baseline/.modport/mod-analysis.json"
            scan_path = root / "scan.json"
            atomic_json(scan_path, {"skills": {"platform": {"report": {"findings": [{}], "known_gaps": []}}}})
            atomic_json(output, {"schema_version": 2, "candidates": [
                {"skill": "platform", "index": 0, "status": "confirmed_change", "evidence": ["source line"]}],
                "gap_assessments": []})
            ref = {"path": "scan.json", "sha256": file_digest(scan_path)}
            command = OperationInput("test", "mod_analysis", "mod_analysis", "test:analysis:1", str(root),
                                     artifact_refs={"mod_scan_report": ref})
            outputs = {"artifact_refs": {"analysis-output": {"path": str(output.relative_to(root)), "sha256": file_digest(output)}}}
            result = OperationResult("completed", outputs=outputs)
            handler = AnalysisStageHandler("mod_analysis", (".modport/mod-analysis.json",))
            with patch("modport.handlers.CodexStageHandler") as agent:
                agent.return_value.return_value = result
                failed = handler(command)
                self.assertEqual(failed.error_code, "analysis_output_invalid")
                self.assertTrue(failed.outputs["format_repairable"])
                self.assertEqual(failed.outputs["artifact_refs"], outputs["artifact_refs"])
                self.assertIn("confirmed_change", failed.outputs["validation_error"])
                output.write_text('{"broken":')
                self.assertTrue(handler(command).outputs["format_repairable"])
                scan_path.write_text("tampered")
                self.assertFalse(handler(command).outputs.get("format_repairable", False))

    def test_v17_malformed_analysis_is_retained_as_unverified_raw_report(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / "baseline/.modport/mod-analysis.json"
            output.parent.mkdir(parents=True)
            output.write_text('{"schema_version":2,"candidates":[]}', encoding="utf-8")
            scan_path = root / "scan.json"
            atomic_json(scan_path, {"skills": {}})
            command = OperationInput("test", "mod_analysis", "mod_analysis", "test:analysis:v17", str(root),
                artifact_refs={"mod_scan_report": {"path": "scan.json", "sha256": file_digest(scan_path)}},
                options={"workflow_version": 17})
            agent_result = OperationResult("completed", outputs={"artifact_refs": {}})
            with patch("modport.handlers.CodexStageHandler") as agent:
                agent.return_value.return_value = agent_result
                result = AnalysisStageHandler("mod_analysis", (".modport/mod-analysis.json",))(command)
            self.assertEqual(result.status, "completed", result.detail)
            self.assertEqual(result.outputs["acceptance_status"], "unverified")
            self.assertIn('"candidates":[]', result.outputs["raw_report"])
            self.assertTrue(result.outputs["format_repairable"])

    def test_v17_host_candidates_survive_empty_agent_array(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            scan = {"schema_version": 1, "scan_complete": True, "skills": {"java": {
                "report": {"scan_complete": True, "findings": [{"rule_id": "old-api",
                    "path": "Example.java", "line": 4, "column": 9, "match": "OldApi"}],
                    "known_gaps": [], "knowledge_entries": []}}}}
            atomic_json(root / "scan.json", scan)
            output = root / "baseline/.modport/mod-analysis.json"
            atomic_json(output, {"schema_version": 2, "candidates": [], "gap_assessments": []})
            command = OperationInput("test", "mod_analysis", "mod_analysis", "test:analysis:host",
                str(root), artifact_refs={"mod_scan_report": {"path": "scan.json",
                    "sha256": file_digest(root / "scan.json")}}, options={"workflow_version": 17})
            with patch("modport.handlers.CodexStageHandler") as agent:
                agent.return_value.return_value = OperationResult("completed")
                result = AnalysisStageHandler("mod_analysis", (".modport/mod-analysis.json",))(command)

            self.assertEqual(result.status, "completed", result.detail)
            preserved = json.loads((root / result.outputs["artifact_refs"]["mod_analysis"]["path"]).read_text())
            self.assertEqual(len(preserved["candidates"]), 1)
            self.assertEqual(preserved["candidates"][0]["status"], "unknown")
            self.assertIn("Example.java:4:9", preserved["candidates"][0]["evidence"])

    def test_v17_distinguishes_complete_zero_hits_from_incomplete_scan(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / "baseline/.modport/mod-analysis.json"
            atomic_json(output, {"schema_version": 2, "candidates": [], "gap_assessments": []})
            handler = AnalysisStageHandler("mod_analysis", (".modport/mod-analysis.json",))
            with patch("modport.handlers.CodexStageHandler") as agent:
                agent.return_value.return_value = OperationResult("completed")
                for complete in (True, False):
                    with self.subTest(complete=complete):
                        scan = {"schema_version": 1, "scan_complete": complete, "skills": {}}
                        atomic_json(root / "scan.json", scan)
                        command = OperationInput("test", "mod_analysis", "mod_analysis",
                            "test:analysis:" + str(complete).lower(), str(root),
                            artifact_refs={"mod_scan_report": {"path": "scan.json",
                                "sha256": file_digest(root / "scan.json")}},
                            options={"workflow_version": 17})
                        result = handler(command)
                        self.assertEqual(result.status, "completed", result.detail)
                        self.assertIs(result.outputs["scan_complete"], complete)
                        if complete:
                            self.assertNotIn("acceptance_status", result.outputs)
                        else:
                            self.assertEqual(result.outputs["acceptance_status"], "unverified")
                            self.assertTrue(result.outputs["business_diagnostics"])

    def test_v17_unresolved_knowledge_is_diagnostic_not_a_downstream_stop(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "baseline/Source.java"
            source.parent.mkdir(parents=True)
            source.write_text("class Source { OldApi value; }\n")
            scan = {"schema_version": 1, "scan_complete": True, "skills": {"java": {
                "report": {"scan_complete": True, "findings": [],
                    "known_gaps": [{"id": "gap.api", "summary": "Unknown target behavior"}],
                    "knowledge_entries": []}}}}
            atomic_json(root / "scan.json", scan)
            gap = {"entry_id": "gap.api", "gap_id": "knowledge:java:gap.api", "skill": "java",
                "index": 0, "kind": "knowledge", "status": "unresolved", "applicable": True,
                "resolution_stage": "mod_analysis", "closure_criteria": ["Confirm target behavior"],
                "affected_tasks": ["port-api"], "question": "What replaces OldApi?",
                "existing_answer": "The source uses OldApi.", "missing_information": "Target contract",
                "evidence": ["Source.java:1"],
                "usage_locations": [{"path": "Source.java", "line": 1, "symbol": "OldApi"}]}
            atomic_json(root / "baseline/.modport/mod-analysis.json",
                        {"schema_version": 2, "candidates": [], "gap_assessments": [gap]})
            command = OperationInput("test", "mod_analysis", "mod_analysis", "test:analysis:gap",
                str(root), artifact_refs={"mod_scan_report": {"path": "scan.json",
                    "sha256": file_digest(root / "scan.json")}}, options={"workflow_version": 17})
            with patch("modport.handlers.CodexStageHandler") as agent:
                agent.return_value.return_value = OperationResult("completed")
                result = AnalysisStageHandler("mod_analysis", (".modport/mod-analysis.json",))(command)

            self.assertEqual(result.status, "completed", result.detail)
            self.assertIsNone(result.error_code)
            self.assertEqual(result.outputs["acceptance_status"], "unverified")
            self.assertEqual(result.outputs["unresolved_relevant_gaps"], [gap])

    def test_mod_analysis_rejects_wrong_scan_hash(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            atomic_json(root / "scan.json", {"schema_version": 1, "scan_complete": True, "skills": {}})
            command = OperationInput("test", "mod_analysis", "mod_analysis", "test:analysis:hash",
                str(root), artifact_refs={"mod_scan_report": {"path": "scan.json", "sha256": "0" * 64}},
                options={"workflow_version": 17})
            with patch("modport.handlers.CodexStageHandler") as agent:
                result = AnalysisStageHandler("mod_analysis", (".modport/mod-analysis.json",))(command)

            self.assertEqual(result.status, "failed")
            self.assertEqual(result.error_code, "analysis_output_invalid")
            agent.assert_not_called()

    def test_verification_is_deferred_without_claiming_it_resolved(self):
        scan = {"skills": {"platform": {"report": {"findings": [], "known_gaps": ["old saves"]}}}}
        gap = {"skill": "platform", "index": 0, "applicable": True, "status": "unresolved",
               "kind": "verification", "resolution_stage": "test_execute", "evidence": ["source writes player NBT"],
               "closure_criteria": ["reopen an upgraded source save twice and compare fields"],
               "affected_tasks": ["state migration"]}
        data = {"schema_version": 2, "candidates": [], "gap_assessments": [gap]}
        self.assertEqual(validate_analysis(data, scan), [])
        self.assertEqual(gap["status"], "unresolved")
        gap["resolution_stage"] = "delivery"
        with self.assertRaisesRegex(ValueError, "resolution_stage"):
            validate_analysis(data, scan)
        gap["resolution_stage"] = "test_execute"
        gap["applicable"] = False
        with self.assertRaisesRegex(ValueError, "not_applicable"):
            validate_analysis(data, scan)
        gap["status"] = "not_applicable"
        self.assertEqual(validate_analysis(data, scan), [])
        gap["closure_criteria"] = []
        self.assertEqual(validate_analysis(data, scan), [])
        gap.update(applicable=True, status="unresolved")
        with self.assertRaisesRegex(ValueError, "closure_criteria"):
            validate_analysis(data, scan)

    def test_handler_returns_researchable_knowledge_and_tracks_deferred_gaps(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            scan = {"skills": {"platform": {"report": {"findings": [], "known_gaps": ["API", "client"]}}}}
            gaps = [{"skill": "platform", "index": index, "applicable": True, "status": "unresolved",
                     "kind": kind, "resolution_stage": stage, "closure_criteria": ["concrete proof"],
                     "affected_tasks": ["migration"], "evidence": ["source symbol"]}
                    for index, kind, stage in ((0, "knowledge", "mod_analysis"), (1, "verification", "client_smoke"))]
            atomic_json(root / "scan.json", scan)
            output = root / "baseline/.modport/mod-analysis.json"
            data = {"schema_version": 2, "candidates": [], "gap_assessments": gaps}
            atomic_json(output, data)
            command = OperationInput("test", "mod_analysis", "mod_analysis", "test:analysis:1", str(root),
                                     artifact_refs={"mod_scan_report": {"path": "scan.json", "sha256": file_digest(root / "scan.json")}})
            with patch("modport.handlers.CodexStageHandler") as agent:
                agent.return_value.return_value = OperationResult("completed")
                handler = AnalysisStageHandler("mod_analysis", (".modport/mod-analysis.json",))
                result = handler(command)
                self.assertEqual(result.status, "failed")
                self.assertEqual(result.error_code, "relevant_skill_gap")
                self.assertTrue(result.outputs["research_repairable"])
                self.assertEqual(result.outputs["unresolved_relevant_gaps"], [gaps[0]])
                self.assertEqual(result.outputs["deferred_verification_gaps"], [gaps[1]])
                gaps[0]["status"] = "resolved"
                atomic_json(output, data)
                result = handler(command)
                self.assertEqual(result.status, "completed")
                self.assertEqual(result.outputs["deferred_verification_gaps"], [gaps[1]])

    def test_mod_unrelated_gap_is_ignored_without_research_or_verification_obligations(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            scan = {"skills": {"platform": {"report": {"findings": [], "known_gaps": ["custom menu protocol"]}}}}
            gap = {"skill": "platform", "index": 0, "applicable": False, "status": "not_applicable",
                   "kind": "knowledge", "resolution_stage": "mod_analysis",
                   "closure_criteria": [], "affected_tasks": [],
                   "evidence": ["Inspected mod source, resources and dependencies; no custom menu feature or protocol is involved."]}
            data = {"schema_version": 2, "candidates": [], "gap_assessments": [gap]}
            atomic_json(root / "scan.json", scan)
            atomic_json(root / "baseline/.modport/mod-analysis.json", data)
            command = OperationInput("test", "mod_analysis", "mod_analysis", "test:analysis:1", str(root),
                                     artifact_refs={"mod_scan_report": {"path": "scan.json", "sha256": file_digest(root / "scan.json")}})
            with patch("modport.handlers.CodexStageHandler") as agent:
                agent.return_value.return_value = OperationResult("completed")
                result = AnalysisStageHandler("mod_analysis", (".modport/mod-analysis.json",))(command)
            self.assertEqual(result.status, "completed")
            self.assertFalse(result.outputs["research_repairable"])
            self.assertEqual(result.outputs["unresolved_relevant_gaps"], [])
            self.assertEqual(result.outputs["deferred_verification_gaps"], [])
