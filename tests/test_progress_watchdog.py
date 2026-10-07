import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from modport.progress_watchdog import (
    capture_progress, compare_progress, read_snapshot, write_snapshot,
)


class ProgressWatchdogTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.command = {'run_id': 'run-1', 'task_id': 'author',
                        'stage_id': 'artifact_test_design', 'command_id': 'run-1:author:1',
                        'attempt': 1, 'options': {}, 'artifact_refs': {}}
        self.write('worktree/src/main/java/Mod.java', 'class Mod {}')

    def write(self, relative, value):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(value)
        return path

    def capture(self):
        return capture_progress(self.root, self.command)

    def test_actual_content_change_counts_even_with_unchanged_mtime_and_size(self):
        path = self.root / 'worktree/src/main/java/Mod.java'
        original_time = path.stat().st_mtime_ns
        before = self.capture()
        path.write_text('class Mud {}')
        os.utime(path, ns=(original_time, original_time))
        summary = compare_progress(before, self.capture())
        self.assertTrue(summary['useful_progress'])
        self.assertEqual(['worktree/src/main/java/Mod.java'], summary['changed_paths'])

    def test_logs_cache_reports_and_heartbeat_are_activity_only(self):
        before = self.capture()
        for name in ('logs/activity.log', 'worktree/build/report.json',
                     'worktree/.gradle/cache.properties', 'worktree/report.json',
                     'worktree/.modport/agent-report.json'):
            self.write(name, '{"status":"passed"}')
        self.write('artifacts/executions/run-1:author:1/opencode-shell/result.json',
                   json.dumps({'tool': 'shell', 'exit_code': 1, 'status': 'failed'}))
        after = self.capture()
        summary = compare_progress(before, after)
        self.assertFalse(summary['useful_progress'])
        self.assertTrue(summary['activity'])
        self.assertTrue(all(Path(value).parts[0] == 'artifacts'
                            and (self.root / value).is_file() for value in after['evidence_paths']))

    def test_harness_and_explicit_goal_content_changes_count(self):
        self.write('worktree/.modport/harness/Test.java', 'class Test {}')
        self.write('artifacts/goals/current.md', 'Fix the codec')
        self.command['artifact_refs'] = {'coder_goal': {'path': 'artifacts/goals/current.md'}}
        before = self.capture()
        self.write('worktree/.modport/harness/Test.java', 'class Test { int result; }')
        self.write('artifacts/goals/current.md', 'Fix the codec and then test')
        result = compare_progress(before, self.capture())
        self.assertEqual(['artifacts/goals/current.md', 'worktree/.modport/harness/Test.java'],
                         result['changed_paths'])

    def test_current_junit_adapter_source_under_protocol_tests_counts(self):
        before = self.capture()
        self.write('worktree/.modport/tests/pkg/AdapterTest.java', 'class AdapterTest {}')
        after = self.capture()
        result = compare_progress(before, after)
        self.assertTrue(result['useful_progress'])
        self.assertEqual(['worktree/.modport/tests/pkg/AdapterTest.java'], result['changed_paths'])
        self.write('worktree/.modport/tests/pkg/AdapterTest.java', 'class AdapterTest { int fixed; }')
        self.assertTrue(compare_progress(after, self.capture())['useful_progress'])

    def test_case_improvement_counts_but_repeated_pass_or_failure_does_not(self):
        relative = 'artifacts/executions/run-1:author:1/receipt.json'

        def receipt(status):
            self.write(relative, json.dumps({'response': {'outputs': {
                'case_results': {'C01': {'status': status, 'test_outcome': status}}}}}))

        receipt('failed')
        failed = self.capture()
        receipt('failed')
        self.assertFalse(compare_progress(failed, self.capture())['useful_progress'])
        receipt('passed')
        passed = self.capture()
        improved = compare_progress(failed, passed)
        self.assertTrue(improved['useful_progress'])
        self.assertEqual('C01', improved['verification_improvements'][0]['test_id'])
        self.assertFalse(compare_progress(passed, self.capture())['useful_progress'])

    def test_characterization_selected_case_receipt_is_observed(self):
        before = self.capture()
        self.write('artifacts/executions/run-1:author:1/opencode-characterization/case.json',
                   json.dumps({'receipt_type': 'modport.characterization_verification.v1',
                               'command_id': self.command['command_id'],
                               'outcome': 'passed', 'test_results': [
                                   {'test_id': 'C02', 'outcome': 'passed'}]}))
        result = compare_progress(before, self.capture())
        self.assertEqual('C02', result['verification_improvements'][0]['test_id'])

    def test_first_observation_and_other_execution_do_not_claim_progress(self):
        first = self.capture()
        self.assertFalse(compare_progress(None, first)['useful_progress'])
        self.command['command_id'] = 'run-1:author:2'
        self.write('worktree/src/main/java/Mod.java', 'changed')
        result = compare_progress(first, self.capture())
        self.assertFalse(result['useful_progress'])
        self.assertIn('observations belong to different executions', result['limitations'])

    def test_baseline_and_explicit_isolated_workspace_selection(self):
        self.write('baseline/src/main/java/Base.java', 'baseline')
        self.command['stage_id'] = 'contract_review'
        baseline = self.capture()
        self.assertIn('baseline/src/main/java/Base.java', baseline['content'])
        self.assertNotIn('worktree/src/main/java/Mod.java', baseline['content'])
        self.write('workspaces/coder/src/Isolated.java', 'isolated')
        self.command.update(stage_id='coder', options={'workspace': 'workspaces/coder'})
        self.assertIn('workspaces/coder/src/Isolated.java', self.capture()['content'])

    def test_truncation_does_not_invent_deletion_and_is_explicit(self):
        before = self.capture()
        with patch('modport.progress_watchdog.MAX_FILE_BYTES', 4):
            after = self.capture()
        result = compare_progress(before, after)
        self.assertFalse(result['useful_progress'])
        self.assertFalse(after['content_complete'])
        self.assertTrue(any('byte limit' in item for item in result['limitations']))

    def test_actual_addition_and_deletion_count_on_complete_observations(self):
        before = self.capture()
        path = self.write('worktree/src/New.java', 'new')
        added = self.capture()
        self.assertEqual(['worktree/src/New.java'], compare_progress(before, added)['changed_paths'])
        path.unlink()
        self.assertEqual(['worktree/src/New.java'], compare_progress(added, self.capture())['changed_paths'])

    def test_symlink_content_and_workspace_escape_are_omitted(self):
        target = self.write('outside.java', 'outside')
        (self.root / 'worktree/src/Escape.java').symlink_to(target)
        snapshot = self.capture()
        self.assertNotIn('worktree/src/Escape.java', snapshot['content'])
        self.assertTrue(any('symlink omitted' in item for item in snapshot['limitations']))
        self.command['options'] = {'workspace': '../outside'}
        self.assertFalse(self.capture()['content_complete'])

    def test_snapshot_slots_round_trip_without_changing_original_evidence(self):
        evidence = self.write('artifacts/executions/run-1:author:1/raw.log', 'original evidence')
        snapshot = self.capture()
        relative = write_snapshot(self.root, self.command['command_id'], snapshot)
        self.assertEqual(snapshot, read_snapshot(self.root, relative))
        write_snapshot(self.root, self.command['command_id'], snapshot, slot='previous')
        self.assertEqual(2, len(list((self.root / Path(relative).parent).iterdir())))
        self.assertEqual('original evidence', evidence.read_text())
        with self.assertRaises(ValueError):
            write_snapshot(self.root, self.command['command_id'], snapshot, slot='archive')
        self.assertIsNone(read_snapshot(self.root, '../outside.json'))

    def test_snapshot_reader_and_writer_reject_symlink_slot_or_parent(self):
        snapshot = self.capture()
        relative = write_snapshot(self.root, self.command['command_id'], snapshot)
        slot = self.root / relative
        slot.unlink()
        slot.symlink_to(self.write('other.json', '{}'))
        self.assertIsNone(read_snapshot(self.root, relative))
        with self.assertRaises(ValueError):
            write_snapshot(self.root, self.command['command_id'], snapshot)

    def test_unregistered_reports_and_foreign_receipts_do_not_count(self):
        before = self.capture()
        document = {'case_results': {'C03': {'status': 'passed', 'test_outcome': 'passed'}}}
        self.write('artifacts/executions/run-1:author:1/agent-report.json', json.dumps(document))
        self.write('artifacts/executions/run-1:author:1/opencode-characterization/foreign.json',
                   json.dumps({'receipt_type': 'modport.characterization_verification.v1',
                               'command_id': 'other-execution', 'test_results': [
                                   {'test_id': 'C04', 'outcome': 'passed'}]}))
        self.assertFalse(compare_progress(before, self.capture())['useful_progress'])

    def test_receipt_bound_keeps_newest_outcomes(self):
        before = self.capture()
        for index, status in enumerate(('failed', 'failed', 'passed'), start=1):
            path = self.write(
                f'artifacts/executions/run-1:author:1/opencode-characterization/{index}.json',
                json.dumps({'receipt_type': 'modport.characterization_verification.v1',
                            'command_id': self.command['command_id'], 'test_results': [
                                {'test_id': 'C05', 'outcome': status}]}))
            os.utime(path, ns=(index * 1000000000, index * 1000000000))
        with patch('modport.progress_watchdog.MAX_RECEIPTS', 2):
            after = self.capture()
        self.assertEqual('passed', after['verification']['C05']['status'])
        self.assertTrue(compare_progress(before, after)['useful_progress'])
        self.assertTrue(any('newest receipts' in item for item in after['limitations']))

    def test_assignment_raw_error_is_readable_in_snapshot_and_log_churn_is_not_progress(self):
        self.command['options']['agent_assignment'] = 7
        self.write('artifacts/executions/run-1:author:1/input.json', '{}')
        self.write('logs/agent-author-7.log', 'Raw failure: package example.api does not exist\npassword=hidden\n')
        self.write('logs/agent-peer-7.log', 'unrelated failure')
        before = self.capture()
        excerpts = before['diagnostic_excerpts']
        self.assertEqual(['logs/agent-author-7.log'], [item['origin_path'] for item in excerpts])
        self.assertIn('package example.api does not exist', excerpts[0]['text'])
        self.assertNotIn('hidden', excerpts[0]['text'])
        self.assertGreater(excerpts[0]['origin_size'], 0)
        self.assertIn('origin_mtime', excerpts[0])
        self.assertEqual(['artifacts/executions/run-1:author:1/input.json'], before['evidence_paths'])
        relative = write_snapshot(self.root, self.command['command_id'], before)
        self.assertIn('package example.api does not exist',
                      read_snapshot(self.root, relative)['diagnostic_excerpts'][0]['text'])
        self.write('logs/agent-author-7.log', 'More raw failure: missing dependency\n')
        self.assertFalse(compare_progress(before, self.capture())['useful_progress'])

    def test_large_log_tail_is_bounded_and_symlink_log_is_not_read(self):
        self.command['options']['agent_assignment'] = 7
        self.write('logs/agent-author-7.log', 'old output\n' * 10000 + 'Raw failure at the end\n')
        excerpt = self.capture()['diagnostic_excerpts'][0]
        self.assertTrue(excerpt['truncated'])
        self.assertGreater(excerpt['offset'], 0)
        self.assertLessEqual(excerpt['sample_bytes'], 16 * 1024)
        self.assertIn('Raw failure at the end', excerpt['text'])
        (self.root / 'logs/agent-author-7.txt').symlink_to(self.write('outside.txt', 'private'))
        after = self.capture()
        self.assertFalse(any(item['origin_path'].endswith('.txt') for item in after['diagnostic_excerpts']))

    def test_fixed_registered_compile_failure_to_success_counts_once(self):
        directory = 'artifacts/executions/run-1:author:1/'
        self.write(directory + 'artifact-compile-session.json', json.dumps({
            'kind': 'artifact_compile', 'command_id': self.command['command_id'],
            'workspace': str(self.root / 'worktree'),
            'operation': {'options': {'artifact_init_script': 'artifact-init.gradle'}}}))
        path = directory + 'artifact-compile-' + 'a' * 32 + '.json'
        self.write(path, json.dumps({'acceptance_evidence': False,
                                    'tasks': ['compileJava', 'compileTestJava'],
                                    'exit_code': 1, 'stderr': 'Raw compiler failure'}))
        failed = self.capture()
        self.assertIn('Raw compiler failure', failed['diagnostic_excerpts'][0]['text'])
        self.write(path, json.dumps({'acceptance_evidence': False,
                                    'tasks': ['compileJava', 'compileTestJava'],
                                    'exit_code': 0, 'stdout': 'BUILD SUCCESSFUL'}))
        passed = self.capture()
        improved = compare_progress(failed, passed)
        self.assertTrue(improved['useful_progress'])
        self.assertEqual('artifact_harness_compile', improved['tool_improvements'][0]['context']['kind'])
        self.assertFalse(compare_progress(passed, self.capture())['useful_progress'])

    def test_project_compile_transition_requires_same_exact_context(self):
        path = 'artifacts/executions/run-1:author:1/opencode-shell/compile.json'

        def receipt(command, exit_code):
            self.write(path, json.dumps({'request_command_id': self.command['command_id'],
                                        'workspace': 'worktree', 'command_redacted': command,
                                        'command_truncated': False, 'exit_code': exit_code}))

        command = './gradlew --offline compileJava compileTestJava'
        receipt(command, 1)
        failed = self.capture()
        receipt(command, 0)
        self.assertTrue(compare_progress(failed, self.capture())['useful_progress'])
        receipt('./gradlew --offline compileJava', 0)
        self.assertFalse(compare_progress(failed, self.capture())['useful_progress'])
        receipt('echo repaired', 1)
        arbitrary = self.capture()
        receipt('echo repaired', 0)
        self.assertFalse(compare_progress(arbitrary, self.capture())['useful_progress'])


if __name__ == '__main__':
    unittest.main()
