import unittest

from modport.diagnostics import classify_characterization_failure, compare_characterization_progress


class DiagnosticsTests(unittest.TestCase):
    def classify(self, log, **kwargs):
        return classify_characterization_failure(log, **{"exit_code": 1, "timed_out": False, "phase": "baseline", **kwargs})

    def test_first_compile_task_and_raw_log_reference(self):
        value = self.classify('> Task :compileTestJava FAILED\n.modport/characterization/Test.java:42: error: cannot find symbol\n> Task :runClient FAILED', raw_log_refs=[{"path": "logs/raw.log"}])
        self.assertEqual(value["category"], "harness_compile")
        self.assertEqual(value["first_failed_task"], ":compileTestJava")
        self.assertEqual(value["raw_log_refs"], [{"path": "logs/raw.log"}])

    def test_nonfatal_audio_does_not_explain_timeout(self):
        value = self.classify('OpenAL initialization failed\nResources loaded\n', timed_out=True)
        self.assertEqual(value["category"], "unknown")
        self.assertEqual(value["error_code"], "execution_timeout")
        self.assertTrue(value["audio_capability_warning"])

    def test_display_failure_is_separate(self):
        self.assertEqual(self.classify('Failed to initialize GLFW')["category"], "environment")

    def test_failed_to_open_openal_is_a_capability_warning_not_a_timeout_cause(self):
        value = self.classify(
            'java.lang.IllegalStateException: Failed to open OpenAL device\n'
            '[minecraft/TextureAtlas]: Created: 1024x512x4 blocks.png-atlas\n',
            timed_out=True)
        self.assertTrue(value['audio_capability_warning'])
        self.assertEqual('unknown', value['category'])
        self.assertEqual('execution_timeout', value['error_code'])

    def test_cancellation_does_not_repair(self):
        value = self.classify('java.lang.AssertionError: x', cancelled=True)
        self.assertEqual(value["category"], "cancelled")
        self.assertFalse(value["repair_allowed"])
        self.assertFalse(compare_characterization_progress(value, value)["stalled"])

    def test_multiple_runtime_failures_retain_each_cause_without_authenticating_tests(self):
        # Two failures in the same synthetic verifier were previously
        # summarized only as the first assertion and two missing evidence IDs.
        log = (
            'java.lang.AssertionError: bundled minecraft:swamp biome is not registered\n'
            'MODPORT_RUNTIME_FAILURE difficulty.modes java.lang.AssertionError: bundled minecraft:swamp biome is not registered\n'
            'MODPORT_RUNTIME_FAILURE worldgen.ores java.lang.AssertionError: server feature gate did not retain requested value false\n')
        value = self.classify(log, exit_code=0,
                              missing_tests=['difficulty.modes', 'worldgen.ores'])
        self.assertEqual(value['category'], 'behavior_assertion')
        self.assertIn('swamp', value['normalized_failure'])
        self.assertEqual([(row['test_id'], row['log_line']) for row in value['runtime_failures']],
                         [('difficulty.modes', 2), ('worldgen.ores', 3)])
        self.assertIn('requested value false', value['runtime_failures'][1]['detail'])
        self.assertEqual(value['runtime_failure_trust'], 'untrusted_process_log')
        self.assertFalse(value['runtime_failures_truncated'])
        self.assertEqual(value['authenticated_test_ids'], [])
        self.assertEqual(value['evidence_counts'], {'authenticated': 0, 'missing': 2})
        cancelled = self.classify(log, cancelled=True)
        self.assertEqual(cancelled['category'], 'cancelled')
        self.assertFalse(cancelled['repair_allowed'])

    def test_runtime_failure_summary_is_bounded_and_deduplicated(self):
        marker = 'MODPORT_RUNTIME_FAILURE test failure\n'
        value = self.classify(marker * 100)
        self.assertEqual(len(value['runtime_failures']), 1)
        self.assertFalse(value['runtime_failures_truncated'])
        oversized = self.classify('MODPORT_RUNTIME_FAILURE test ' + 'x' * 3000)
        self.assertTrue(oversized['runtime_failures_truncated'])
        value = self.classify(''.join(
            f'MODPORT_RUNTIME_FAILURE test-{i} ' + 'x' * 3000 + '\n'
            for i in range(65)))
        self.assertEqual(len(value['runtime_failures']), 64)
        self.assertTrue(value['runtime_failures_truncated'])
        self.assertEqual(len(value['runtime_failures'][0]['detail']), 2000)

    def test_unknown_and_protocol(self):
        self.assertEqual(self.classify('unfamiliar output')["category"], "unknown")
        self.assertEqual(self.classify('', record_errors=['missing fresh evidence: a'])["category"], "evidence_protocol")

    def test_source_churn_is_ignored_for_progress(self):
        before = self.classify('java.lang.AssertionError: same', executor_provenance={"test": "old"})
        after = self.classify('java.lang.AssertionError: same', executor_provenance={"test": "new"})
        result = compare_characterization_progress(before, after)
        self.assertNotIn("source_changed", result)
        self.assertTrue(result["stalled"])

    def test_coverage_or_stage_advancement_is_progress(self):
        before = self.classify('java.lang.AssertionError: same', evidence_records={"a": {}})
        after = {**before, "authenticated_test_ids": ["b"], "last_milestone": "world_ready"}
        result = compare_characterization_progress(before, after)
        self.assertTrue(result["evidence_advanced"])
        self.assertTrue(result["milestone_advanced"])
        self.assertFalse(result["stalled"])

    def test_new_failure_not_stalled(self):
        before = self.classify('java.lang.AssertionError: one')
        after = self.classify('java.lang.AssertionError: two')
        self.assertFalse(compare_characterization_progress(before, after)["stalled"])

    def test_explicit_client_timeout_is_not_a_behavior_assertion(self):
        value = self.classify('java.lang.AssertionError: Client characterization timed out after eight minutes: stage=0')
        self.assertEqual(value["category"], "harness_runtime")
        self.assertEqual(value["error_code"], "client_timeout")

    def test_explicit_infrastructure_signal_and_cancel_priority(self):
        value = self.classify('', infrastructure_error='sandbox receipt missing')
        self.assertEqual(value["category"], "infrastructure")
        value = self.classify('', infrastructure_error='sandbox receipt missing', cancelled=True)
        self.assertEqual(value["category"], "cancelled")

    def test_synthetic_failure_excerpts(self):
        # Synthetic examples retain error categories without historical Run data.
        cases = [
            ('/workspace/.modport/characterization/src/main/java/modport/characterization/ServerCharacterization.java:167: error: name clash: awardStat(Stat<?>,int) in TrackingPlayer and awardStat(Stat,int) in FakePlayer have the same erasure, yet neither overrides the other', 'harness_compile'),
            ('/workspace/.modport/characterization/src/main/java/modport/characterization/ServerCharacterization.java:674: error: getLootModifierManager() is not public in ForgeInternalHandler; cannot be accessed from outside package', 'harness_compile'),
            ('/workspace/.modport/characterization/src/main/java/modport/characterization/ServerCharacterization.java:196: error: SERVER_SPEC has private access in ExampleConfig', 'harness_compile'),
            ('[12:00:00] [Server thread/ERROR] [minecraft/LogTestReporter]: characterize failed! example.persistence: player identity {actual=00000000-0000-0000-0000-000000000001, assertion_passed=false, expected=00000000-0000-0000-0000-000000000002}', 'behavior_assertion'),
            ('error: XDG_RUNTIME_DIR is invalid or not set in the environment.\njava.lang.IllegalStateException: Failed to initialize GLFW, errors: GLFW error during init: [0x1000E]134791544427632\n at com.mojang.blaze3d.platform.GLX._initGlfw(GLX.java:75)', 'environment'),
        ]
        for log, category in cases:
            with self.subTest(category=category, log=log):
                self.assertEqual(self.classify(log, record_errors=['missing fresh evidence: a'])["category"], category)

    def test_glfw_address_and_timestamp_are_not_new_failure(self):
        before = self.classify('[14:46:08] Failed to initialize GLFW, errors: GLFW error during init: [0x1000E]134791544427632')
        after = self.classify('[15:47:09] Failed to initialize GLFW, errors: GLFW error during init: [0x1000E]934791544427639')
        self.assertEqual(before["failure_signature"], after["failure_signature"])

    def test_diagnostics_do_not_emit_workspace_fingerprints(self):
        before = self.classify('', environment={"execution_id": "first", "timestamp": "today", "display": "available"})
        after = self.classify('', environment={"execution_id": "second", "timestamp": "tomorrow", "display": "available"})
        self.assertNotIn("environment_fingerprint", before)
        self.assertNotIn("candidate_sha256", before)
        self.assertNotIn("source_fingerprint", before)
        self.assertEqual(before["failure_signature"], after["failure_signature"])

    def test_launcher_identity_bound_failures_and_timeouts(self):
        for error, category in (("xvfb_unavailable", "environment"), ("display_start_failed", "environment"), ("workload_timeout", "harness_runtime"), ("preflight_timeout", "harness_runtime")):
            environment = {"schema_version": 1, "kind": "client_environment_diagnostic", "execution_id": "current", "acceptance_evidence": False, "error_code": error}
            value = self.classify('', environment=environment, execution_id='current', record_errors=['missing fresh evidence: test'])
            self.assertEqual(value['category'], category)
            self.assertEqual(value['error_code'], error)
            self.assertEqual(value['timed_out'], error.endswith('_timeout'))
            self.assertNotIn('workload_budget', value)
            for invalid in ({**environment, 'execution_id': 'stale'}, {**environment, 'kind': 'runtime_evidence'}, {**environment, 'schema_version': True}, {**environment, 'acceptance_evidence': True}):
                ignored = self.classify('', environment=invalid, execution_id='current', exit_code=124)
                self.assertEqual(ignored['category'], 'unknown')
                self.assertFalse(ignored['timed_out'])
        self.assertFalse(self.classify('', exit_code=124)['timed_out'])

    def test_short_host_workload_window_is_not_attributed_to_harness(self):
        environment = {"schema_version": 1, "kind": "client_environment_diagnostic",
                       "execution_id": "current", "acceptance_evidence": False,
                       "error_code": "workload_timeout"}
        budget = {"outer_seconds": 15.897, "launcher_seconds": 10.897,
                  "nominal_deadline_source": "sdk_stage"}
        result = self.classify('', execution_id='current', environment=environment,
                               workload_budget=budget)
        self.assertEqual(result['category'], 'infrastructure')
        self.assertEqual(result['error_code'], 'insufficient_workload_window')
        self.assertEqual(result['suggested_repair_scope'], 'host_executor')
        self.assertEqual(result['workload_budget'], budget)
        self.assertTrue(result['timed_out'])
        long_window = self.classify('', execution_id='current', environment=environment,
                                    workload_budget={"launcher_seconds": 3600})
        self.assertEqual(long_window['category'], 'harness_runtime')

    def test_compile_scope_requires_source_attribution(self):
        for path, expected, scope in (
            ('src/main/java/Product.java', 'product_compile', 'product_sources'),
            ('/workspace/.modport/characterization/Test.java', 'harness_compile', 'harness_sources'),
            ('Mystery.java', 'unknown', 'independent_diagnosis'),
        ):
            value = self.classify(path + ':12: error: cannot find symbol', record_errors=['missing fresh evidence: test'])
            self.assertEqual(value['category'], expected)
            self.assertEqual(value['suggested_repair_scope'], scope)
        value = self.classify('src/test/java/Fixture.java:12: error: cannot find symbol', executor_provenance={'test': {'test_source_files': {'src/test/java/Fixture.java': 'sha'}}})
        self.assertEqual(value['category'], 'harness_compile')
        self.assertEqual(self.classify('Compilation failed; see compiler error output')['category'], 'unknown')

    def test_product_syntax_error_is_not_harness_failure(self):
        value = self.classify('/workspace/src/main/java/Product.java:8: error: unclosed string literal')
        self.assertEqual(value['category'], 'product_compile')
        self.assertEqual(value['suggested_repair_scope'], 'product_sources')
