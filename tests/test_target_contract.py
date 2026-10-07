"""Source-reading target coverage and actual reusable Java session behavior."""
import json
import os
from pathlib import Path
import shutil
import subprocess
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from modport.artifact_verification import ArtifactTestReportHandler, prepare_artifact_runtime
from modport.artifact_verification_policy import required_behavior_assessments
from modport.contracts import OperationInput
from modport.evidence import atomic_json
from modport.target_contract import TargetContractFreezeHandler, validate_target_contract
from modport.target_session import target_session_support_files
from modport.workflow import WORKFLOW_VERSION


def requirements():
    return {'schema_version': 1, 'behaviors': [{'behavior_id': 'damage',
        'description': 'Damage updates health',
        'source_anchors': [{'path': 'src/Health.java', 'symbol': 'Health.damage'}],
        'assertions': [{'assertion_id': 'damage.health', 'expected': 'Health decreases by the configured damage',
                        'trigger': 'damage event'}]}]}


def target_contract():
    return {'schema_version': 1, 'behaviors': [{'id': 'damage', 'side': 'server',
        'preconditions': ['live target player'], 'action': ['apply damage'],
        'test_mapping': ['target.damage'], 'assertion_contracts': [
            {'assertion_id': 'damage.health', 'text': 'Health decreases by the configured damage',
             'test_ids': ['target.damage']}]}],
        'baseline_gradle_tasks': ['runGameTestServer'],
        'baseline_evidence_files': ['.modport/evidence/target.damage.json'],
        'test_evidence': {'target.damage': {'path': '.modport/evidence/target.damage.json',
            'evidence_kind': 'runtime', 'executor': 'gametest', 'runtime_operations': ['apply damage'],
            'test_source_files': ['.modport/harness/example/HealthGameTest.java'],
            'result_identity': {'kind': 'junit_xml', 'gradle_task': 'runGameTestServer',
                                'classname': 'example.HealthGameTest', 'name': 'damage'}}}}


