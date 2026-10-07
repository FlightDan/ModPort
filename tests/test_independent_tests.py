"""Independent-suite gate regressions; models and Gradle are never executed."""
import copy
from dataclasses import replace
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch
from xml.etree import ElementTree

from modport.characterization import BehaviorEntry, CharacterizationContract
from modport.contracts import OperationInput
from modport.evidence import atomic_json, candidate_fingerprint, digest, file_digest, seal_ref
from modport.handlers import _result
from modport.independent_tests import build_test_registry
from modport.rubric import acceptance_rubric


PASS_XML = '<testsuite name="independent" tests="1" failures="0" errors="0" skipped="0"><testcase classname="Independent" name="preservesValue"/></testsuite>'


def suite():
    return {'schema_version': 1, 'init_script': 'init.gradle', 'tests': [
        {'id': 'independent.value', 'behavior_ids': ['value'], 'sources': ['Independent.java'],
         'task': ':independentTest', 'report_paths': ['build/test-results/independent/TEST-Independent.xml']}]}


class IndependentTestTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.work = self.root / 'worktree'
        self.work.mkdir()
        self.product = self.work / 'src/main/java/example/build/Logic.java'
        self.product.parent.mkdir(parents=True)
        self.product.write_text('package example.build; public class Logic { public int value() { return 2; } }\n')
        (self.work / 'build.gradle').write_text("plugins { id 'java' }\n")
        (self.work / 'settings.gradle').write_text("rootProject.name = 'fixture'\n")
        (self.work / 'gradlew').write_text('# fixture wrapper, never run\n')
        subprocess.run(['git', 'init', '-q'], cwd=self.work, check=True, capture_output=True)
        subprocess.run(['git', 'add', '.'], cwd=self.work, check=True)
        subprocess.run(['git', '-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid',
                        'commit', '-qm', 'fixture'], cwd=self.work, check=True)
        (self.work / '.modport/tests').mkdir(parents=True)
        (self.work / '.modport/tests/Frozen.java').write_text('class Frozen { /* existing characterization */ }\n')
        (self.work / '.modport/functional-contract.json').write_text('{"existing":"contract copy"}\n')
        (self.work / 'build').mkdir()
        (self.work / 'build/stale.xml').write_text(PASS_XML)
        rubric = acceptance_rubric()
        atomic_json(self.root / 'artifacts/acceptance-rubric.json', rubric)
        typed = CharacterizationContract(entries=(
            BehaviorEntry('value', 'src/main/java/example/build/Logic.java', assertions=('value is 2',)),
            BehaviorEntry('visual', 'client display', side='client', assertions=('display remains visible',))))
        lock = {'schema_version': 1, 'contract': typed.to_dict(),
                'acceptance_rubric': {key: rubric[key] for key in ('rubric_id', 'rubric_version', 'rubric_sha256')}}
        lock['lock_sha256'] = digest(lock)
        atomic_json(self.root / 'artifacts/functional-contract.lock.json', lock)
        self.refs = {key: {'path': path, 'sha256': file_digest(self.root / path)} for key, path in (
            ('acceptance_rubric', 'artifacts/acceptance-rubric.json'),
            ('functional_contract_lock', 'artifacts/functional-contract.lock.json'))}
        self.refs['acceptance_rubric']['metadata'] = {'rubric_sha256': rubric['rubric_sha256']}
        self.options = {'acceptance_rubric_sha256': rubric['rubric_sha256']}
        self.payload = {'locked_artifacts': {'contract_lock_sha256': lock['lock_sha256']}}
        self.registry = build_test_registry()
        self.index = 0

    def command(self, stage, *, refs=None, workspace=None):
        self.index += 1
        return OperationInput('run', stage, stage, f'{stage}-{self.index}', str(self.root),
            payload=self.payload, options={**self.options, 'workspace': workspace or f'workspaces/tests/{self.index}'},
            artifact_refs=self.refs if refs is None else refs)

    def design(self, *, value=None, mutation=None, agent_status='completed', create_suite=True, command=None):
        command = command or self.command('test_design')
        def fake(_handler, operation):
            root = self.root / operation.options['workspace']
            if create_suite:
                directory = root / '.modport/independent-tests'
                directory.mkdir(parents=True)
                (directory / 'init.gradle').write_text('// fixture independent Gradle task setup\n')
                (directory / 'Independent.java').write_text('class Independent { void check() { assert new example.build.Logic().value() == 2; } }\n')
                (directory / 'suite.json').write_text(json.dumps(suite() if value is None else value))
            if mutation:
                mutation(root)
            return _result(operation, agent_status, detail='fixture design response')
        with patch('modport.handlers.CodexStageHandler.__call__', fake):
            return self.registry['test_design'](command)

    def execution_command(self, designed, *, sealed=False):
        refs = {**self.refs, **designed.outputs['artifact_refs']}
        if sealed:
            refs = {key: seal_ref(self.root, ref, execution_id='fixture-seal') for key, ref in refs.items()}
        return self.command('test_execute', refs=refs, workspace=designed.outputs['workspace'])

    def execute(self, designed, *, xml=PASS_XML, returncode=0, mutation=None, command=None, timeout=False,
                observation_mutation=None):
        command = command or self.execution_command(designed)
        self.last_command = None
        def sandbox(root, workspace, args, **kwargs):
            self.assertEqual(root, self.root)
            self.last_cache_name = kwargs['cache_name']
            if command.payload.get('regression_scope'):
                self.assertTrue(kwargs['cache_name'].startswith('independent-test-gradle-cache-'))
            else:
                self.assertEqual('independent-test-gradle-cache', kwargs['cache_name'])
            self.assertEqual(self.root / 'locked-jdk', kwargs['java_home'])
            return ['bwrap', '--bind', str(workspace), '/workspace', '--chdir', '/workspace', *args]
        def fake(args, *, cwd, log, timeout=None):
            self.last_command = list(args)
            expected = (f'workspaces/tests/{command.stage_id}-{command.command_id}'
                        if command.options.get('workflow_version', 12) >= 13 else command.options['workspace'])
            self.assertEqual(cwd, self.root / expected)
            log.write_text('fixture Gradle execution, not real mod acceptance\n')
            if mutation:
                mutation(cwd)
            if xml is not None:
                declaration = json.loads((cwd / '.modport/independent-tests/suite.json').read_text())
                for test in declaration['tests']:
                    for relative in test['report_paths']:
                        report = cwd / relative
                        report.parent.mkdir(parents=True, exist_ok=True)
                        report.write_text(xml)
            if command.payload.get('regression_scope'):
                nonce = Path(args[-1]).name.split('.')[0]
                observed = {}
                declaration = json.loads((cwd / '.modport/independent-tests/suite.json').read_text())
                for task in {test['task'] for test in declaration['tests']}:
                    paths = {path for test in declaration['tests'] if test['task'] == task for path in test['report_paths']}
                    cases = [case for path in paths if (cwd / path).is_file()
                             for case in ElementTree.parse(cwd / path).getroot().iter('testcase')]
                    observed[task] = {'executed': True, 'tests': len(cases), 'failures': 0,
                                     'skipped': sum(case.find('skipped') is not None for case in cases),
                                     'report_directory': '/workspace/' + str(Path(sorted(paths)[0]).parent)}
                record = {'nonce': nonce, 'tasks': observed}
                if observation_mutation:
                    observation_mutation(record)
                path = cwd / 'build/.modport-regression' / (nonce + '.json')
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(json.dumps(record))
            if timed_out:
                raise subprocess.TimeoutExpired(args, timeout)
            return subprocess.CompletedProcess(args, returncode, 'fixture stdout')
        timed_out = timeout
        with patch('modport.handlers._sandboxed_build_command', sandbox), \
             patch('modport.handlers._locked_java_home', return_value=self.root / 'locked-jdk'), \
             patch('modport.handlers._exec', fake):
            return self.registry['test_execute'](command)

    def assert_failed(self, result):
        self.assertEqual('failed', result.status, result.detail)
        self.assertEqual('independent_test_failed', result.error_code)

    def test_design_copies_untracked_contract_tests_and_does_not_share_git(self):
        before = candidate_fingerprint(self.work)
        result = self.design()
        self.assertEqual('completed', result.status, result.detail)
        clone = self.root / result.outputs['workspace']
        self.assertFalse((clone / '.git').exists())
        self.assertFalse((clone / 'build').exists())
        self.assertEqual((self.work / '.modport/tests/Frozen.java').read_bytes(),
                         (clone / '.modport/tests/Frozen.java').read_bytes())
        self.assertEqual(before, candidate_fingerprint(self.work))
        self.assertNotIn('candidate_sha256', result.outputs)
        self.assertEqual(['value'], result.outputs['covered_behavior_ids'])
        self.assertEqual(['visual'], result.outputs['uncovered_behavior_ids'])
        self.assertIn('independent_test_snapshot', result.outputs['artifact_refs'])
        self.assertIn('independent_test_source:Independent.java', result.outputs['artifact_refs'])

    def test_task_refs_execute_fresh_tests_readonly_and_record_result(self):
        designed = self.design()
        command = self.execution_command(designed, sealed=True)
        for ref in command.artifact_refs.values():
            self.assertTrue((self.root / ref['path']).is_file())
            self.assertFalse(ref['path'].startswith('artifacts/objects/'))
        result = self.execute(designed, command=command)
        self.assertEqual('completed', result.status, result.detail)
        clone = str(self.root / designed.outputs['workspace'])
        self.assertNotIn(['--bind', clone, '/workspace'], [self.last_command[i:i + 3] for i in range(len(self.last_command))])
        self.assertIn(['--ro-bind', clone, '/workspace'], [self.last_command[i:i + 3] for i in range(len(self.last_command))])
        self.assertIn(['--bind', clone + '/build', '/workspace/build'], [self.last_command[i:i + 3] for i in range(len(self.last_command))])
        self.assertIn('--rerun-tasks', self.last_command)
        self.assertIn('--no-build-cache', self.last_command)
        self.assertEqual(['value'], result.outputs['covered_behavior_ids'])
        self.assertFalse(result.outputs['frozen_characterization_gate_replaced'])
        ref = result.outputs['artifact_refs']['independent_test_result']
        record = json.loads((self.root / ref['path']).read_text())
        self.assertEqual(command.command_id, record['execution_command_id'])
        self.assertNotIn('candidate_sha256', record)
        self.assertEqual(designed.outputs['suite_sha256'], record['suite_sha256'])
        report = next(iter(record['reports'].values()))
        self.assertEqual(1, report['tests'])
        self.assertEqual(64, len(report['sha256']))
        self.assertEqual(file_digest(self.root / ref['path']), ref['sha256'])

    def test_subset_allowed_but_unknown_behavior_rejected(self):
        value = suite(); value['tests'][0]['behavior_ids'] = ['unknown']
        self.assert_failed(self.design(value=value))
        value['tests'][0]['behavior_ids'] = ['value']
        self.assertEqual('completed', self.design(value=value).status)

    def test_invalid_suite_schema_empty_duplicate_tests_and_sources_fail(self):
        variants = [[], {'schema_version': True}, {'schema_version': 1, 'tests': []}]
        value = suite(); value['tests'].append(copy.deepcopy(value['tests'][0])); variants.append(value)
        for field in ('behavior_ids', 'sources', 'report_paths'):
            value = suite(); value['tests'][0][field] = []; variants.append(value)
        for value in variants:
            with self.subTest(value=value):
                self.assert_failed(self.design(value=value))
        self.assert_failed(self.design(mutation=lambda w: (w / '.modport/independent-tests/Independent.java').write_text('  \n')))
        self.assert_failed(self.design(mutation=lambda w: (w / '.modport/independent-tests/init.gradle').write_text('')))
        self.assert_failed(self.design(mutation=lambda w: (w / '.modport/independent-tests/Independent.java').unlink()))

    def test_paths_reject_absolute_traversal_symlink_and_report_source_targets(self):
        for field, values in (
            ('sources', ['/tmp/Test.java', '../tests/Frozen.java', 'x/../Independent.java', 'C:/Test.java']),
            ('report_paths', ['/tmp/result.xml', '../result.xml', 'src/result.xml', 'build/../build/result.xml', '.modport/result.xml', 'build/*.xml'])):
            for value in values:
                with self.subTest(field=field, value=value):
                    data = suite(); data['tests'][0][field] = [value]
                    result = self.design(value=data)
                    self.assert_failed(result)
        def symlink(work):
            path = work / '.modport/independent-tests/Independent.java'
            path.unlink(); path.symlink_to(work / '.modport/tests/Frozen.java')
        self.assert_failed(self.design(mutation=symlink))
        data = suite(); data['init_script'] = '../init.gradle'
        self.assert_failed(self.design(value=data))

    def test_shell_options_and_multitask_strings_rejected(self):
        for task in ['test;echo hacked', '--init-script', 'test other', '$(whoami)', 'test\nhelp', ':', 'test/other']:
            with self.subTest(task=task):
                value = suite(); value['tests'][0]['task'] = task
                self.assert_failed(self.design(value=value))

    def test_design_allows_workspace_file_updates(self):
        for relative in ['build.gradle', 'settings.gradle', '.modport/tests/Frozen.java',
                         '.modport/functional-contract.json', 'src/main/java/example/build/Logic.java']:
            with self.subTest(relative=relative):
                self.assertEqual('completed', self.design(mutation=lambda w: (w / relative).write_text('updated')).status)
        def output(work):
            (work / 'build').mkdir(); (work / 'build/fake.xml').write_text(PASS_XML)
        self.assertEqual('completed', self.design(mutation=output).status)
        self.assertEqual('completed', self.design(mutation=lambda w: (w / 'new-config.txt').write_text('new')).status)

    def test_agent_failure_and_missing_declaration_return_rework_failure(self):
        self.assert_failed(self.design(agent_status='failed'))
        self.assert_failed(self.design(create_suite=False))

    def test_main_update_during_design_allowed(self):
        self.assertEqual('completed', self.design(mutation=lambda w: self.product.write_text('main updated')).status)

    def test_unsafe_or_reused_workspace_rejected_before_agent(self):
        for relative in ['worktree', '/tmp/workspace', 'workspaces/tests/../escape', 'workspaces/tests/a/nested']:
            with self.subTest(relative=relative), patch('modport.handlers.CodexStageHandler.__call__') as agent:
                command = self.command('test_design', workspace=relative)
                self.assert_failed(self.registry['test_design'](command)); agent.assert_not_called()
        result = self.design()
        command = self.command('test_design', workspace=result.outputs['workspace'])
        with patch('modport.handlers.CodexStageHandler.__call__') as agent:
            self.assert_failed(self.registry['test_design'](command)); agent.assert_not_called()

    def test_changed_main_candidate_allows_execution(self):
        designed = self.design(); self.product.write_text('main changed')
        result = self.execute(designed)
        self.assertEqual('completed', result.status, result.detail)
        self.assertIsNotNone(self.last_command)

    def test_changed_suite_and_clone_sources_allow_execution(self):
        for relative in ['.modport/independent-tests/Independent.java', '.modport/independent-tests/init.gradle',
                         '.modport/tests/Frozen.java',
                         'src/main/java/example/build/Logic.java']:
            with self.subTest(relative=relative):
                designed = self.design()
                (self.root / designed.outputs['workspace'] / relative).write_text('tampered')
                result = self.execute(designed)
                self.assertEqual('completed', result.status, result.detail)
                self.assertIsNotNone(self.last_command)

    def test_invalid_updated_suite_fails_before_execution(self):
        designed = self.design()
        (self.root / designed.outputs['workspace'] / '.modport/independent-tests/suite.json').write_text('{}')
        self.assert_failed(self.execute(designed))
        self.assertIsNone(self.last_command)

    def test_symlink_report_parent_fails_without_deleting_target(self):
        designed = self.design()
        clone = self.root / designed.outputs['workspace']
        victim = self.root / 'victim'; victim.mkdir()
        file = victim / 'TEST-Independent.xml'; file.write_text(PASS_XML)
        (clone / 'build/test-results').mkdir(parents=True)
        (clone / 'build/test-results/independent').symlink_to(victim, target_is_directory=True)
        self.assert_failed(self.execute(designed)); self.assertIsNone(self.last_command)
        self.assertEqual(PASS_XML, file.read_text())

    def test_stale_report_deleted_and_missing_replacement_fails(self):
        designed = self.design()
        clone = self.root / designed.outputs['workspace']
        report = clone / suite()['tests'][0]['report_paths'][0]
        report.parent.mkdir(parents=True); report.write_text(PASS_XML)
        result = self.execute(designed, xml=None)
        self.assert_failed(result)
        self.assertFalse(report.exists())
        self.assertIn('independent_test_result', result.outputs['artifact_refs'])

    def test_exit_failure_rejects_passing_xml(self):
        result = self.execute(self.design(), returncode=1)
        self.assert_failed(result)
        self.assertEqual(1, result.outputs['exit_code'])

    def test_failed_missing_empty_skipped_forged_and_malformed_reports_fail(self):
        variants = [None, '<testsuite tests="0" failures="0" errors="0"/>',
                    '<testsuite tests="1" failures="0" errors="0"/>',
                    '<testsuite tests="1" failures="1" errors="0"><testcase name="fails"><failure/></testcase></testsuite>',
                    '<testsuite tests="1" failures="0" errors="1"><testcase name="errors"><error/></testcase></testsuite>',
                    '<testsuite tests="1" failures="0" errors="0" skipped="1"><testcase name="skip"><skipped/></testcase></testsuite>',
                    '<testsuite tests="1" failures="0" errors="0"><testcase name="lying"><failure/></testcase></testsuite>',
                    '<testsuite tests="1" failures="0" errors="0" skipped="0"><testcase name="lying"><skipped/></testcase></testsuite>',
                    '<invalid>', '<!DOCTYPE x [<!ENTITY value "x">]>' + PASS_XML]
        for xml in variants:
            with self.subTest(xml=xml):
                result = self.execute(self.design(), xml=xml)
                self.assert_failed(result)
                if xml is not None:
                    report = next(iter(result.outputs['reports'].values()))
                    self.assertIn('sha256', report)

    def test_partial_skip_allowed_but_each_report_must_have_executed_cases(self):
        xml = '<testsuite tests="2" failures="0" errors="0" skipped="1"><testcase name="ok"/><testcase name="skip"><skipped/></testcase></testsuite>'
        designed = self.design()
        command = self.execution_command(designed)
        command = replace(command, options={**command.options, 'workflow_version': 11})
        result = self.execute(designed, xml=xml, command=command)
        self.assertEqual('completed', result.status, result.detail)
        self.assertEqual(1, next(iter(result.outputs['reports'].values()))['skipped'])

    def test_nested_junit_aggregate_counts_are_not_double_counted(self):
        xml = '<testsuites tests="1" failures="0" errors="0">' + PASS_XML + '</testsuites>'
        result = self.execute(self.design(), xml=xml)
        self.assertEqual('completed', result.status, result.detail)
        self.assertEqual(1, next(iter(result.outputs['reports'].values()))['tests'])

    def test_file_updates_do_not_invalidate_passing_execution(self):
        for relative in ['build.gradle', '.modport/independent-tests/Independent.java',
                         '.modport/independent-tests/init.gradle', 'src/main/java/example/build/Logic.java']:
            with self.subTest(relative=relative):
                result = self.execute(self.design(), mutation=lambda w: (w / relative).write_text('tampered'))
                self.assertEqual('completed', result.status, result.detail)

    def test_timeout_cannot_pass_with_xml(self):
        result = self.execute(self.design(), timeout=True)
        self.assert_failed(result)
        self.assertTrue(result.outputs['timed_out'])

    def test_changed_frozen_contract_or_snapshot_fails_before_exec(self):
        designed = self.design()
        command = self.execution_command(designed, sealed=True)
        canonical = self.root / 'artifacts/functional-contract.lock.json'
        original = canonical.read_bytes()
        canonical.write_text('{}')
        self.assert_failed(self.execute(designed, command=command)); self.assertIsNone(self.last_command)
        canonical.write_bytes(original)
        ref = command.artifact_refs['independent_test_snapshot']
        (self.root / ref['path']).write_text('{}')
        self.assert_failed(self.execute(designed, command=command)); self.assertIsNone(self.last_command)

    def test_scoped_design_requires_exact_assigned_coverage(self):
        for ids in (['visual'], ['value', 'visual']):
            scope = {'scope_id': 'scope-001', 'behavior_ids': ids, 'gap_obligations': []}
            command = replace(self.command('test_design'), payload={**self.payload, 'regression_scope': scope})
            self.assert_failed(self.design(command=command))

    def scoped_branch(self, index, behavior):
        scope = {'scope_id': f'scope-{index:03d}', 'behavior_ids': [behavior], 'gap_obligations': []}
        payload = {**self.payload, 'regression_scope': scope}
        command = replace(self.command('test_design'), payload=payload, task_id=f'test_design.g1.scope-{index:03d}')
        value = suite()
        value['tests'][0]['behavior_ids'] = [behavior]
        designed = self.design(value=value, command=command)
        self.assertEqual('completed', designed.status, designed.detail)
        command = replace(self.execution_command(designed), payload=payload, task_id=f'test_execute.g1.scope-{index:03d}')
        executed = self.execute(designed, command=command)
        self.assertEqual('completed', executed.status, executed.detail)
        return scope, designed, executed

    def join_command(self, branches):
        return replace(self.command('test_execute'), payload={**self.payload,
            'regression_generation': 1, 'regression_scopes': [row[0] for row in branches],
            'regression_designs': [row[1].to_dict() for row in branches],
            'regression_results': [row[2].to_dict() for row in branches]})

    def test_scoped_executions_use_separate_caches_and_join_all_evidence(self):
        first = self.scoped_branch(1, 'value')
        first_cache = self.last_cache_name
        second = self.scoped_branch(2, 'visual')
        self.assertNotEqual(first_cache, self.last_cache_name)
        with patch('modport.handlers._exec') as execute:
            result = self.registry['test_execute'](self.join_command([first, second]))
        execute.assert_not_called()
        self.assertEqual('completed', result.status, result.detail)
        self.assertEqual(['value', 'visual'], result.outputs['covered_behavior_ids'])
        self.assertEqual([], result.outputs['uncovered_behavior_ids'])
        refs = result.outputs['artifact_refs']
        self.assertIn('independent_test_result', refs)
        for index in (1, 2):
            self.assertIn(f'regression:test_execute.g1.scope-{index:03d}:independent_test_result', refs)
            self.assertIn(f'regression:test_design.g1.scope-{index:03d}:independent_test_snapshot', refs)

    def test_scoped_execution_rejects_skips_and_unrelated_task_reports(self):
        scope, designed, executed = self.scoped_branch(1, 'value')
        mixed = ('<testsuite tests="2" failures="0" errors="0" skipped="1">'
                 '<testcase name="pass"/><testcase name="skip"><skipped/></testcase></testsuite>')
        command = replace(self.execution_command(designed), payload={**self.payload, 'regression_scope': scope})
        self.assert_failed(self.execute(designed, command=command, xml=mixed))
        for change in (lambda record: record['tasks'].clear(),
                       lambda record: record['tasks'][':independentTest'].update(report_directory='/workspace/build/unrelated'),
                       lambda record: record['tasks'][':independentTest'].update(tests=2)):
            command = replace(self.execution_command(designed), payload={**self.payload, 'regression_scope': scope})
            self.assert_failed(self.execute(designed, command=command, observation_mutation=change))

    def test_join_rejects_missing_duplicate_failed_and_misattributed_scopes(self):
        first = self.scoped_branch(1, 'value')
        second = self.scoped_branch(2, 'visual')
        commands = [self.join_command([first]), self.join_command([first, first])]
        for change in ({'status': 'failed'}, {'command_id': 'other-execution'},
                       {'task_id': 'test_execute.g2.scope-001'}):
            command = self.join_command([first, second])
            command.payload['regression_results'][0].update(change)
            commands.append(command)
        for generation in (2, True, 0, None):
            command = self.join_command([first, second])
            command.payload['regression_generation'] = generation
            commands.append(command)
        for command in commands:
            with self.subTest(command=command.command_id):
                self.assert_failed(self.registry['test_execute'](command))

    def test_join_rechecks_branch_contract_and_rubric_identity(self):
        first = self.scoped_branch(1, 'value')
        second = self.scoped_branch(2, 'visual')
        for producer, alias in ((first[1], 'independent_test_snapshot'),
                                (first[2], 'independent_test_result')):
            path = self.root / producer.outputs['artifact_refs'][alias]['path']
            original = path.read_bytes()
            try:
                for field in ('contract_id', 'contract_schema_version', 'rubric_id', 'rubric_version'):
                    with self.subTest(alias=alias, field=field):
                        value = json.loads(original)
                        value[field] = 'different-binding'
                        atomic_json(path, value)
                        result = self.registry['test_execute'](self.join_command([first, second]))
                        self.assert_failed(result)
                        self.assertIn('contract or rubric identity mismatch', result.detail)
            finally:
                path.write_bytes(original)

    def test_execution_rejects_another_scopes_design(self):
        scope, designed, executed = self.scoped_branch(1, 'value')
        command = replace(self.execution_command(designed), payload={**self.payload,
            'regression_scope': {**scope, 'scope_id': 'scope-002'}})
        self.assert_failed(self.execute(designed, command=command))
        self.assertIsNone(self.last_command)

    def test_invalid_snapshot_schema_or_identity_fails_before_execution(self):
        for changes in ({'schema_version': True}, {'design_command_id': ''},
                        {'workspace': 'workspaces/tests/other'}, {'contract_id': 'other-contract'}):
            with self.subTest(changes=changes):
                designed = self.design()
                command = self.execution_command(designed)
                path = self.root / command.artifact_refs['independent_test_snapshot']['path']
                document = json.loads(path.read_text())
                atomic_json(path, {**document, **changes})
                self.assert_failed(self.execute(designed, command=command))
                self.assertIsNone(self.last_command)


    def modern_design(self, *, mutation=None, value=None, scope=None):
        command = self.command('test_design')
        command = replace(command, options={**command.options, 'workflow_version': 13},
                          payload={**command.payload, **({'regression_scope': scope} if scope else {})})
        result = self.design(command=command, mutation=mutation, value=value)
        self.assertEqual('completed', result.status, result.detail)
        return result

    def review(self, designed, *, change=None, observe=None, workflow_version=13):
        from modport.independent_tests import _review_binding
        command = self.command('test_review', refs={**self.refs, **designed.outputs['artifact_refs']},
                               workspace=designed.outputs['workspace'])
        command = replace(command, options={**command.options, 'workflow_version': workflow_version},
            payload={**self.payload, **({'regression_scope': designed.outputs['regression_scope']}
                                       if 'regression_scope' in designed.outputs else {})})
        snapshot = json.loads((self.root / command.artifact_refs['independent_test_snapshot']['path']).read_text())
        def fake(handler, operation):
            self.assertTrue(handler.read_only)
            if observe:
                observe(self.root / operation.options['workspace'])
            document = {'schema_version': 1, 'reviewer_id': 'independent-test-review-agent',
                        'verdict': 'approved', 'findings': [], **_review_binding(snapshot)}
            if change:
                change(document)
            message = self.root / f'logs/{command.command_id}.json'
            message.parent.mkdir(exist_ok=True)
            message.write_text(json.dumps(document))
            return _result(operation, 'completed', outputs={'last_message': message.relative_to(self.root).as_posix(),
                            'artifact_refs': {'agent_log:test_review': {'path': message.relative_to(self.root).as_posix(),
                                                                      'sha256': file_digest(message)}}})
        with patch('modport.handlers.CodexStageHandler.__call__', fake):
            return self.registry['test_review'](command)

    def reviewed_command(self, designed, reviewed, *, workflow_version=13):
        command = self.execution_command(designed)
        return replace(command, options={**command.options, 'workflow_version': workflow_version},
            payload={**self.payload, **({'regression_scope': designed.outputs['regression_scope']}
                                       if 'regression_scope' in designed.outputs else {})},
            artifact_refs={**command.artifact_refs, **reviewed.outputs['artifact_refs']},
            upstream_results={'test_review': reviewed.to_dict()})

    def test_v13_review_and_execution_use_pristine_source_and_captured_suite(self):
        product = 'src/main/java/example/build/Logic.java'
        original = self.product.read_text()
        designed = self.modern_design(mutation=lambda w: (w / product).write_text('author changed product'))
        def observe(work):
            self.assertEqual(original, (work / product).read_text())
            self.assertIn('assert new example.build.Logic()', (work / '.modport/independent-tests/Independent.java').read_text())
        reviewed = self.review(designed, observe=observe)
        self.assertEqual('completed', reviewed.status, reviewed.detail)
        author = self.root / designed.outputs['workspace']
        (author / '.modport/independent-tests/suite.json').write_text('{}')
        self.product.write_text('later candidate generation')
        executed = self.execute(designed, command=self.reviewed_command(designed, reviewed), mutation=observe)
        self.assertEqual('completed', executed.status, executed.detail)
        self.assertEqual(reviewed.command_id, executed.outputs['review_command_id'])

    def test_v23_partial_coverage_keeps_real_snapshot_review_execution_and_join_handoffs(self):
        scope = {'scope_id': 'scope-001', 'behavior_ids': ['value', 'visual'],
                 'runtime_behavior_ids': ['value', 'visual'], 'static_behavior_ids': [],
                 'gap_obligations': []}
        design_command = self.command('test_design')
        design_command = replace(design_command,
            options={**design_command.options, 'workflow_version': 23},
            payload={**design_command.payload, 'regression_scope': scope},
            task_id='test_design.g1.scope-001')

        designed = self.design(command=design_command)
        self.assertEqual('completed', designed.status, designed.detail)
        self.assertEqual('unverified', designed.outputs['acceptance_status'])
        self.assertIn('independent_test_snapshot', designed.outputs['artifact_refs'])
        self.assertIn('independent_test_suite', designed.outputs['artifact_refs'])
        self.assertEqual(['value'], designed.outputs['covered_behavior_ids'])
        self.assertEqual(['visual'], designed.outputs['uncovered_behavior_ids'])
        self.assertIn('visual', ' '.join(designed.outputs['business_diagnostics']))

        reviewed = self.review(designed, workflow_version=23)
        self.assertEqual('completed', reviewed.status, reviewed.detail)
        self.assertEqual('unverified', reviewed.outputs['acceptance_status'])
        reviewed = replace(reviewed, task_id='test_review.g1.scope-001')

        execute_command = self.reviewed_command(designed, reviewed, workflow_version=23)
        execute_command = replace(execute_command, task_id='test_execute.g1.scope-001')
        executed = self.execute(designed, command=execute_command)
        self.assertEqual('completed', executed.status, executed.detail)
        self.assertEqual('unverified', executed.outputs['acceptance_status'])
        self.assertIsNotNone(self.last_command)
        self.assertEqual(['value'], executed.outputs['covered_behavior_ids'])
        self.assertEqual(['visual'], executed.outputs['uncovered_behavior_ids'])

        branch = (scope, designed, executed)
        join_command = self.join_command([branch])
        join_command = replace(join_command,
            options={**join_command.options, 'workflow_version': 23},
            payload={**join_command.payload, 'regression_reviews': [reviewed.to_dict()]})
        joined = self.registry['test_execute'](join_command)
        self.assertEqual('completed', joined.status, joined.detail)
        self.assertEqual('unverified', joined.outputs['acceptance_status'])
        self.assertEqual(['value'], joined.outputs['covered_behavior_ids'])
        self.assertEqual(['visual'], joined.outputs['uncovered_behavior_ids'])
        self.assertTrue(any('visual' in item for item in joined.outputs['business_diagnostics']))

    def test_v23_contract_known_extra_runtime_id_is_reviewed_executed_and_reported(self):
        scope = {'scope_id': 'scope-001', 'behavior_ids': ['value'],
                 'runtime_behavior_ids': ['value'], 'static_behavior_ids': [],
                 'gap_obligations': []}
        value = suite()
        value['tests'][0]['behavior_ids'] = ['value', 'visual']
        design_command = self.command('test_design')
        design_command = replace(design_command,
            options={**design_command.options, 'workflow_version': 23},
            payload={**design_command.payload, 'regression_scope': scope},
            task_id='test_design.g1.scope-001')

        designed = self.design(command=design_command, value=value)
        self.assertEqual('completed', designed.status, designed.detail)
        self.assertEqual('unverified', designed.outputs['acceptance_status'])
        self.assertIn('independent_test_snapshot', designed.outputs['artifact_refs'])
        self.assertEqual(['value', 'visual'], designed.outputs['covered_behavior_ids'])
        self.assertEqual([], designed.outputs['uncovered_behavior_ids'])
        self.assertTrue(any('unexpected behavior IDs: visual' in item
                            for item in designed.outputs['business_diagnostics']))

        reviewed = self.review(designed, workflow_version=23)
        self.assertEqual('completed', reviewed.status, reviewed.detail)
        reviewed = replace(reviewed, task_id='test_review.g1.scope-001')
        execute_command = self.reviewed_command(designed, reviewed, workflow_version=23)
        execute_command = replace(execute_command, task_id='test_execute.g1.scope-001')
        executed = self.execute(designed, command=execute_command)
        self.assertEqual('completed', executed.status, executed.detail)
        self.assertIsNotNone(self.last_command)
        self.assertEqual(['value', 'visual'], executed.outputs['covered_behavior_ids'])

        join_command = self.join_command([(scope, designed, executed)])
        join_command = replace(join_command,
            options={**join_command.options, 'workflow_version': 23},
            payload={**join_command.payload, 'regression_reviews': [reviewed.to_dict()]})
        joined = self.registry['test_execute'](join_command)
        self.assertEqual('completed', joined.status, joined.detail)
        self.assertEqual('unverified', joined.outputs['acceptance_status'])
        self.assertEqual(['value', 'visual'], joined.outputs['covered_behavior_ids'])
        self.assertEqual([], joined.outputs['uncovered_behavior_ids'])
        self.assertTrue(any('outside the assigned scope: visual' in item
                            for item in joined.outputs['business_diagnostics']))

    def test_v23_unknown_and_global_static_behavior_ids_are_not_snapshotted_as_runtime(self):
        scope = {'scope_id': 'scope-001', 'behavior_ids': ['value'],
                 'runtime_behavior_ids': ['value'], 'static_behavior_ids': [],
                 'gap_obligations': []}
        cases = [('unknown', None, ['value', 'unknown.behavior']),
                 ('static', 'visual', ['value', 'visual'])]
        for label, static_id, mapped_ids in cases:
            with self.subTest(label=label):
                value = suite()
                value['tests'][0]['behavior_ids'] = mapped_ids
                if static_id:
                    lock_path = self.root / self.refs['functional_contract_lock']['path']
                    lock = json.loads(lock_path.read_text())
                    for rows in (lock['contract']['entries'], lock['contract']['behaviors']):
                        row = next(row for row in rows if row['entry_id'] == static_id)
                        row['test_mapping'] = [static_id + '-test']
                        row['executable_tests'] = [static_id + '-test']
                    lock['test_evidence'] = {static_id + '-test': {
                        'evidence_kind': 'static_client', 'static_reason': 'Human client observation required',
                        'acceptance_gates': ['client_smoke']}}
                    atomic_json(lock_path, lock)
                design_command = self.command('test_design')
                design_command = replace(design_command,
                    options={**design_command.options, 'workflow_version': 23},
                    payload={**design_command.payload, 'regression_scope': scope})
                designed = self.design(command=design_command, value=value)
                self.assertEqual('completed', designed.status, designed.detail)
                self.assertEqual('unverified', designed.outputs['acceptance_status'])
                self.assertNotIn('independent_test_snapshot',
                                 designed.outputs.get('artifact_refs', {}))
                self.assertTrue(designed.outputs['business_diagnostics'])
                self.assertIn('unknown or non-runtime frozen behavior IDs',
                              ' '.join(designed.outputs['business_diagnostics']))

    def test_v23_unavailable_declared_source_reaches_consumer_as_specific_diagnostic(self):
        scope = {'scope_id': 'scope-001', 'behavior_ids': ['value'],
                 'runtime_behavior_ids': ['value'], 'static_behavior_ids': [],
                 'gap_obligations': []}
        value = suite()
        value['tests'][0]['sources'] = ['Missing.java']
        design_command = self.command('test_design')
        design_command = replace(design_command,
            options={**design_command.options, 'workflow_version': 23},
            payload={**design_command.payload, 'regression_scope': scope},
            task_id='test_design.g1.scope-001')
        designed = self.design(command=design_command, value=value)
        self.assertEqual('completed', designed.status, designed.detail)
        self.assertNotIn('independent_test_snapshot', designed.outputs.get('artifact_refs', {}))

        command = self.command('test_execute', refs=self.refs,
                               workspace=design_command.options['workspace'])
        command = replace(command, options={**command.options, 'workflow_version': 23},
            payload={**command.payload, 'regression_scope': scope},
            task_id='test_execute.g1.scope-001',
            upstream_results={'test_design': designed.to_dict()})
        with patch('modport.handlers._exec') as execute:
            result = self.registry['test_execute'](command)
        self.assertEqual('failed', result.status)
        self.assertEqual('unverified', result.outputs['acceptance_status'])
        self.assertIsNone(execute.call_args)
        diagnostics = ' '.join(result.outputs['business_diagnostics'])
        self.assertIn('Missing.java', diagnostics)
        self.assertIn('independent_test_snapshot', diagnostics)
        self.assertEqual('completed', result.outputs['observed_test_input']['upstream'][0]['status'])

    def test_v13_review_missing_rejected_forged_or_wrong_design_never_executes(self):
        designed = self.modern_design()
        command = self.execution_command(designed)
        command = replace(command, options={**command.options, 'workflow_version': 13})
        self.assert_failed(self.execute(designed, command=command))
        self.assertIsNone(self.last_command)
        reviewed = self.review(designed)
        command = replace(self.reviewed_command(designed, reviewed), upstream_results={})
        self.assert_failed(self.execute(designed, command=command))
        self.assertIsNone(self.last_command)
        rejected = self.review(designed, change=lambda d: d.update(verdict='rejected', findings=[
            {'reason': 'Assertion cannot detect bad return value', 'location': 'Independent.java',
             'closure_criteria': 'Compare real result against frozen baseline'}]))
        self.assertEqual('failed', rejected.status)
        self.assertIn('independent_test_review', rejected.outputs['artifact_refs'])
        self.assert_failed(self.execute(designed, command=self.reviewed_command(designed, rejected)))
        other = self.modern_design()
        self.assert_failed(self.execute(other, command=self.reviewed_command(other, reviewed)))

    def test_v13_changed_suite_artifact_requires_new_design_and_review(self):
        designed = self.modern_design()
        reviewed = self.review(designed)
        ref = designed.outputs['artifact_refs']['independent_test_source:Independent.java']
        (self.root / ref['path']).write_text('class Fake {}')
        self.assert_failed(self.execute(designed, command=self.reviewed_command(designed, reviewed)))
        self.assertIsNone(self.last_command)

    def test_v13_static_only_review_retains_client_gate_without_executing_project(self):
        scope = {'scope_id': 'scope-001', 'behavior_ids': ['visual'], 'runtime_behavior_ids': [],
                 'static_behavior_ids': ['visual'], 'gap_obligations': []}
        value = {'schema_version': 1, 'tests': [], 'static_behavior_ids': ['visual'],
                 'static_evidence': [{'behavior_id': 'visual', 'reason': 'Human visual observation required',
                     'evidence': ['Frozen visual contract requires display visibility'],
                     'acceptance_gates': ['client_smoke']}]}
        lock_path = self.root / self.refs['functional_contract_lock']['path']
        lock = json.loads(lock_path.read_text())
        visual = next(row for row in lock['contract']['entries'] if row['entry_id'] == 'visual')
        for rows in (lock['contract']['entries'], lock['contract']['behaviors']):
            visual = next(row for row in rows if row['entry_id'] == 'visual')
            visual['test_mapping'] = ['visual-test']
            visual['executable_tests'] = ['visual-test']
        lock['test_evidence'] = {'visual-test': {'evidence_kind': 'static_client',
            'static_reason': 'Human observation required', 'acceptance_gates': ['client_smoke']}}
        atomic_json(lock_path, lock)
        designed = self.modern_design(value=value, scope=scope)
        reviewed = self.review(designed)
        self.assertEqual('completed', reviewed.status, reviewed.detail)
        executed = self.execute(designed, command=self.reviewed_command(designed, reviewed))
        self.assertEqual('completed', executed.status, executed.detail)
        self.assertIsNone(self.last_command)
        self.assertEqual(['visual'], executed.outputs['static_behavior_ids'])
        self.assertEqual([], executed.outputs['runtime_behavior_ids'])
        self.assertEqual(['client_smoke'], executed.outputs['required_acceptance_gates'])


    def test_v13_join_requires_each_scopes_matching_independent_review(self):
        branches, reviews = [], []
        for index, behavior in enumerate(('value', 'visual'), 1):
            scope = {'scope_id': f'scope-{index:03d}', 'behavior_ids': [behavior],
                     'runtime_behavior_ids': [behavior], 'static_behavior_ids': [], 'gap_obligations': []}
            value = suite()
            value['tests'][0]['behavior_ids'] = [behavior]
            designed = self.modern_design(value=value, scope=scope)
            designed = replace(designed, task_id=f'test_design.g1.{scope["scope_id"]}')
            reviewed = self.review(designed)
            self.assertEqual('completed', reviewed.status, reviewed.detail)
            reviewed = replace(reviewed, task_id=f'test_review.g1.{scope["scope_id"]}')
            command = replace(self.reviewed_command(designed, reviewed),
                              task_id=f'test_execute.g1.{scope["scope_id"]}')
            executed = self.execute(designed, command=command)
            self.assertEqual('completed', executed.status, executed.detail)
            branches.append((scope, designed, executed))
            reviews.append(reviewed.to_dict())
        command = self.join_command(branches)
        command = replace(command, options={**command.options, 'workflow_version': 13},
                          payload={**command.payload, 'regression_reviews': reviews})
        joined = self.registry['test_execute'](command)
        self.assertEqual('completed', joined.status, joined.detail)
        self.assertEqual(['value', 'visual'], joined.outputs['runtime_behavior_ids'])
        self.assertEqual([], joined.outputs['static_behavior_ids'])
        bad = self.join_command(branches)
        bad = replace(bad, options={**bad.options, 'workflow_version': 13},
                      payload={**bad.payload, 'regression_reviews': list(reversed(reviews))})
        self.assert_failed(self.registry['test_execute'](bad))
        missing = self.join_command(branches)
        missing = replace(missing, options={**missing.options, 'workflow_version': 13})
        self.assert_failed(self.registry['test_execute'](missing))


    def test_v13_multiproject_task_executes_with_reports_redirected_to_root_build(self):
        value = suite()
        value['tests'][0]['task'] = ':module:independentTest'
        value['tests'][0]['report_paths'] = ['build/test-results/module/TEST-Independent.xml']
        designed = self.modern_design(value=value)
        reviewed = self.review(designed)
        self.assertEqual('completed', reviewed.status, reviewed.detail)
        executed = self.execute(designed, command=self.reviewed_command(designed, reviewed))
        self.assertEqual('completed', executed.status, executed.detail)
        self.assertEqual([':module:independentTest'], executed.outputs['tasks'])
        self.assertEqual(['build/test-results/module/TEST-Independent.xml'], list(executed.outputs['reports']))
        self.assertIn(':module:independentTest', self.last_command)
        binds = [self.last_command[index:index + 3] for index in range(len(self.last_command))]
        self.assertTrue(any(item[0] == '--bind' and item[-1] == '/workspace/build' for item in binds))
        self.assertFalse(any(item[0] == '--bind' and item[-1] == '/workspace/module/build' for item in binds))

    def test_v13_multiproject_report_outside_root_build_is_rejected_before_review_or_execution(self):
        value = suite()
        value['tests'][0]['task'] = ':module:independentTest'
        value['tests'][0]['report_paths'] = ['module/build/test-results/independent/TEST-Independent.xml']
        command = self.command('test_design')
        command = replace(command, options={**command.options, 'workflow_version': 13})
        with patch('modport.handlers._exec') as project_execution:
            designed = self.design(command=command, value=value)
        self.assert_failed(designed)
        self.assertIn('explicit XML files under root build/', designed.detail)
        project_execution.assert_not_called()
        self.assertNotIn('independent_test_snapshot', designed.outputs.get('artifact_refs', {}))


if __name__ == '__main__':
    unittest.main()
