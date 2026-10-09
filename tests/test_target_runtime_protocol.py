"""Current target declarations cross freeze, selection and native XML matching."""

from copy import deepcopy
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from modport.contracts import OperationInput
from modport.opencode_shell_mcp import _read_junit_result
from modport.runtime_result_identity import validate_runtime_result_identity
from modport.target_contract import TargetContractFreezeHandler, validate_target_contract
from modport.test_selection_execution import build_selected_test_execution
from modport.workflow import WORKFLOW_VERSION


CASE_ID = 'hyperbox.target.capabilities'
FIXTURE = Path(__file__).parent / 'fixtures/target-runtime-protocol.json'


class TargetRuntimeProtocolTests(unittest.TestCase):
    def setUp(self):
        # A bounded extraction of the current source-reading requirements and
        # authored target declarations. The native identity uses the official
        # 26.1 reporter's structure ID and registry ID without a report adapter.
        fixture = json.loads(FIXTURE.read_text(encoding='utf-8'))
        self.requirements = fixture['requirements']
        self.contract = fixture['contract']

    def test_official_native_identity_crosses_freeze_selection_and_xml_reader(self):
        with TemporaryDirectory() as folder:
            root = Path(folder)
            workspace = root / 'worktree'
            for declaration in self.contract['test_evidence'].values():
                for relative in declaration['test_source_files']:
                    path = workspace / relative
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text('// authored test support\n', encoding='utf-8')
            contract_path = workspace / '.modport/functional-contract.json'
            contract_path.write_text(json.dumps(self.contract), encoding='utf-8')
            requirements_path = root / 'artifacts/requirements.json'
            requirements_path.parent.mkdir(parents=True)
            requirements_path.write_text(json.dumps({
                'schema_version': 1, 'requirements': self.requirements,
                'source_commit': 'host-provided-source',
            }), encoding='utf-8')
            command = OperationInput('run', 'freeze', 'target_contract_freeze', 'freeze', str(root),
                options={'workflow_version': WORKFLOW_VERSION}, artifact_refs={
                    'behavior_requirements': {'path': 'artifacts/requirements.json'}})
            result = TargetContractFreezeHandler()(command)
            self.assertEqual('completed', result.status, result.detail)
            lock = json.loads((root / result.outputs['artifact_refs'][
                'functional_contract_lock']['path']).read_text())
            identity = lock['test_evidence'][CASE_ID]['result_identity']
            self.assertEqual(self.contract['test_evidence'][CASE_ID]['result_identity'], identity)
            selected = build_selected_test_execution(lock['contract'], [CASE_ID],
                                                     workflow_version=WORKFLOW_VERSION)
            self.assertEqual((':runGameTestServer',), selected.gradle_tasks)
            self.assertIn('hyperbox:target_empty.hyperbox:target/capabilities',
                          selected.gradle_init_script)
            report = workspace / 'build/test-results/runGameTestServer/TEST-native.xml'
            report.parent.mkdir(parents=True)
            report.write_text('<testsuite><testcase classname="hyperbox:target_empty" '
                              'name="hyperbox:target/capabilities"/></testsuite>', encoding='utf-8')
            observed = _read_junit_result(workspace, CASE_ID, identity,
                                         require_isolated=False, include_xml_digest=False)
            self.assertEqual('passed', observed['outcome'])
            self.assertEqual(identity['classname'], observed['classname'])
            self.assertEqual(identity['name'], observed['name'])
            self.assertNotIn('xml_sha256', observed)

    def test_freeze_and_selection_reject_the_same_unsafe_native_literals(self):
        for name in ["hyperbox:target/capabilities'", 'hyperbox:target/*',
                     'hyperbox:target\\capabilities', 'hyperbox:target/capabilities\n']:
            candidate = deepcopy(self.contract)
            candidate['test_evidence'][CASE_ID]['result_identity']['name'] = name
            with self.subTest(name=name):
                with self.assertRaisesRegex(ValueError, 'unsafe.*result_identity name'):
                    validate_target_contract(self.requirements, candidate)
                with self.assertRaisesRegex(ValueError, 'unsafe.*result_identity name'):
                    build_selected_test_execution(candidate, [CASE_ID],
                                                  workflow_version=WORKFLOW_VERSION)

    def test_gradle_support_sources_preserve_containment_and_output_exclusions(self):
        for relative in ['.modport/target-sessions/native-reports.gradle',
                         '.modport/harness/runner.gradle.kts']:
            candidate = deepcopy(self.contract)
            candidate['test_evidence'][CASE_ID]['test_source_files'] = [relative]
            with self.subTest(relative=relative):
                normalized = validate_target_contract(self.requirements, candidate)
                self.assertEqual([relative], normalized['test_evidence'][CASE_ID]['test_source_files'])
        for relative in ['.modport/../runner.gradle', '/tmp/runner.gradle',
                         '.modport\\runner.gradle', '.modport/evidence/runner.gradle',
                         '.modport/target-sessions/build/runner.gradle']:
            candidate = deepcopy(self.contract)
            candidate['test_evidence'][CASE_ID]['test_source_files'] = [relative]
            with self.subTest(relative=relative), self.assertRaises(ValueError):
                validate_target_contract(self.requirements, candidate)

    def test_undeclared_and_unsafe_tasks_remain_distinct_failures(self):
        for task, message in [('otherTests', 'not declared'),
                              ('runGameTestServer/../../tmp', 'unsafe'),
                              ('runClient', 'unsafe')]:
            candidate = deepcopy(self.contract)
            candidate['test_evidence'][CASE_ID]['result_identity']['gradle_task'] = task
            with self.subTest(task=task):
                with self.assertRaisesRegex(ValueError, message):
                    validate_target_contract(self.requirements, candidate)
                with self.assertRaisesRegex(ValueError, message):
                    build_selected_test_execution(candidate, [CASE_ID],
                                                  workflow_version=WORKFLOW_VERSION)

    def test_native_resource_paths_do_not_expand_junit_class_or_method_syntax(self):
        identity = self.contract['test_evidence'][CASE_ID]['result_identity']
        self.assertEqual((':runGameTestServer', 'hyperbox:target_empty',
                          'hyperbox:target/capabilities'),
                         validate_runtime_result_identity(identity, native=True))
        with self.assertRaisesRegex(ValueError, 'unsafe.*classname'):
            validate_runtime_result_identity(identity, native=False)
        junit = {'kind': 'junit_xml', 'gradle_task': 'test',
                 'classname': 'example.Outer$Inner', 'name': 'checkState'}
        self.assertEqual((':test', 'example.Outer$Inner', 'checkState'),
                         validate_runtime_result_identity(junit, native=False))


if __name__ == '__main__':
    unittest.main()