class TargetContractTests(unittest.TestCase):
    def command(self, root, stage='target_contract_freeze', **kwargs):
        return OperationInput('run', stage, stage, stage, str(root),
                              options={'workflow_version': WORKFLOW_VERSION, 'validation_policy': {
                                  'scope': 'artifact_verification', 'required_behavior_completion': True}},
                              **kwargs)

    def test_freeze_preserves_symbol_anchors_and_independent_target_ids(self):
        with TemporaryDirectory() as folder:
            root = Path(folder)
            atomic_json(root / 'artifacts/requirements.json', {'schema_version': 1,
                'requirements': requirements(), 'source_commit': 'host-provided-source'})
            candidate = target_contract()
            atomic_json(root / 'worktree/.modport/functional-contract.json', candidate)
            source = root / 'worktree' / candidate['test_evidence']['target.damage']['test_source_files'][0]
            source.parent.mkdir(parents=True)
            source.write_text('class HealthGameTest {}')
            command = self.command(root, artifact_refs={'behavior_requirements': {
                'path': 'artifacts/requirements.json'}})
            result = TargetContractFreezeHandler()(command)
            self.assertEqual('completed', result.status, result.detail)
            lock_ref = result.outputs['artifact_refs']['functional_contract_lock']
            lock = json.loads((root / lock_ref['path']).read_text())
            self.assertEqual('host-provided-source', lock['contract']['source_fingerprint'])
            row = lock['contract']['behaviors'][0]['assertion_contracts'][0]
            self.assertEqual(['target.damage'], row['test_ids'])
            self.assertEqual(requirements()['behaviors'][0]['source_anchors'], row['source_anchors'])
            self.assertNotIn('source_anchor', row)
            self.assertFalse(lock['source_runtime_tested'])
            self.assertNotIn('sha256', lock_ref)
            self.assertEqual('source_reading', lock['verification_basis'])
            runtime = json.loads((root / 'worktree/.modport/functional-contract.json').read_text())
            self.assertEqual(lock['contract'], runtime)
            self.assertNotIn('contract', runtime)

    def test_frozen_runtime_contract_is_consumed_by_top_level_java_reader(self):
        if not shutil.which('javac') or not shutil.which('java'):
            self.skipTest('Java toolchain is unavailable')
        configured = os.environ.get('MODPORT_TEST_GSON_JAR')
        jars = [Path(configured)] if configured else sorted(
            (Path.home() / '.gradle/wrapper/dists').glob('gradle-*-bin/*/gradle-*/lib/gson-*.jar'))
        gson = next((path for path in reversed(jars) if path.is_file()), None)
        if gson is None:
            self.skipTest('Cached Gson jar is unavailable; set MODPORT_TEST_GSON_JAR')
        # The authored batch reader parses this path and accesses behaviors at
        # the root. Exercise those Gson calls across the real freeze boundary.
        reader = r'''import com.google.gson.JsonObject;
import com.google.gson.JsonParser;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.List;
public class TargetContractReader {
    public static void main(String[] args) throws Exception {
        Path root = Path.of(args[0]);
        JsonObject contract = JsonParser.parseString(Files.readString(
            root.resolve(".modport/functional-contract.json"))).getAsJsonObject();
        List<String> result = new ArrayList<>();
        String id = "target.damage";
        contract.getAsJsonArray("behaviors").forEach(b ->
            b.getAsJsonObject().getAsJsonArray("assertion_contracts").forEach(a -> {
                JsonObject assertion = a.getAsJsonObject();
                for (var test : assertion.getAsJsonArray("test_ids")) {
                    if (id.equals(test.getAsString())) result.add(assertion.get("assertion_id").getAsString());
                }
            }));
        if (!result.equals(List.of("damage.health"))) throw new AssertionError(result);
        JsonObject declaration = contract.getAsJsonObject("test_evidence").getAsJsonObject(id);
        if (!"junit".equals(declaration.get("executor").getAsString())) throw new AssertionError(declaration);
        if (!"author-value".equals(contract.get("reader_metadata").getAsString())) throw new AssertionError(contract);
        System.out.println("top-level target assertion mapping consumed after freeze");
    }
}'''
        with TemporaryDirectory() as folder:
            root = Path(folder)
            candidate = target_contract()
            candidate.update(source_commit='host-provided-source', reader_metadata='author-value')
            candidate['baseline_gradle_tasks'] = ['test']
            declaration = candidate['test_evidence']['target.damage']
            declaration.update(executor='junit', test_source_files=[
                '.modport/tests/TargetContractReader.java'])
            declaration['result_identity'].update(gradle_task='test', classname='TargetContractReader')
            runtime_path = root / 'worktree/.modport/functional-contract.json'
            atomic_json(runtime_path, candidate)
            # A previously authored hard link must not let freezing change a
            # delivered file outside the mutable harness contract.
            product_path = root / 'worktree/delivered.json'
            os.link(runtime_path, product_path)
            original_product = product_path.read_bytes()
            source = root / 'worktree' / declaration['test_source_files'][0]
            source.parent.mkdir(parents=True)
            source.write_text(reader)
            atomic_json(root / 'artifacts/requirements.json', requirements())
            result = TargetContractFreezeHandler()(self.command(root, artifact_refs={
                'behavior_requirements': {'path': 'artifacts/requirements.json'}}))
            self.assertEqual('completed', result.status, result.detail)
            self.assertEqual(original_product, product_path.read_bytes())
            compiled = subprocess.run(['javac', '-cp', str(gson), '-d', str(root), str(source)],
                                      capture_output=True, text=True, timeout=30)
            self.assertEqual(0, compiled.returncode, compiled.stderr)
            consumed = subprocess.run(['java', '-cp', os.pathsep.join((str(root), str(gson))),
                                       'TargetContractReader', str(root / 'worktree')],
                                      capture_output=True, text=True, timeout=30)
            self.assertEqual(0, consumed.returncode, consumed.stderr)
            self.assertIn('assertion mapping consumed after freeze', consumed.stdout)

    def test_missing_weakened_and_unmapped_assertions_cannot_freeze(self):
        for mutate in (
                lambda candidate: candidate['behaviors'][0].update(assertion_contracts=[]),
                lambda candidate: candidate['behaviors'][0]['assertion_contracts'][0].update(text='usually works'),
                lambda candidate: candidate['behaviors'][0]['assertion_contracts'][0].update(test_ids=['missing'])):
            candidate = target_contract()
            mutate(candidate)
            with self.subTest(candidate=candidate), self.assertRaises(ValueError):
                validate_target_contract(requirements(), candidate)

    def test_missing_test_source_returns_concrete_freeze_failure(self):
        with TemporaryDirectory() as folder:
            root = Path(folder)
            atomic_json(root / 'artifacts/requirements.json', requirements())
            atomic_json(root / 'worktree/.modport/functional-contract.json', target_contract())
            result = TargetContractFreezeHandler()(self.command(root, artifact_refs={
                'behavior_requirements': {'path': 'artifacts/requirements.json'}}))
            self.assertEqual('failed', result.status)
            self.assertEqual('target_contract_invalid', result.error_code)
            self.assertIn('HealthGameTest.java', result.detail)

    def test_target_only_assessment_uses_target_freeze_and_fails_missing_runtime(self):
        with TemporaryDirectory() as folder:
            root = Path(folder)
            candidate = validate_target_contract(requirements(), target_contract())
            atomic_json(root / 'artifacts/target.json', {'contract': candidate})
            effective = {'target_contract_freeze': {'outputs': {'artifact_refs': {
                'functional_contract_lock': {'path': 'artifacts/target.json'}}}},
                'artifact_test_execute': {'status': 'completed', 'outputs': {
                    'process_executed': True,
                    'case_results': {'target.damage': {'status': 'passed', 'test_outcome': 'passed'}},
                    'assertion_results': {'damage.health': {'status': 'passed'}},
                    'evidence_records': {'target.damage': {'evidence_kind': 'runtime',
                        'path': '.modport/evidence/target.damage.json'}}}}}
            assessment = required_behavior_assessments(self.command(root), root, effective)
            self.assertEqual({'target'}, set(assessment))
            self.assertEqual('passed', assessment['target']['status'])
            effective['artifact_test_execute']['outputs']['evidence_records'] = {}
            self.assertEqual('failed', required_behavior_assessments(
                self.command(root), root, effective)['target']['status'])

    def test_report_marks_source_reading_without_source_runtime_gate(self):
        with TemporaryDirectory() as folder:
            root = Path(folder)
            command = self.command(root, 'artifact_test_report')
            with patch('modport.artifact_verification.artifact_input', return_value=({}, None)), \
                    patch('modport.artifact_verification.file_digest', return_value='host-reference'), \
                    patch('modport.artifact_verification_policy.assess_target_selection',
                          return_value={'status': 'passed', 'gaps': []}):
                result = ArtifactTestReportHandler()(command)
            self.assertEqual('completed', result.status)
            report = json.loads((root / 'artifacts/artifact-verification-report.json').read_text())
            self.assertEqual({'target'}, set(report['required_behavior_assessments']))
            self.assertFalse(report['source_verification']['runtime_tested'])
            self.assertEqual('source_reading', report['source_verification']['verification_basis'])

    def test_author_compile_wiring_reads_live_declarations_before_freeze(self):
        with TemporaryDirectory() as folder:
            root = Path(folder)
            command = self.command(root, 'artifact_test_design')
            directory = root / 'artifacts/artifact-runtime/artifact_test_design/classes'
            directory.mkdir(parents=True)
            with patch('modport.artifact_verification.artifact_input', return_value=({}, None)):
                _, _, _, prepared = prepare_artifact_runtime(command)
            wiring = Path(prepared.options['artifact_init_script']).read_text()
            self.assertIn("p.file('.modport/functional-contract.json')", wiring)
            self.assertIn('t.setSource(p.files(junitSources))', wiring)
            self.assertIn('t.source(p.files(gameTestSources))', wiring)
            self.assertNotIn('__DECLARED_TEST_SOURCES__', wiring)
            self.assertNotIn('__GAMETEST_SOURCES__', wiring)


