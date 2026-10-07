"""Workflow v17 records business diagnostics and keeps executing."""

import unittest

from modport.contracts import OperationInput, OperationResult
import test_planning_operations
import test_regression_operations


class BusinessFreeRoutingTests(unittest.TestCase):
    def setUp(self):
        self.fixture = test_planning_operations.PlanningPolicyTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.header["definition"]["workflow_version"] = 17
        self.fixture.header["definition"]["gate_policy"] = {
            "mode": "disabled", "automatic_rework": False}

    def test_failed_and_rejected_stages_advance_without_handoff_or_retry(self):
        f = self.fixture
        changes = f.settle("target_build", status="failed", error_code="build_failed")
        self.assertEqual(["code_review"], [row.stage_id for row in f.scheduled(changes)])
        self.assertFalse(any(row.stage_id == "gate_handoff" for row in f.scheduled(changes)))
        self.assertEqual({}, f.app["rounds"])

        changes = f.settle("contract_review", outputs={"verdict": "rejected"})
        self.assertEqual(["contract_freeze"], [row.stage_id for row in f.scheduled(changes)])
        self.assertEqual("rejected", f.app["effective"]["contract_review"]["outputs"]["verdict"])

        changes = f.settle("migration_tasks", status="failed",
                           error_code="planning_output_invalid")
        self.assertEqual(["parallel_review"], [row.stage_id for row in f.scheduled(changes)])
        self.assertNotIn("migration_tasks", [row.stage_id for row in f.scheduled(changes)])
        self.assertEqual({}, f.app["format_retries"])

    def test_failed_test_advances_to_remaining_acceptance_observation(self):
        f = self.fixture
        changes = f.settle("test_execute", status="failed", error_code="tests_failed")
        self.assertEqual(["acceptance_preflight"],
                         [row.stage_id for row in f.scheduled(changes)])
        self.assertEqual("failed", f.app["effective"]["test_execute"]["status"])

    def test_second_revision_is_the_actual_input_to_task_organization(self):
        f = self.fixture
        from pathlib import Path
        from modport.evidence import file_digest
        refs = []
        for name in ("draft", "improved"):
            path = Path(f.header["run_dir"]) / "artifacts" / (name + ".md")
            path.parent.mkdir(exist_ok=True)
            path.write_text(name)
            refs.append({"path": path.relative_to(Path(f.header["run_dir"])).as_posix(),
                         "sha256": file_digest(path), "media_type": "text/markdown"})
        draft, improved = refs
        f.settle("migration_inventory", outputs={"artifact_refs": {"current_plan": draft},
            "plan_revision": 1, "plan_status": "continue"})
        changes = f.settle("migration_plan", outputs={"artifact_refs": {"current_plan": improved},
            "plan_revision": 2, "plan_status": "ready"})
        tasks, = f.scheduled(changes)
        self.assertEqual("migration_tasks", tasks.stage_id)
        self.assertEqual(improved, tasks.artifact_refs["current_plan"])
        self.assertEqual(improved, f.app["plan_documents"]["migration"]["current_ref"])

    def test_unavailable_failed_ref_is_diagnostic_without_poisoning_downstream(self):
        f = self.fixture
        broken = {"path": "artifacts/no-such-report.json", "sha256": "f" * 64}
        changes = f.settle("target_build", status="failed", error_code="build_failed",
            outputs={"artifact_refs": {"missing": broken}})
        review, = f.scheduled(changes)
        self.assertEqual("code_review", review.stage_id)
        self.assertNotIn("missing", review.artifact_refs)
        self.assertIn("missing", review.payload["unavailable_artifact_refs"])
        self.assertNotIn("missing", review.upstream_results["target_build"]["outputs"]["artifact_refs"])
        self.assertEqual(broken, f.app["effective"]["target_build"]["outputs"]["artifact_refs"]["missing"])

    def test_changed_verified_input_remains_an_operational_failure(self):
        f = self.fixture
        changes = f.settle("code_review", status="failed", error_code="command_artifact_invalid")
        self.assertEqual([], f.scheduled(changes))
        self.assertEqual(["failed"], [row["state"] for row in changes if row["kind"] == "finish"])

    def test_invalid_contract_observation_does_not_block_acceptance_build(self):
        f = self.fixture
        changes = f.settle("acceptance_preflight", status="failed",
            error_code="locked_artifact_invalid")
        self.assertEqual(["acceptance_build"],
                         [row.stage_id for row in f.scheduled(changes)])
        self.assertEqual("failed", f.app["effective"]["acceptance_preflight"]["status"])

    def test_source_and_provider_launch_failures_stop_execution(self):
        f = self.fixture
        for stage, code in (("source", "source_clone_failed"),
                            ("environment", "version_resolution_failed"),
                            ("contract_draft", "agent_output_invalid"),
                            ("migration_inventory", "agent_launch_failed"),
                            ("migration_plan", "opencode_cleanup_unconfirmed")):
            with self.subTest(stage=stage):
                f.app.update(stop_reason=None, stop_state=None, terminal_reason=None)
                changes = f.settle(stage, status="failed", error_code=code)
                self.assertEqual([], f.scheduled(changes))
                self.assertEqual(["failed"], [row["state"] for row in changes
                                              if row["kind"] == "finish"])

    def test_sandbox_unavailability_remains_an_operational_stop(self):
        f = self.fixture
        changes = f.settle("target_build", status="failed",
                           error_code="build_sandbox_unavailable")
        self.assertEqual(["failed"], [row["state"] for row in changes
                                      if row["kind"] == "finish"])
        self.assertEqual("build_sandbox_unavailable", f.app["terminal_reason"])
        self.assertEqual([], f.scheduled(changes))

    def test_failed_group_member_does_not_cancel_peer_and_starts_coder(self):
        f = self.fixture
        tasks = [
            {"id": "a", "dependencies": [], "model": "gpt-5.6-luna",
             "reasoning_effort": "max"},
            {"id": "b", "dependencies": [], "model": "gpt-5.6-luna",
             "reasoning_effort": "max"},
        ]
        f.app["active_stage"] = None
        f.app["active_group"] = {
            "kind": "development", "generation": 1, "tasks": tasks,
            "base": "f" * 40, "parallel": True,
            "members": ["goal.g1.a", "goal.g1.b"],
            "goal_scheduled": ["a", "b"], "scheduled": [], "results": {},
            "goal_scope": "migration", "planning_context": {},
            "artifact_refs": {}, "execution_payload": {},
        }
        for name, state in (("a", "succeeded"), ("b", "running")):
            command = OperationInput("policy", f"goal.g1.{name}", "goal_prepare",
                                     f"goal:{name}:1", f.header["run_dir"])
            attempt = {"state": state, "command": {
                "execution_id": command.command_id, "payload": command.to_dict()}}
            if name == "a":
                result = OperationResult("failed", command.run_id, command.task_id,
                    command.stage_id, command.command_id, error_code="goal_output_invalid")
                attempt["result"] = {"value": result.to_dict()}
            f.snapshot["tasks"][command.task_id] = {"attempts": [attempt]}

        changes, app = f.operations._decision(f.snapshot, f.header)
        f.snapshot["application_state"] = app

        self.assertEqual(["coder"], [row.stage_id for row in f.scheduled(changes)])
        self.assertFalse(any(row["kind"] == "cancel" for row in changes))
        self.assertIsNotNone(app["active_group"])

    def test_delivery_finishes_execution_as_unverified_with_diagnostics(self):
        f = self.fixture
        changes = f.settle("delivery", status="failed", error_code="missing_evidence")

        self.assertEqual(["succeeded"], [row["state"] for row in changes
                                         if row["kind"] == "finish"])
        self.assertEqual("workflow_execution_finished", f.app["terminal_reason"])
        self.assertEqual("unverified", f.app["acceptance_status"])
        self.assertEqual("finished", f.app["execution_status"])
        self.assertGreaterEqual(f.app["diagnostic_count"], 1)
        self.assertNotIn("acceptance", f.app["terminal_reason"])

    def test_compile_package_records_source_baseline_failure_without_test_stages(self):
        from modport.models import MigrationRequest
        from modport.workflow import WorkflowDefinition
        f = self.fixture
        request = MigrationRequest.from_mapping(f.header['request'])
        request = MigrationRequest.from_mapping({**request.to_dict(),
                                                'validation_scope': 'compile_package'})
        f.header['request'] = request.to_dict()
        f.header['definition'] = WorkflowDefinition(f.header['request'], version=27).to_dict()
        f.header['definition']['validation_policy']['scope'] = 'compile_package'

        changes = f.settle('code_review', outputs={'verdict': 'approved'})
        self.assertEqual(['acceptance_preflight'],
                         [row.stage_id for row in f.scheduled(changes)])
        self.assertNotIn('test_design', [row.stage_id for row in f.scheduled(changes)])
        changes = f.settle('acceptance_preflight')
        self.assertEqual(['gap_review'], [row.stage_id for row in f.scheduled(changes)])
        changes = f.settle('gap_review')
        self.assertEqual(['delivery'], [row.stage_id for row in f.scheduled(changes)])

        changes = f.settle('delivery', outputs={'required_checks': {
            'source_baseline_behavior_tests': {'status': 'failed'},
            'target_compile': {'status': 'passed'},
            'target_package': {'status': 'passed'},
        }})
        self.assertEqual(['succeeded'], [row['state'] for row in changes if row['kind'] == 'finish'])
        self.assertEqual('delivery_compile_package_requirements_not_met',
                         f.app['terminal_reason'])
        self.assertEqual('unverified', f.app['acceptance_status'])

    def test_v28_compile_package_settles_after_target_receipts_without_source_tests(self):
        from modport.models import MigrationRequest
        from modport.workflow import WorkflowDefinition
        f = self.fixture
        request = MigrationRequest.from_mapping({**f.header['request'],
                                                'validation_scope': 'compile_package'})
        f.header['request'] = request.to_dict()
        f.header['definition'] = WorkflowDefinition(f.header['request'], version=28).to_dict()
        changes = f.settle('delivery', outputs={'required_checks': {
            'target_compile': {'status': 'passed'},
            'target_package': {'status': 'passed'},
        }})
        self.assertEqual(['succeeded'], [row['state'] for row in changes if row['kind'] == 'finish'])
        self.assertEqual('delivery_completed_acceptance_unverified', f.app['terminal_reason'])
        self.assertEqual('unverified', f.app['acceptance_status'])

    def test_v28_compile_package_routes_identity_check_to_freeze(self):
        from modport.models import MigrationRequest
        from modport.workflow import WorkflowDefinition
        f = self.fixture
        request = MigrationRequest.from_mapping({**f.header['request'],
                                                'validation_scope': 'compile_package'})
        f.header['request'] = request.to_dict()
        f.header['definition'] = WorkflowDefinition(f.header['request'], version=28).to_dict()
        changes = f.settle('contract_verify', outputs={
            'verification_scope': 'source_identity_only',
            'process_executed': False,
        })
        self.assertEqual(['source', 'contract_freeze'],
                         [row.stage_id for row in f.scheduled(changes)])
        self.assertNotIn('contract_review', [row.stage_id for row in f.scheduled(changes)])

    def test_v28_inherited_identity_failure_stops_before_planning(self):
        from modport.models import MigrationRequest
        from modport.workflow import WorkflowDefinition
        f = self.fixture
        request = MigrationRequest.from_mapping({**f.header['request'],
                                                'validation_scope': 'compile_package'})
        f.header['request'] = request.to_dict()
        f.header['definition'] = WorkflowDefinition(f.header['request'], version=28).to_dict()
        changes = f.settle('contract_verify', status='blocked',
                           error_code='inherited_harness_identity_invalid')
        self.assertEqual([], f.scheduled(changes))
        self.assertEqual(['failed'], [row['state'] for row in changes if row['kind'] == 'finish'])
        self.assertEqual('inherited_harness_identity_invalid', f.app['terminal_reason'])

    def test_failed_source_baseline_test_still_advances_to_contract_review(self):
        from modport.models import MigrationRequest
        from modport.workflow import WorkflowDefinition
        f = self.fixture
        request = MigrationRequest.from_mapping(f.header['request'])
        request = MigrationRequest.from_mapping({**request.to_dict(),
                                                'validation_scope': 'compile_package'})
        f.header['request'] = request.to_dict()
        f.header['definition'] = WorkflowDefinition(f.header['request'], version=27).to_dict()
        f.header['definition']['validation_policy']['scope'] = 'compile_package'

        changes = f.settle('contract_verify', status='failed',
                           error_code='baseline_contract_failed')
        self.assertEqual(['source', 'contract_review'],
                         [row.stage_id for row in f.scheduled(changes)])

    def test_continued_failed_boundary_schedules_its_successor(self):
        f = self.fixture
        failure = OperationResult("failed", "policy", "contract_repair_tasks",
            "contract_repair_tasks", "old:repair-tasks", error_code="scope_invalid")
        f.app["effective"]["contract_repair_tasks"] = failure.to_dict()
        f.app["flowthrough_resume"] = {
            "stage": "contract_repair_tasks", "task_id": "contract_repair_tasks",
            "command_id": failure.command_id, "location": "early"}

        changes, app = f.operations._decision(f.snapshot, f.header)

        self.assertEqual(["contract_repair_review"],
                         [row.stage_id for row in f.scheduled(changes)])
        self.assertFalse(any(row.stage_id == "gate_handoff" for row in f.scheduled(changes)))
        self.assertNotIn("flowthrough_resume", app)

    def test_continuation_can_enter_top_level_migration_planning(self):
        f = self.fixture
        failure = OperationResult("failed", "policy", "contract_repair_tasks",
            "contract_repair_tasks", "old:repair-tasks", error_code="scope_invalid")
        f.app["effective"]["contract_repair_tasks"] = failure.to_dict()
        f.app["flowthrough_resume"] = {
            "stage": "contract_repair_tasks", "task_id": "contract_repair_tasks",
            "command_id": failure.command_id, "location": "early",
            "next_stage": "migration_inventory"}

        changes, _ = f.operations._decision(f.snapshot, f.header)

        self.assertEqual(["migration_inventory"],
                         [row.stage_id for row in f.scheduled(changes)])

    def test_group_continuation_keeps_failed_partial_result(self):
        f = self.fixture
        task = {"id": "a", "dependencies": [], "model": "gpt-5.6-luna",
                "reasoning_effort": "max"}
        failure = OperationResult("failed", "policy", "coder.g1.a", "coder",
            "old:coder-a", outputs={"artifact_refs": {"coder_patch": f.ref("partial")}},
            error_code="checks_failed").to_dict()
        f.app["active_group"] = {
            "kind": "development", "generation": 1, "tasks": [task],
            "base": "f" * 40, "parallel": True, "members": ["coder.g1.a"],
            "goal_scheduled": ["a"], "scheduled": ["a"],
            "results": {"coder.g1.a": failure}, "failure": failure,
            "goal_scope": "migration", "planning_context": {},
            "artifact_refs": {}, "execution_payload": {},
        }
        f.app["effective"]["coder.g1.a"] = failure
        f.app["flowthrough_resume"] = {
            "stage": "coder", "task_id": "coder.g1.a",
            "command_id": "old:coder-a", "location": "group"}

        changes, app = f.operations._decision(f.snapshot, f.header)

        integrate, = f.scheduled(changes)
        self.assertEqual("development_integrate", integrate.stage_id)
        self.assertEqual("failed", integrate.payload["development_results"][0]["status"])
        self.assertNotIn("failure", app.get("active_group") or {})
        self.assertFalse(any(row["kind"] == "cancel" for row in changes))

    def test_isolated_coder_failure_is_a_diagnostic_and_group_integrates_available_work(self):
        f = self.fixture
        task = {"id": "a", "dependencies": [], "model": "gpt-5.6-luna",
                "reasoning_effort": "max"}
        command = OperationInput("policy", "coder.g1.a", "coder", "coder:a:1",
                                 f.header["run_dir"])
        outcome = OperationResult("blocked", command.run_id, command.task_id,
                                  command.stage_id, command.command_id,
                                  error_code="coder_isolation_violation")
        f.snapshot["tasks"][command.task_id] = {"attempts": [{
            "state": "succeeded", "command": {
                "execution_id": command.command_id, "payload": command.to_dict()},
            "result": {"value": outcome.to_dict()}}]}
        f.app["active_stage"] = None
        f.app["active_group"] = {
            "kind": "development", "generation": 1, "tasks": [task],
            "base": "f" * 40, "parallel": True, "members": [command.task_id],
            "goal_scheduled": ["a"], "scheduled": ["a"], "results": {},
            "goal_scope": "migration", "planning_context": {},
            "artifact_refs": {}, "execution_payload": {},
        }

        changes, app = f.operations._decision(f.snapshot, f.header)

        integrate, = f.scheduled(changes)
        self.assertEqual("development_integrate", integrate.stage_id)
        self.assertEqual("blocked", integrate.payload["development_results"][0]["status"])
        self.assertFalse(any(row["kind"] == "finish" for row in changes))
        self.assertEqual("coder_isolation_violation",
                         app["effective"][command.task_id]["error_code"])

    def test_failed_regression_scope_does_not_stop_other_scopes(self):
        r = test_regression_operations.RegressionOperationsTests()
        r.setUp()
        r.header["definition"] = {"workflow_version": 17}
        r.policy._flowthrough_diagnostic = lambda app, result: app.setdefault(
            "business_diagnostics", []).append(result)
        r.policy._flowthrough_operational_failure = lambda attempt, outcome: None
        r.decide()
        r.complete("test_design", r.scopes[0], status="failed")
        r.complete("test_design", r.scopes[1])

        changes = r.decide()

        self.assertEqual(2, len(changes))
        self.assertTrue(all(row["task_id"].startswith("test_review.") for row in changes))
        self.assertIsNotNone(r.app["active_group"])
        self.assertNotIn("failure", r.group)

    def test_malformed_admin_review_does_not_invent_approval_or_close_gaps(self):
        f = self.fixture
        f.app['support_pending'] = ['admin_review']
        f.app['admin_imports'] = {'submission': {'status': 'pending', 'base_gap_revision': 0,
                                                'knowledge_revisions': {}}}
        f.app['project_research_gaps'] = {'platform:0': {'project_status': 'unresolved'}}
        command = OperationInput('policy', 'admin_review', 'admin_review', 'admin:1',
            f.header['run_dir'], options={'workflow_version': 17},
            payload={'admin_submission_id': 'submission'})
        result = OperationResult('completed', command.run_id, command.task_id,
            command.stage_id, command.command_id, outputs={
                'raw_report': 'Unfinished review', 'acceptance_status': 'unverified',
                'approved_gap_resolutions': [{'gap_id': 'platform:0', 'project_status': 'resolved'}],
                'approved_generic_knowledge_entries': {'platform': ['unverified']}})
        f.snapshot['tasks']['admin_review'] = {'attempts': [{
            'state': 'succeeded', 'command': {'execution_id': command.command_id,
                                             'payload': command.to_dict()},
            'result': {'value': result.to_dict()}}]}
        changes = f.operations._support_decision(f.snapshot, f.header, f.app)
        self.assertEqual([], f.scheduled(changes))
        self.assertEqual('diagnostic', f.app['admin_imports']['submission']['status'])
        self.assertEqual('unresolved', f.app['project_research_gaps']['platform:0']['project_status'])
        self.assertEqual(0, f.app['gap_revision'])


if __name__ == "__main__":
    unittest.main()
