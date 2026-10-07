"""Current official GameTest XML is produced and read through one host path."""

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from modport.opencode_shell_mcp import _clear_junit_results, _read_junit_result
from modport.test_selection_execution import build_selected_test_execution
from modport.workflow import WORKFLOW_VERSION


def native_contract():
    return {'test_evidence': {
        test_id: {'evidence_kind': 'runtime', 'executor': 'gametest',
            'result_identity': {'kind': 'junit_xml', 'gradle_task': 'runGameTestServer',
                'classname': 'example:empty', 'name': 'example:' + test_id}}
        for test_id in ('registry', 'ore_filters')}}


class NativeGameTestReportTests(unittest.TestCase):
    def read(self, workspace, test_id='registry'):
        identity = native_contract()['test_evidence'][test_id]['result_identity']
        with patch('modport.opencode_shell_mcp.sha256',
                   side_effect=AssertionError('current result collection must not hash XML')):
            return _read_junit_result(workspace, test_id, identity,
                                      require_isolated=False, include_xml_digest=False)

    def write(self, workspace, xml, directory='runGameTestServer'):
        report = workspace / 'build/test-results' / directory / 'TEST-modport-gametest.xml'
        report.parent.mkdir(parents=True, exist_ok=True)
        report.write_text(xml)
        return report

    def test_host_selection_binds_native_report_and_preserves_selected_ids(self):
        selected = build_selected_test_execution(native_contract(),
            ['registry', 'ore_filters'], workflow_version=WORKFLOW_VERSION)
        self.assertEqual(('registry', 'ore_filters'), selected.test_ids)
        self.assertEqual((':runGameTestServer',), selected.gradle_tasks)
        self.assertIn("selectedTask.name + '/TEST-modport-gametest.xml'",
                      selected.gradle_init_script)
        self.assertIn("binding.run.programArguments.set(reportArguments)",
                      selected.gradle_init_script)
        self.assertIn("argument == '--report'", selected.gradle_init_script)
        self.assertIn("argument.startsWith('--report=')", selected.gradle_init_script)

    def test_official_nested_suite_preserves_exact_case_identity(self):
        with TemporaryDirectory() as folder:
            workspace = Path(folder)
            self.write(workspace, '<testsuite><testsuite>'
                '<testcase classname="example:empty" name="example:ore_filters"/>'
                '<testcase classname="example:empty" name="example:registry"/>'
                '</testsuite></testsuite>')
            for test_id in ('registry', 'ore_filters'):
                result = self.read(workspace, test_id)
                self.assertEqual('passed', result['outcome'])
                self.assertEqual(test_id, result['test_id'])
                self.assertEqual('example:' + test_id, result['name'])
                self.assertEqual(2, result['task_testcase_count'])
                self.assertEqual('build/test-results/runGameTestServer/TEST-modport-gametest.xml',
                                 result['xml_path'])
                self.assertNotIn('xml_sha256', result)

    def test_other_task_reports_cannot_satisfy_or_poison_selected_identity(self):
        with TemporaryDirectory() as folder:
            workspace = Path(folder)
            self.write(workspace, '<testsuite><testcase classname="example:empty" '
                'name="example:registry"/></testsuite>', directory='otherTask')
            self.write(workspace, '<malformed', directory='unrelatedTask')
            with self.assertRaisesRegex(ValueError, 'matched 0 results'):
                self.read(workspace)

    def test_duplicate_exact_identity_is_rejected(self):
        with TemporaryDirectory() as folder:
            workspace = Path(folder)
            self.write(workspace, '<testsuite>' +
                '<testcase classname="example:empty" name="example:registry"/>' * 2 +
                '</testsuite>')
            with self.assertRaisesRegex(ValueError, 'matched 2 results'):
                self.read(workspace)

    def test_namespaced_failure_error_and_skip_never_pass(self):
        with TemporaryDirectory() as folder:
            workspace = Path(folder)
            for child, outcome in (('failure', 'failed'), ('error', 'error'), ('skipped', 'skipped')):
                with self.subTest(child=child):
                    self.write(workspace, '<testsuites xmlns="urn:junit"><testsuite>'
                        '<testcase classname="example:empty" name="example:registry">'
                        '<' + child + ' message="runtime failure"/>'
                        '</testcase></testsuite></testsuites>')
                    self.assertEqual(outcome, self.read(workspace)['outcome'])

    def test_fresh_cleanup_prevents_old_report_acceptance(self):
        with TemporaryDirectory() as folder:
            workspace = Path(folder)
            report = self.write(workspace, '<testsuite><testcase classname="example:empty" '
                'name="example:registry"/></testsuite>')
            self.assertEqual([report.relative_to(workspace).as_posix()],
                             _clear_junit_results(workspace, validated_tasks=['runGameTestServer']))
            with self.assertRaisesRegex(ValueError, 'matched 0 results'):
                self.read(workspace)

    def test_native_subproject_is_rejected_without_directory_mapping(self):
        contract = native_contract()
        contract['test_evidence']['registry']['result_identity']['gradle_task'] = ':module:runGameTestServer'
        with self.assertRaisesRegex(ValueError, 'root-project Gradle task'):
            build_selected_test_execution(contract, ['registry'], workflow_version=WORKFLOW_VERSION)


if __name__ == '__main__':
    unittest.main()
