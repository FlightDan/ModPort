import unittest
from types import SimpleNamespace

from modport.report_schemas import report_contract


def command(stage, *, command_id="exec-1", payload=None, workflow_version=None):
    options = {} if workflow_version is None else {"workflow_version": workflow_version}
    return SimpleNamespace(stage_id=stage, command_id=command_id, payload=payload or {},
                           options=options)


def assert_strict_objects(test, schema):
    if not isinstance(schema, dict):
        return
    if schema.get("type") == "object":
        test.assertFalse(schema.get("additionalProperties", True), schema)
        properties = schema.get("properties", {})
        test.assertEqual(set(schema.get("required", [])), set(properties), schema)
        for child in properties.values():
            assert_strict_objects(test, child)
    for keyword in ("items", "additionalProperties"):
        if isinstance(schema.get(keyword), dict):
            assert_strict_objects(test, schema[keyword])
    for keyword in ("anyOf", "oneOf", "allOf"):
        for child in schema.get(keyword, []):
            assert_strict_objects(test, child)


class ReportContractTests(unittest.TestCase):
    def test_read_only_planning_uses_markdown_then_task_json(self):
        first = report_contract(command("migration_inventory"))
        second = report_contract(command("migration_plan"))
        tasks = report_contract(command("migration_tasks"))
        handoff = report_contract(command("parallel_review"))
        self.assertIsNone(first["schema"])
        self.assertIsNone(second["schema"])
        self.assertIsNone(first["output_path"])
        self.assertIsNone(second["output_path"])
        self.assertEqual(tasks["schema"]["required"], ["tasks"])
        self.assertIsNone(tasks["output_path"])
        self.assertIsNone(handoff["schema"])

    def test_primary_json_documents_use_the_existing_paths(self):
        expected = {
            "preparation": ".modport/preparation.json",
            "mod_analysis": ".modport/mod-analysis.json",
            "contract_review": ".modport/contract-review.json",
            "code_review": ".modport/code-review.json",
            "test_design": ".modport/independent-tests/suite.json",
            "research_review": ".modport/research-review.json",
            "admin_review": ".modport/admin-review.json",
            "gap_plan_review": ".modport/gap-plan-review.json",
            "gap_review": ".modport/gap-review.json",
        }
        for stage, path in expected.items():
            with self.subTest(stage=stage):
                contract = report_contract(command(stage))
                self.assertEqual(contract["output_path"], path)
                self.assertIsInstance(contract["schema"], dict)

    def test_common_reviews_share_one_shape(self):
        schemas = [report_contract(command(stage))["schema"] for stage in (
            "code_review", "test_review",
            "platform_skill_review", "java_skill_review",
        )]
        self.assertTrue(all(schema == schemas[0] for schema in schemas[1:]))
        self.assertEqual(set(schemas[0]["properties"]), {"verdict", "findings", "report"})

    def test_current_contract_review_schema_carries_semantic_assertion_reviews(self):
        schema = report_contract(command("contract_review", workflow_version=31))["schema"]
        properties = schema["properties"]
        self.assertEqual(set(properties), {"verdict", "findings", "report", "assertion_reviews"})
        reviews = properties["assertion_reviews"]["items"]["properties"]
        self.assertEqual(set(reviews), {"assertion_id", "source_anchor", "status", "reasoning"})
        self.assertEqual(reviews["status"]["enum"], ["supported", "unsupported", "ambiguous"])
        self.assertEqual(set(reviews["source_anchor"]["properties"]),
                         {"path", "start_line", "end_line"})
        assert_strict_objects(self, schema)

    def test_markdown_report_paths_remain_real_consumer_paths(self):
        self.assertEqual(report_contract(command("gap_research"))["output_path"],
                         ".modport/gap-research/report.md")
        self.assertEqual(report_contract(command("gap_plan"))["output_path"],
                         ".modport/gap-plan.json")
        self.assertEqual(report_contract(command("gate_handoff", command_id="handoff-2"))["output_path"],
                         ".modport/gate-handoffs/handoff-2.md")
        coder = report_contract(command("coder", payload={"development_task": {"id": "task-a"}}))
        self.assertEqual(coder["output_path"], ".modport/goal-reports/task-a.json")
        self.assertIsNone(coder["schema"])

    def test_contract_draft_uses_array_wire_shape_for_dynamic_test_ids(self):
        contract = report_contract(command("contract_draft"),
                                   required_paths=(".modport/functional-contract.json",))
        self.assertEqual(contract["transform"], "characterization")
        self.assertEqual(contract["output_path"], ".modport/functional-contract.json")
        evidence = contract["schema"]["properties"]["test_evidence"]
        self.assertEqual(set(evidence["items"]["properties"]), {"test_id", "declaration"})
        self.assertTrue({"source_fingerprint", "rubric_id", "rubric_version", "rubric_sha256"}
                        .isdisjoint(contract["schema"]["properties"]))

    def test_skill_generation_is_an_explicit_multi_document_envelope(self):
        for stage, kind, version_keys in (
            ("platform_diff", "platform", {"minecraft", "loader", "loader_version"}),
            ("java_diff", "java", {"java"}),
        ):
            with self.subTest(stage=stage):
                contract = report_contract(command(stage))
                outputs = contract["schema"]["properties"]["outputs"]["properties"]
                self.assertEqual(set(outputs), {"SKILL.md", "metadata.json", "rules.json",
                                                "coverage.json", "evidence.json"})
                self.assertEqual(set(contract["output_paths"]), set(outputs))
                metadata = outputs["metadata.json"]["properties"]
                self.assertEqual(metadata["kind"]["enum"], [kind])
                self.assertEqual(set(metadata["source"]["properties"]), version_keys)

    def test_every_structured_contract_uses_strict_object_shapes(self):
        stages = (
            "preparation", "mod_analysis", "contract_draft", "migration_tasks", "contract_review",
            "test_design", "test_review", "research_review", "admin_review",
            "gap_plan_review", "gap_review", "platform_diff", "java_diff", "supervisor",
        )
        for stage in stages:
            with self.subTest(stage=stage):
                schema = report_contract(command(stage))["schema"]
                self.assertIsInstance(schema, dict)
                assert_strict_objects(self, schema)

    def test_mod_analysis_uses_all_current_candidate_statuses(self):
        schema = report_contract(command("mod_analysis"))["schema"]
        candidate = schema["properties"]["candidates"]["items"]
        self.assertIn("candidate", candidate["properties"]["status"]["enum"])

    def test_supervisor_uses_nullable_wire_intervention(self):
        contract = report_contract(command("supervisor"))
        self.assertEqual(contract["transform"], "supervisor")
        intervention = contract["schema"]["properties"]["intervention"]
        self.assertEqual(set(intervention["properties"]),
                         {"prompt", "task_ids", "stage", "profile"})
        self.assertIn("targeted_fix", contract["schema"]["properties"]["decision"]["enum"])

    def test_static_only_test_suite_omits_init_script(self):
        contract = report_contract(command("test_design", payload={"regression_scope": {
            "runtime_behavior_ids": [], "static_behavior_ids": ["visual"]}}))
        self.assertNotIn("init_script", contract["schema"]["properties"])
        self.assertEqual(contract["schema"]["properties"]["tests"]["maxItems"], 0)

    def test_structured_contracts_do_not_force_nonempty_diagnostic_content(self):
        tasks = report_contract(command("migration_tasks"))["schema"]["properties"]["tasks"]
        tests = report_contract(command("test_design"))["schema"]["properties"]["tests"]
        evidence = report_contract(command("gap_review"))["schema"]["properties"]["gap_resolutions"]
        self.assertNotIn("minItems", tasks)
        self.assertNotIn("minItems", tests)
        self.assertNotIn("minItems", evidence)

    def test_unknown_single_markdown_output_uses_that_path(self):
        contract = report_contract(command("future_report"), required_paths=(".modport/future.md",))
        self.assertEqual(contract["output_path"], ".modport/future.md")
        self.assertIsNone(contract["schema"])


if __name__ == "__main__":
    unittest.main()