class SharedSessionTests(unittest.TestCase):
    def test_actual_java_session_launches_once_resets_and_retains_per_case_results(self):
        if not shutil.which('javac') or not shutil.which('java'):
            self.skipTest('Java toolchain is unavailable')
        probe = r'''import modport.harness.SharedGameSession;
import java.util.*;
public class SessionProbe {
    static int starts, before, after, closed, actions, writes;
    static SharedGameSession.Adapter<Integer> adapter(boolean breakReset) {
        return new SharedGameSession.Adapter<Integer>() {
            public Integer start() { starts++; return 1; }
            public void resetBefore(Integer session, String id) { before++; }
            public void resetAfter(Integer session, String id) { after++; if (breakReset) throw new IllegalStateException("reset failed"); }
            public void close(Integer session) { closed++; }
        };
    }
    static SharedGameSession.Case<Integer> test(String id, boolean passed) {
        return new SharedGameSession.Case<Integer>(id, Arrays.asList(id + ".assertion"), session -> {
            actions++;
            return new SharedGameSession.Observation(Collections.singletonMap(id + ".assertion", passed),
                Collections.singletonMap("health", 10));
        });
    }
    public static void main(String[] args) {
        SharedGameSession<Integer> group = new SharedGameSession<>(adapter(false), (id, observation) -> writes++,
            Arrays.asList(test("one", true), test("two", false), test("three", true)));
        group.requirePassed("one"); group.requirePassed("three");
        try { group.requirePassed("two"); throw new RuntimeException("failed case passed"); }
        catch (AssertionError expected) { }
        try { group.requirePassed("absent"); throw new RuntimeException("missing case passed"); }
        catch (AssertionError expected) { }
        if (starts != 1 || before != 3 || after != 3 || actions != 3 || closed != 1 || writes != 2)
            throw new RuntimeException("not one isolated batch");
        SharedGameSession<Integer> broken = new SharedGameSession<>(adapter(true), (id, observation) -> writes++,
            Arrays.asList(test("four", true), test("five", true)));
        Map<String, SharedGameSession.Receipt> receipts = broken.run();
        if (receipts.get("four").passed || receipts.get("five").passed || actions != 4 || writes != 2)
            throw new RuntimeException("failed isolation passed or executed following case");
        System.out.println("single session; per-case failure; missing case failure; reset isolation verified");
    }
}'''
        with TemporaryDirectory() as folder:
            root = Path(folder)
            support = root / 'modport/harness/SharedGameSession.java'
            support.parent.mkdir(parents=True)
            support.write_text(target_session_support_files()['modport/harness/SharedGameSession.java'])
            (root / 'SessionProbe.java').write_text(probe)
            compiled = subprocess.run(['javac', '-d', str(root), str(support), str(root / 'SessionProbe.java')],
                                      capture_output=True, text=True, timeout=30)
            self.assertEqual(0, compiled.returncode, compiled.stderr)
            result = subprocess.run(['java', '-cp', str(root), 'SessionProbe'],
                                    capture_output=True, text=True, timeout=30)
            self.assertEqual(0, result.returncode, result.stderr)
            self.assertIn('reset isolation verified', result.stdout)


if __name__ == '__main__':
    unittest.main()
