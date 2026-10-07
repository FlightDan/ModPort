import unittest

from modport.repair_progress import build_repair_feedback, render_repair_feedback


class RepairProgressTests(unittest.TestCase):
    def record(self, candidate="a" * 40, verified=None, status="failed", detail="same failure", **extra):
        verified = candidate if verified is None else verified
        updates = [
            {"stage": "integration", "result": {
                "status": "completed", "run_id": "run", "task_id": "author",
                "stage_id": "integration", "command_id": "author-1",
                "outputs": {"after_head": candidate, "build_status": "completed"},
                "detail": "", "error_code": None,
            }},
            {"stage": "verification", "result": {
                "status": status, "run_id": "run", "task_id": "review",
                "stage_id": "verification", "command_id": "review-1",
                "outputs": {"verification_candidate_id": verified,
                            "verification_status": status,
                            "build_executed": True,
                            "verification_executed": True,
                            "failure_signature": "stable-check-failure" if status == "failed" else None,
                            "verification_failure_signature": "stable-check-failure" if status == "failed" else None,
                            "raw_log_refs": [{"path": "logs/verify.log", "sha256": "f" * 64}]},
                "detail": detail, "error_code": "check_failed" if status == "failed" else None,
            }},
        ]
        return {"request_id": "repair-1", "target_agent": "coder-a",
                "reviewer_execution_id": "waiting-reviewer",
                "verification_execution_id": "review-1", "updates": updates, **extra}

    def inventory(self, candidate, issues, *, source_complete=True,
                  logs_complete=True, truncated=False):
        scan_scope = {
            "ruleset": "rules-v3", "rule_ids": ["forge-package-reference"],
            "source_files": ["src/main/java/A.java"], "build_files": ["build.gradle"],
            "resource_files": [], "source_complete": source_complete,
            "logs_provided": True, "log_sources": ["target_build"],
            "scanned_log_sources": ["target_build"],
            "log_scope": {"parser": "compiler-diagnostics-v3", "sources": ["target_build"],
                          "executions": [{"source": "target_build", "build_status": "failed",
                                          "execution_scope_known": True,
                                          "tasks": [{"task": ":compileJava", "status": "failed"}],
                                          "tasks_truncated": False}]},
            "logs_complete": logs_complete, "observations_provided": True,
            "observation_categories": ["runtime_failure"],
        }
        coverage = {
            "scan_complete": source_complete and logs_complete and not truncated,
            "source_complete": source_complete, "logs_complete": logs_complete,
            "truncated": truncated,
            "limits": {"max_files": 100, "max_file_bytes": 1000,
                       "max_total_bytes": 10000, "max_log_tasks": 256},
            "excluded_directory_names": [".git", "build"],
            "excluded_directory_prefixes": [".codex-"],
        }
        return {"schema_version": 1, "kind": "repair_inventory",
                "candidate_id": candidate, "execution_id": "inventory-" + candidate[:8],
                "issues": issues, "coverage": coverage, "scan_scope": scan_scope}

    def test_stale_candidate_never_becomes_current(self):
        feedback = build_repair_feedback(self.record(verified="b" * 40))
        self.assertFalse(feedback["verification_current"])
        self.assertFalse(feedback["acceptance_evidence"])
        self.assertEqual(feedback["progress"]["stalled"], "unknown")

    def test_inherited_harness_fingerprint_takes_precedence_over_source_head(self):
        record = self.record(candidate='a' * 40, verified='b' * 64)
        author = record['updates'][0]['result']['outputs']
        author['after_candidate_id'] = 'b' * 64
        feedback = build_repair_feedback(record)
        self.assertEqual('b' * 64, feedback['candidate_id'])
        self.assertIs(feedback['verification_current'], True)
        author['after_candidate_id'] = None
        feedback = build_repair_feedback(record)
        self.assertEqual(feedback['verification_current'], 'unknown')

    def test_author_cannot_declare_its_own_verification_candidate(self):
        record = self.record()
        record["verification_execution_id"] = None
        record["updates"] = record["updates"][:1]
        record["updates"][0]["result"]["outputs"]["verification_candidate_id"] = "a" * 40
        feedback = build_repair_feedback(record)
        self.assertIsNone(feedback["verification"])
        self.assertEqual(feedback["verification_current"], "unknown")

    def test_declared_run_must_match_observed_results(self):
        feedback = build_repair_feedback(self.record(run_id="another-run"))
        self.assertIsNone(feedback["run_id"])
        self.assertEqual(feedback["verification_current"], "unknown")

    def test_observed_and_expected_verifier_identities_are_distinct(self):
        feedback = build_repair_feedback(self.record())
        self.assertEqual(feedback["expected_verification_execution_id"], "review-1")
        self.assertEqual(feedback["verification_execution_id"], "review-1")

    def test_changed_commit_with_same_failure_is_stalled(self):
        previous = build_repair_feedback(self.record(candidate="a" * 40))
        current = build_repair_feedback(self.record(candidate="b" * 40), previous=previous)
        self.assertIs(current["progress"]["comparable"], True)
        self.assertIs(current["progress"]["same_root_cause"], True)
        self.assertFalse(current["progress"]["substantive_progress"])
        self.assertIs(current["progress"]["stalled"], True)

    def test_known_failure_to_completed_is_progress(self):
        previous = build_repair_feedback(self.record(status="failed"))
        current = build_repair_feedback(self.record(candidate="b" * 40, status="completed", detail=""), previous=previous)
        self.assertIs(current["progress"]["verification_advanced"], True)
        self.assertIs(current["progress"]["substantive_progress"], True)
        self.assertFalse(current["progress"]["stalled"])

    def test_unknown_failure_identity_cannot_be_called_stalled(self):
        first = self.record(detail="")
        second = self.record(candidate="b" * 40, detail="")
        first["updates"][1]["result"]["error_code"] = None
        second["updates"][1]["result"]["error_code"] = None
        first["updates"][1]["result"]["outputs"].pop("failure_signature")
        second["updates"][1]["result"]["outputs"].pop("failure_signature")
        first["updates"][1]["result"]["outputs"].pop("verification_failure_signature")
        second["updates"][1]["result"]["outputs"].pop("verification_failure_signature")
        previous = build_repair_feedback(first)
        current = build_repair_feedback(second, previous=previous)
        self.assertEqual(current["progress"]["same_root_cause"], "unknown")
        self.assertEqual(current["progress"]["stalled"], "unknown")

    def test_generic_wrapper_code_needs_a_trustworthy_root_identity(self):
        first = self.record(detail="build and behavior verification both executed")
        second = self.record(candidate="b" * 40,
                             detail="build and behavior verification both executed")
        for record in (first, second):
            result = record["updates"][1]["result"]
            result["error_code"] = "locked_artifact_invalid"
            result["outputs"].pop("failure_signature")
            result["outputs"].pop("verification_failure_signature")
        current = build_repair_feedback(second, previous=build_repair_feedback(first))
        self.assertEqual(current["progress"]["same_root_cause"], "unknown")
        self.assertEqual(current["progress"]["stalled"], "unknown")

    def test_dual_failure_uses_build_root_consistently(self):
        first = self.record()
        second = self.record(candidate="b" * 40)
        for record, code, signature in (
            (first, "compile-a", "build-signature-a"),
            (second, "compile-b", "build-signature-b"),
        ):
            outputs = record["updates"][1]["result"]["outputs"]
            outputs.update(
                build_status="failed", build_executed=True,
                build_error_code=code, build_failure_signature=signature,
                verification_status="failed", verification_executed=True,
                verification_error_code="assertion-failed",
                verification_failure_signature="stable-behavior-signature",
            )
        current = build_repair_feedback(second, previous=build_repair_feedback(first))
        self.assertFalse(current["progress"]["same_root_cause"])
        self.assertFalse(current["progress"]["stalled"])

    def test_distinct_opaque_signature_digests_remain_distinct(self):
        first = self.record()
        second = self.record(candidate="b" * 40)
        first["updates"][1]["result"]["outputs"]["verification_failure_signature"] = "a" * 64
        second["updates"][1]["result"]["outputs"]["verification_failure_signature"] = "b" * 64
        current = build_repair_feedback(second, previous=build_repair_feedback(first))
        self.assertEqual(current["verification"]["verification_failure_signature"],
                         "b" * 64)
        self.assertFalse(current["progress"]["same_root_cause"])
        self.assertFalse(current["progress"]["stalled"])

    def test_empty_inventory_does_not_invent_a_failure_identity(self):
        baseline = self.record()["updates"][1]["result"]
        baseline["outputs"].pop("failure_signature")
        baseline["outputs"].pop("verification_failure_signature")
        scope = {"complete": True, "scan_rules": ["legacy-api"], "issues": []}
        first = self.record(before_inventory={**scope, "candidate_id": "0" * 40},
                            after_inventory={**scope, "candidate_id": "a" * 40},
                            baseline_verification={**baseline,
                                "outputs": {**baseline["outputs"],
                                            "verification_candidate_id": "0" * 40}})
        first["updates"][1]["result"]["outputs"].pop("failure_signature")
        first["updates"][1]["result"]["outputs"].pop("verification_failure_signature")
        previous = build_repair_feedback(first)
        second = self.record(candidate="b" * 40,
                             before_inventory={**scope, "candidate_id": "a" * 40},
                             after_inventory={**scope, "candidate_id": "b" * 40})
        second["updates"][1]["result"]["outputs"].pop("failure_signature")
        second["updates"][1]["result"]["outputs"].pop("verification_failure_signature")
        current = build_repair_feedback(second, previous=previous)
        self.assertEqual(current["progress"]["same_root_cause"], "unknown")
        self.assertEqual(current["progress"]["stalled"], "unknown")

    def test_log_derived_inventory_requires_log_scope_provenance(self):
        issue = {"issue_id": "compiler", "evidence": [
            {"source": "log:artifacts/old/build.log", "detail": "compiler error"}
        ]}
        before = {"schema_version": 1, "kind": "repair_inventory",
                  "candidate_id": "a" * 40, "issues": [issue],
                  "coverage": {"scan_complete": True, "truncated": False,
                               "limits": {"max_files": 10}}}
        after = {**before, "candidate_id": "b" * 40, "issues": []}
        previous = build_repair_feedback(self.record())
        current = build_repair_feedback(self.record(candidate="b" * 40,
                                                    before_inventory=before,
                                                    after_inventory=after), previous=previous)
        self.assertFalse(current["repair_inventory"]["comparable"])
        self.assertEqual(current["progress"]["inventory_advanced"], "unknown")

    def test_build_recovery_is_progress_while_behavior_still_fails(self):
        first = self.record()
        second = self.record(candidate="b" * 40)
        first_outputs = first["updates"][1]["result"]["outputs"]
        second_outputs = second["updates"][1]["result"]["outputs"]
        first_outputs.update(build_status="failed", build_error_code="compile_failed",
                             verification_status="failed")
        second_outputs.update(build_status="completed", verification_status="failed",
                              verification_error_code="assertion_failed")
        current = build_repair_feedback(second, previous=build_repair_feedback(first))
        self.assertIs(current["progress"]["build_advanced"], True)
        self.assertIs(current["progress"]["substantive_progress"], True)
        self.assertFalse(current["progress"]["stalled"])

    def test_behavior_completed_does_not_mask_failed_build(self):
        first = self.record()
        second = self.record(candidate="b" * 40, status="completed", detail="")
        first["updates"][1]["result"]["outputs"].update(
            build_status="failed", verification_status="failed")
        second["updates"][1]["result"]["outputs"].update(
            build_status="failed", build_error_code="compile_failed",
            verification_status="completed")
        current = build_repair_feedback(second, previous=build_repair_feedback(first))
        self.assertFalse(current["progress"]["verification_advanced"])
        self.assertFalse(current["progress"]["substantive_progress"])

    def test_incomplete_inventory_cannot_close_issue(self):
        before = {"complete": True, "scope": {"rules": ["legacy-api"]},
                  "candidate_id": "a" * 40, "issues": [{"id": "one"}]}
        after = {"complete": False, "scope": {"rules": ["legacy-api"]},
                 "candidate_id": "b" * 40, "issues": []}
        previous = build_repair_feedback(self.record())
        current = build_repair_feedback(self.record(candidate="b" * 40,
                                                    before_inventory=before,
                                                    after_inventory=after), previous=previous)
        self.assertFalse(current["repair_inventory"]["comparable"])
        self.assertEqual(current["progress"]["inventory_advanced"], "unknown")
        self.assertIs(current["progress"]["stalled"], True)

    def test_complete_same_scope_inventory_disappearance_is_scan_progress_only(self):
        one = {"issue_id": "one", "evidence": [{"source": "workspace_scan"}]}
        two = {"issue_id": "two", "evidence": [{"source": "workspace_path"}]}
        before = self.inventory("a" * 40, [one, two])
        after = self.inventory("b" * 40, [two])
        previous = build_repair_feedback(self.record())
        current = build_repair_feedback(self.record(candidate="b" * 40,
                                                    before_inventory=before,
                                                    after_inventory=after), previous=previous)
        self.assertEqual(current["repair_inventory"]["closed_issue_ids"], ["one"])
        self.assertNotIn("issue_ids", current["repair_inventory"]["before"])
        self.assertIs(current["progress"]["inventory_advanced"], True)
        self.assertIs(current["progress"]["substantive_progress"], True)
        self.assertFalse(current["acceptance_evidence"])

    def test_disappeared_failed_compiler_issue_is_not_progress(self):
        compiler = {"issue_id": "compiler", "evidence": [
            {"source": "log:target_build", "detail": ":compileJava failed"}
        ]}
        before = self.inventory("a" * 40, [compiler])
        after = self.inventory("b" * 40, [])
        previous = build_repair_feedback(self.record())
        current = build_repair_feedback(self.record(candidate="b" * 40,
                                                    before_inventory=before,
                                                    after_inventory=after), previous=previous)
        inventory = current["repair_inventory"]
        self.assertTrue(inventory["comparable"])
        self.assertEqual(inventory["closed_issue_count"], 0)
        self.assertTrue(inventory["source_comparable"])
        self.assertEqual(inventory["source_closed_issue_count"], 0)
        self.assertFalse(current["progress"]["inventory_advanced"])
        self.assertFalse(current["progress"]["substantive_progress"])

    def test_inline_agent_inventory_cannot_override_host_scan(self):
        issue = {"issue_id": "unfixed", "evidence": [{"source": "workspace_scan"}]}
        before = self.inventory("a" * 40, [issue])
        after = self.inventory("b" * 40, [issue])
        forged = self.inventory("b" * 40, [])
        previous = build_repair_feedback(self.record())
        for update_index in (0, 1):
            for injected in (forged, {"before_inventory": before, "after_inventory": forged}):
                with self.subTest(update=update_index, wrapped="after_inventory" in injected):
                    record = self.record(candidate="b" * 40,
                                         before_inventory=before, after_inventory=after)
                    record["updates"][update_index]["result"]["outputs"]["repair_inventory"] = injected
                    current = build_repair_feedback(record, previous=previous)
                    self.assertEqual(current["repair_inventory"]["after"]["issue_count"], 1)
                    self.assertEqual(current["repair_inventory"]["source_closed_issue_count"], 0)
                    self.assertFalse(current["progress"]["substantive_progress"])
                    record.pop("before_inventory")
                    record.pop("after_inventory")
                    self.assertNotIn("repair_inventory", build_repair_feedback(record))

    def test_static_issue_removal_progresses_despite_truncated_build_log(self):
        source_issue = {"issue_id": "forge-source", "evidence": [
            {"source": "workspace_scan", "detail": "Forge import"}
        ]}
        compiler = {"issue_id": "compiler", "evidence": [
            {"source": "log:target_build", "detail": ":compileJava failed"}
        ]}
        before = self.inventory("a" * 40, [source_issue, compiler],
                                logs_complete=False, truncated=True)
        after = self.inventory("b" * 40, [compiler],
                               logs_complete=False, truncated=True)
        after["scan_scope"]["log_sources"] = ["different-build"]
        after["scan_scope"]["log_scope"]["sources"] = ["different-build"]
        after["scan_scope"]["log_scope"]["executions"][0]["source"] = "different-build"
        after["coverage"]["limits"]["max_log_tasks"] = 1
        previous = build_repair_feedback(self.record())
        current = build_repair_feedback(self.record(candidate="b" * 40,
                                                    before_inventory=before,
                                                    after_inventory=after), previous=previous)
        inventory = current["repair_inventory"]
        self.assertFalse(inventory["comparable"])
        self.assertTrue(inventory["source_comparable"])
        self.assertEqual(inventory["source_closed_issue_ids"], ["forge-source"])
        self.assertIs(current["progress"]["inventory_advanced"], True)
        self.assertIs(current["progress"]["substantive_progress"], True)

    def test_caller_observation_is_not_a_closable_issue(self):
        observation = {"issue_id": "caller", "evidence": [
            {"source": "caller_observation", "detail": "reported"}
        ]}
        before = self.inventory("a" * 40, [observation])
        after = self.inventory("b" * 40, [])
        previous = build_repair_feedback(self.record())
        current = build_repair_feedback(self.record(candidate="b" * 40,
                                                    before_inventory=before,
                                                    after_inventory=after), previous=previous)
        self.assertEqual(current["repair_inventory"]["closed_issue_count"], 0)
        self.assertFalse(current["progress"]["inventory_advanced"])

    def test_missing_input_remains_full_inventory_closure_only(self):
        missing = {"issue_id": "missing", "evidence": [
            {"source": "missing_inputs", "detail": "contract"}
        ]}
        before = self.inventory("a" * 40, [missing])
        after = self.inventory("b" * 40, [])
        previous = build_repair_feedback(self.record())
        current = build_repair_feedback(self.record(candidate="b" * 40,
                                                    before_inventory=before,
                                                    after_inventory=after), previous=previous)
        inventory = current["repair_inventory"]
        self.assertEqual(inventory["closed_issue_ids"], ["missing"])
        self.assertEqual(inventory["source_closed_issue_count"], 0)
        self.assertFalse(current["progress"]["inventory_advanced"])

    def test_scope_hash_compares_entries_beyond_display_bound(self):
        before_files = [f"src/{index}.java" for index in range(40)]
        after_files = [*before_files]
        after_files[-1] = "src/different.java"
        before = {"complete": True, "candidate_id": "a" * 40,
                  "scan_scope": {"source_files": before_files}, "issue_ids": ["one"]}
        after = {"complete": True, "candidate_id": "b" * 40,
                 "scan_scope": {"source_files": after_files}, "issue_ids": []}
        previous = build_repair_feedback(self.record())
        current = build_repair_feedback(self.record(candidate="b" * 40,
                                                    before_inventory=before,
                                                    after_inventory=after), previous=previous)
        self.assertNotEqual(
            current["repair_inventory"]["before"]["scope"]["scope_sha256"],
            current["repair_inventory"]["after"]["scope"]["scope_sha256"],
        )
        self.assertFalse(current["repair_inventory"]["comparable"])
        self.assertEqual(current["progress"]["inventory_advanced"], "unknown")

    def test_replay_is_deterministic_and_bounded(self):
        record = self.record()
        record["updates"][1]["result"]["outputs"]["raw_log_refs"].extend(
            {"path": f"logs/{index}.log", "untrusted_body": "x" * 10_000}
            for index in range(20)
        )
        first = build_repair_feedback(record)
        second = build_repair_feedback(record)
        self.assertEqual(first, second)
        self.assertLessEqual(len(first["verification"]["log_refs"]), 8)
        self.assertNotIn("untrusted_body", repr(first))

    def test_other_target_is_incomparable(self):
        previous = build_repair_feedback(self.record())
        current_record = self.record(candidate="b" * 40)
        current_record["target_agent"] = "coder-b"
        current = build_repair_feedback(current_record, previous=previous)
        self.assertEqual(current["progress"]["comparable"], "unknown")
        self.assertEqual(current["progress"]["stalled"], "unknown")

    def test_missing_binding_is_unknown_and_render_distinguishes_absence(self):
        record = self.record()
        record["verification_execution_id"] = None
        record["updates"] = record["updates"][:1]
        feedback = build_repair_feedback(record)
        self.assertEqual(feedback["verification_current"], "unknown")
        self.assertIn("not executed", render_repair_feedback(feedback))
        failed = render_repair_feedback(build_repair_feedback(self.record()))
        self.assertIn("verification result review-1", failed)
        self.assertIn("wrapper recorded failed", failed)
        self.assertIn("logs/verify.log", failed)

    def test_executed_failure_remains_visible_when_candidate_binding_missing(self):
        record = self.record()
        record["updates"][1]["result"]["outputs"].pop("verification_candidate_id")
        feedback = build_repair_feedback(record)
        self.assertEqual(feedback["verification_current"], "unknown")
        rendered = render_repair_feedback(feedback)
        self.assertIn("Candidate binding: unknown", rendered)
        self.assertIn("wrapper recorded failed", rendered)

    def test_expected_verifier_is_distinct_from_waiting_reviewer(self):
        record = self.record()
        record["verification_execution_id"] = "different-execution"
        feedback = build_repair_feedback(record)
        self.assertFalse(feedback["verification_current"])
        self.assertEqual(feedback["verification"]["execution_id"], "review-1")
        self.assertEqual(feedback["expected_verification_execution_id"],
                         "different-execution")
        self.assertEqual(feedback["verification_execution_id"], "review-1")

    def test_text_artifact_refs_and_plain_log_are_summarized(self):
        record = self.record()
        outputs = record["updates"][1]["result"]["outputs"]
        outputs["log"] = "logs/direct.log"
        outputs["artifact_refs"] = {
            "receipt": {"path": "logs/receipt.txt", "media_type": "text/plain"},
            "binary": {"path": "data/blob", "media_type": "application/octet-stream"},
        }
        refs = build_repair_feedback(record)["verification"]["log_refs"]
        self.assertIn("logs/direct.log", refs)
        self.assertIn({"path": "logs/receipt.txt", "media_type": "text/plain"}, refs)
        self.assertNotIn({"path": "data/blob", "media_type": "application/octet-stream"}, refs)

    def test_missing_contract_distinguishes_unexecuted_behavior_check(self):
        record = self.record(detail="build stopped before behavior verification")
        outputs = record["updates"][0]["result"]["outputs"]
        outputs["candidate_workspace"] = "workspaces/rework-candidate"
        verification = record["updates"][1]["result"]["outputs"]
        verification.update(
            build_status="failed", build_error_code="contract_missing",
            build_executed=True, build_detail="functional contract is missing",
            verification_status="failed", verification_error_code="contract_missing",
            verification_executed=False,
            verification_detail="not run because required contract was unavailable",
        )
        feedback = build_repair_feedback(record)
        self.assertEqual(feedback["author"]["candidate_workspace"],
                         "workspaces/rework-candidate")
        self.assertTrue(feedback["verification"]["build_executed"])
        self.assertFalse(feedback["verification"]["verification_executed"])
        rendered = render_repair_feedback(feedback)
        self.assertIn("Build: executed=true; status=failed", rendered)
        self.assertIn("Behavior verification: executed=false; status=failed", rendered)
        self.assertIn("not run because required contract was unavailable", rendered)

    def test_native_inventory_shape_and_baseline_verification(self):
        inventory_base = {
            "schema_version": 1, "kind": "repair_inventory",
            "candidate_id": "a" * 40, "execution_id": "inventory-a",
            "issues": [{"issue_id": "legacy"}],
            "coverage": {"scan_complete": True, "truncated": False,
                         "limits": {"max_files": 10},
                         "excluded_directory_names": [".git"]},
        }
        inventory_after = {
            **inventory_base, "candidate_id": "b" * 40,
            "execution_id": "inventory-b", "issues": [],
        }
        baseline = self.record()["updates"][1]["result"]
        current = build_repair_feedback(self.record(candidate="b" * 40, status="completed",
                                                    detail="", run_id="run",
                                                    target_scope={"task": "scope-a"},
                                                    baseline_verification=baseline,
                                                    before_inventory=inventory_base,
                                                    after_inventory=inventory_after))
        self.assertIs(current["progress"]["verification_advanced"], True)
        self.assertEqual(current["progress"]["inventory_advanced"], "unknown")
        self.assertEqual(current["repair_inventory"]["before"]["issue_count"], 1)
        self.assertEqual(current["repair_inventory"]["after"]["issue_count"], 0)


if __name__ == "__main__":
    unittest.main()
