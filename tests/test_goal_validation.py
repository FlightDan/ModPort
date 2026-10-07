import json
from dataclasses import replace
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from types import SimpleNamespace

from modport.contracts import OperationInput
from modport.goal_validation import validate_goal_candidate
from test_goal_planning import sample


class GoalValidationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.task, self.context, self.goal = sample()
        self.command = OperationInput('run', 'task', 'coder', 'cmd', str(self.root),
            payload={'development_task': self.task, 'planning_context': self.context})
        (self.root / 'metadata').mkdir()
        (self.root / 'metadata/output.json').write_text('{}')
        self.report = self.root / self.goal['acceptance_report']
        self.report.parent.mkdir(parents=True)
        self.write_report()

    def write_report(self, state='passed', refs=None):
        self.report.write_text(json.dumps({'acceptance': [{'criterion': self.task['acceptance'][0],
            'state': state, 'evidence': refs if refs is not None else ['metadata-json']}]}))

    def validate(self):
        return validate_goal_candidate(self.command, self.root, self.goal)

    def test_failed_check_then_corrected_candidate_passes(self):
        (self.root / 'metadata/output.json').write_text('broken')
        self.assertFalse(self.validate()['accepted'])
        (self.root / 'metadata/output.json').write_text('{}')
        result = self.validate()
        self.assertTrue(result['accepted'], result)
        self.assertEqual(result['evidence']['scope'], 'task-check acceptance')

    def test_report_alone_or_pending_or_fake_evidence_never_passes(self):
        for state, refs in [('pending', ['metadata-json']), ('passed', ['invented']), ('passed', [])]:
            self.write_report(state, refs)
            self.assertFalse(self.validate()['accepted'])
        self.report.write_text('{"accepted":true}')
        self.assertFalse(self.validate()['accepted'])

    def test_symlink_check_and_report_are_rejected(self):
        target = self.root / 'metadata/output.json'
        target.unlink()
        target.symlink_to(self.report)
        self.assertFalse(self.validate()['accepted'])
        target.unlink()
        target.write_text('{}')
        self.report.unlink()
        self.report.symlink_to(target)
        self.assertFalse(self.validate()['accepted'])

    def test_python_is_compiled_without_execution(self):
        target = self.root / 'metadata/code.py'
        target.write_text("raise RuntimeError('must not execute')\n")
        self.goal['checks'][0].update(type='python_syntax', path='metadata/code.py')
        self.assertTrue(self.validate()['accepted'])

    def test_gradle_only_executes_sandbox_command_and_honors_failure(self):
        (self.root / 'gradlew').write_text('#!/bin/sh\n')
        self.goal['checks'][0] = {'id': 'metadata-json', 'type': 'gradle_tasks',
                                 'tasks': ['test'], 'acceptance': self.task['acceptance']}
        with patch('modport.handlers._sandboxed_build_command', return_value=['sandbox', 'test']) as sandbox, \
             patch('modport.handlers._locked_java_home', return_value=Path('/jdk')), \
             patch('modport.telemetry.probe_process', return_value=SimpleNamespace(returncode=1, stdout='', stderr='')) as run:
            self.assertFalse(self.validate()['accepted'])
            self.assertEqual(run.call_args.args[0], ['sandbox', 'test'])
            self.assertEqual(run.call_args.kwargs['env'], {})
            self.assertEqual(sandbox.call_args.args[2],
                             ['bash', '/workspace/gradlew', '--no-daemon', '--rerun-tasks', '--no-build-cache', 'test'])
            self.assertEqual(sandbox.call_args.kwargs['cache_name'], 'target-contract-gradle-cache')
            self.assertEqual(sandbox.call_args.kwargs['java_home'], Path('/jdk'))
            run.return_value.returncode = 0
            self.assertTrue(self.validate()['accepted'])

    def test_gradle_timeout_is_capped_by_remaining_run_budget(self):
        self.command = replace(self.command, options={'deadline_epoch': 105})
        (self.root / 'gradlew').write_text('#!/bin/sh\n')
        self.goal['checks'][0] = {'id': 'metadata-json', 'type': 'gradle_tasks',
                                 'tasks': ['test'], 'acceptance': self.task['acceptance']}
        with patch('modport.handlers.time.time', return_value=100), \
             patch('modport.handlers._sandboxed_build_command', return_value=['sandbox']), \
             patch('modport.handlers._locked_java_home', return_value=Path('/jdk')), \
             patch('modport.telemetry.probe_process', return_value=SimpleNamespace(returncode=0, stdout='', stderr='')) as run:
            self.assertTrue(self.validate()['accepted'])
            self.assertEqual(run.call_args.kwargs['timeout'], 5)

    def test_exhausted_budget_prevents_following_checks(self):
        self.goal['checks'].append({**self.goal['checks'][0], 'id': 'second'})
        with patch('modport.handlers._remaining_timeout', side_effect=[1, 1, TimeoutError('expired')]), \
             patch('modport.goal_validation._check', return_value={'type': 'json_valid'}) as check:
            with self.assertRaises(TimeoutError):
                self.validate()
            self.assertEqual(check.call_count, 1)

    def test_gradle_checks_use_fresh_snapshots_and_preserve_candidate_and_evidence(self):
        (self.root / 'gradlew').write_text('#!/bin/sh\n')
        (self.root / 'gradlew').chmod(0o755)
        (self.root / '.git').mkdir()
        (self.root / '.git/config').write_text('host repository metadata')
        self.goal['checks'] = [{'id': identifier, 'type': 'gradle_tasks', 'tasks': ['test'],
                               'acceptance': self.task['acceptance']} for identifier in ['metadata-json', 'second']]
        snapshots = []
        def execute(args, **kwargs):
            snapshot = kwargs['cwd']
            snapshots.append(snapshot)
            self.assertNotEqual(snapshot, self.root)
            self.assertFalse((snapshot / '.git').exists())
            self.assertEqual((snapshot / 'gradlew').stat().st_mode & 0o777, 0o755)
            self.assertEqual((snapshot / 'metadata/output.json').read_text(), '{}')
            (snapshot / '.gradle').mkdir()
            (snapshot / '.gradle/cache').write_text('host-generated build output')
            (snapshot / 'metadata/output.json').write_text('build mutation')
            return SimpleNamespace(returncode=0, stdout='actual test result', stderr='')
        with patch('modport.handlers._sandboxed_build_command', return_value=['sandbox']) as sandbox, \
             patch('modport.handlers._locked_java_home', return_value=Path('/jdk')), \
             patch('modport.telemetry.probe_process', side_effect=execute):
            result = self.validate()
        self.assertTrue(result['accepted'], result)
        self.assertEqual(len(set(snapshots)), 2)
        self.assertEqual([call.args[1] for call in sandbox.call_args_list], snapshots)
        self.assertFalse((self.root / '.gradle').exists())
        self.assertEqual((self.root / 'metadata/output.json').read_text(), '{}')
        record = result['evidence']['checks']['metadata-json']
        self.assertEqual((self.root / record['stdout']['path']).read_text(), 'actual test result')
        manifest = json.loads((self.root / record['candidate_manifest']['path']).read_text())
        self.assertIn('metadata/output.json', manifest)
        self.assertFalse(any('.git/' in path for path in manifest))

    def test_gradle_snapshot_rejects_symlinks_before_execution(self):
        (self.root / 'gradlew').write_text('#!/bin/sh\n')
        (self.root / 'outside-link').symlink_to('/etc/passwd')
        self.goal['checks'][0] = {'id': 'metadata-json', 'type': 'gradle_tasks',
                                 'tasks': ['test'], 'acceptance': self.task['acceptance']}
        with patch('modport.telemetry.probe_process') as run:
            result = self.validate()
        self.assertFalse(result['accepted'])
        self.assertIn('symlinks', ' '.join(result['failures']))
        run.assert_not_called()

    def test_characterization_init_and_scope_specific_java_and_cache(self):
        (self.root / 'gradlew').write_text('#!/bin/sh\n')
        (self.root / 'gradlew').chmod(0o644)
        (self.root / '.modport/characterization.init.gradle').write_text('// task declarations')
        self.goal['checks'][0] = {'id': 'metadata-json', 'type': 'gradle_tasks',
                                 'tasks': ['characterizationTest'], 'acceptance': self.task['acceptance']}
        for scope in ('contract', 'target'):
            self.command = replace(self.command, payload={**self.command.payload, 'goal_scope': scope})
            with self.subTest(scope=scope), \
                 patch('modport.handlers._forge_baseline_init', return_value='/gradle-cache/forge.init.gradle') as forge, \
                 patch('modport.handlers._locked_java_home', return_value=Path('/target-jdk')) as java, \
                 patch('modport.handlers._sandboxed_build_command', return_value=['sandbox']) as sandbox, \
                 patch('modport.telemetry.probe_process', return_value=SimpleNamespace(returncode=0, stdout='', stderr='')):
                self.assertTrue(self.validate()['accepted'])
                args = sandbox.call_args.args[2]
                self.assertEqual(args[:5], ['bash', '/workspace/gradlew', '--no-daemon', '--rerun-tasks', '--no-build-cache'])
                self.assertEqual(args[-3:], ['--init-script', '/workspace/.modport/characterization.init.gradle', 'characterizationTest'])
                if scope == 'contract':
                    self.assertEqual(sandbox.call_args.kwargs['cache_name'], 'baseline-contract-gradle-cache')
                    self.assertIsNone(sandbox.call_args.kwargs['java_home'])
                    forge.assert_called_once_with(self.root, cache_name='baseline-contract-gradle-cache')
                    java.assert_not_called()
                    self.assertIn('/gradle-cache/forge.init.gradle', args)
                else:
                    self.assertEqual(sandbox.call_args.kwargs['cache_name'], 'target-contract-gradle-cache')
                    self.assertEqual(sandbox.call_args.kwargs['java_home'], Path('/target-jdk'))
                    java.assert_called_once_with(self.root)
                    forge.assert_not_called()

    def test_v21_goal_snapshot_adds_conventional_sources_without_changing_candidate(self):
        (self.root / 'gradlew').write_text('#!/bin/sh\n')
        authored = self.root / '.modport/characterization.init.gradle'
        authored.write_text('// authored launcher')
        harness = self.root / '.modport/harness'
        harness.mkdir()
        (harness / 'Smoke.java').write_text('class Smoke {}')
        self.command = replace(self.command, options={**self.command.options, 'workflow_version': 21})
        self.goal['checks'][0] = {'id': 'metadata-json', 'type': 'gradle_tasks',
                                 'tasks': ['characterizationTest'], 'acceptance': self.task['acceptance']}
        with patch('modport.handlers._locked_java_home', return_value=Path('/target-jdk')), \
             patch('modport.handlers._sandboxed_build_command', return_value=['sandbox']) as sandbox, \
             patch('modport.telemetry.probe_process', return_value=SimpleNamespace(returncode=0, stdout='', stderr='')):
            result = self.validate()
            self.assertTrue(result['accepted'])
        args = sandbox.call_args.args[2]
        self.assertIn('/workspace/.modport/characterization.init.gradle', args)
        self.assertIn('/modport-wiring/characterization-sources.init.gradle', args)
        wiring = result['evidence']['checks']['metadata-json']['harness_wiring']
        self.assertEqual(2, len(wiring))
        self.assertTrue(all(item['sha256'] and (self.root / item['path']).is_file() for item in wiring))
        self.assertEqual('// authored launcher', authored.read_text())
        self.assertFalse((authored.parent / 'characterization-sources.init.gradle').exists())

    def test_run_client_check_uses_host_client_launcher(self):
        (self.root / 'gradlew').write_text('#!/bin/sh\n')
        self.goal['checks'][0] = {'id': 'metadata-json', 'type': 'gradle_tasks',
                                 'tasks': [':mod:runClient'], 'acceptance': self.task['acceptance']}
        with patch('modport.handlers._client_launch_arguments', return_value=['client-launcher']) as client, \
             patch('modport.handlers._locked_java_home', return_value=Path('/jdk')), \
             patch('modport.handlers._sandboxed_build_command', return_value=['sandbox']) as sandbox, \
             patch('modport.telemetry.probe_process', return_value=SimpleNamespace(returncode=0, stdout='', stderr='')):
            self.assertTrue(self.validate()['accepted'])
        self.assertEqual(sandbox.call_args.args[2], ['client-launcher'])
        self.assertEqual(client.call_args.args[2][-1], ':mod:runClient')
        self.assertLessEqual(client.call_args.kwargs['timeout'], 595)

    def test_declared_client_check_gets_fresh_nonce_and_executor_provenance(self):
        (self.root / 'gradlew').write_text('#!/bin/sh\n')
        (self.root / '.modport/functional-contract.json').write_text(json.dumps({'test_evidence': {'client': {}}}))
        stale = self.root / '.modport/evidence/client.json'
        stale.parent.mkdir()
        stale.write_text('old runtime evidence')
        self.goal['checks'] = [{'id': identifier, 'type': 'gradle_tasks',
                               'tasks': ['characterizationClient'], 'acceptance': self.task['acceptance']}
                              for identifier in ('metadata-json', 'second')]
        declaration = {'client': {'path': '.modport/evidence/client.json',
                                  'evidence_kind': 'runtime', 'executor': 'client_smoke'}}
        provenance = {'client': {'executor_fingerprint': 'test-source-fingerprint'}}
        def execute(args, **kwargs):
            self.assertFalse((kwargs['cwd'] / '.modport/evidence/client.json').exists())
            return SimpleNamespace(returncode=0, stdout='', stderr='')
        with patch('modport.handlers._acceptance_rubric_for', return_value={}), \
             patch('modport.handlers._test_evidence_declarations', return_value=declaration) as declarations, \
             patch('modport.handlers._runtime_executor_provenance', return_value=provenance) as fingerprint, \
             patch('modport.handlers._client_launch_arguments', return_value=['client-launcher']) as client, \
             patch('modport.handlers._locked_java_home', return_value=Path('/jdk')), \
             patch('modport.handlers._sandboxed_build_command', return_value=['sandbox']) as sandbox, \
             patch('modport.telemetry.probe_process', side_effect=execute):
            result = self.validate()
        self.assertTrue(result['accepted'], result)
        self.assertEqual(client.call_count, 2)
        self.assertEqual(declarations.call_count, 2)
        self.assertEqual(fingerprint.call_count, 2)
        environments = [call.kwargs['environment'] for call in sandbox.call_args_list]
        self.assertEqual(len({env['MODPORT_EVIDENCE_NONCE'] for env in environments}), 2)
        for env in environments:
            self.assertEqual(env['MODPORT_EXECUTION_ID'], self.command.command_id)
            self.assertEqual(json.loads(env['MODPORT_EXECUTOR_FINGERPRINTS']), {'client': 'test-source-fingerprint'})
        self.assertEqual(stale.read_text(), 'old runtime evidence')

    def regression(self):
        (self.root / 'gradlew').write_text('#!/bin/sh\n')
        self.goal['checks'][0] = {'id': 'metadata-json', 'type': 'gradle_regression',
            'tasks': [':module:test'], 'reports': ['module/build/test-results/test/TEST-Suite.xml'],
            'acceptance': self.task['acceptance']}

    def run_regression(self, xml=None, stdout='> Task :module:test', returncode=0, execution=None):
        def execute(args, **kwargs):
            path = kwargs['cwd'] / self.goal['checks'][0]['reports'][0]
            self.assertFalse(path.exists(), 'stale report must be removed before execution')
            if xml is not None:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(xml)
            listener = next((kwargs['cwd'] / 'build/.modport-regression').glob('*.init.gradle'))
            nonce = listener.name[:-len('.init.gradle')]
            observed = execution if execution is not None else {
                ':module:test': {'executed': True, 'tests': 1, 'failures': 0, 'skipped': 0,
                                 'report_directory': '/workspace/module/build/test-results/test'}}
            output = kwargs['cwd'] / 'build/.modport-regression' / (nonce + '.json')
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(json.dumps({'nonce': nonce, 'tasks': observed}))
            return SimpleNamespace(returncode=returncode, stdout=stdout, stderr='')
        with patch('modport.handlers._sandboxed_build_command', return_value=['sandbox']), \
             patch('modport.handlers._locked_java_home', return_value=Path('/jdk')), \
             patch('modport.telemetry.probe_process', side_effect=execute):
            return self.validate()

    def test_regression_requires_fresh_actual_passing_cases(self):
        self.regression()
        passing = '<testsuite tests="1" failures="0" errors="0"><testcase name="behavior"/></testsuite>'
        stale = self.root / self.goal['checks'][0]['reports'][0]
        stale.parent.mkdir(parents=True)
        stale.write_text(passing)
        self.assertFalse(self.run_regression()['accepted'])
        result = self.run_regression(passing)
        self.assertTrue(result['accepted'], result)
        proof = result['evidence']['checks']['metadata-json']['reports'][0]
        self.assertEqual(proof['tests'], 1)
        self.assertEqual(stale.read_text(), passing)
        self.assertIn('sha256', proof)

    def test_regression_rejects_empty_failed_skipped_malformed_and_forged_totals(self):
        self.regression()
        reports = [
            '<testsuite tests="0" failures="0" errors="0"/>',
            '<testsuite tests="1" failures="0" errors="0"/>',
            '<testsuite tests="1" failures="1" errors="0"><testcase><failure/></testcase></testsuite>',
            '<testsuite tests="1" failures="0" errors="1"><testcase><error/></testcase></testsuite>',
            '<testsuite tests="1" failures="0" errors="0" skipped="1"><testcase><skipped/></testcase></testsuite>',
            '<testsuite tests="1" failures="0" errors="0"><testcase><failure/></testcase></testsuite>',
            '<testsuite><testcase/></testsuite>', '<broken',
            '<!DOCTYPE x [<!ENTITY test "ok">]><testsuite/>',
        ]
        for report in reports:
            with self.subTest(report=report):
                self.assertFalse(self.run_regression(report)['accepted'])

    def test_regression_rejects_task_skip_even_when_report_exists(self):
        self.regression()
        passing = '<testsuite tests="1" failures="0" errors="0"><testcase name="behavior"/></testsuite>'
        for outcome in ('NO-SOURCE', 'SKIPPED', 'UP-TO-DATE', 'FROM-CACHE'):
            with self.subTest(outcome=outcome):
                result = self.run_regression(passing, stdout='> Task :module:test ' + outcome)
                self.assertFalse(result['accepted'])
                self.assertIn(outcome, ' '.join(result['failures']))

    def test_quiet_gradle_can_pass_with_listener_and_fresh_junit_evidence(self):
        self.regression()
        passing = '<testsuite tests="1" failures="0" errors="0"><testcase name="behavior"/></testsuite>'
        result = self.run_regression(passing, stdout='')
        self.assertTrue(result['accepted'], result)

    def test_regression_requires_every_requested_task_execution_record(self):
        self.regression()
        passing = '<testsuite tests="1" failures="0" errors="0"><testcase name="behavior"/></testsuite>'
        self.goal['checks'][0]['tasks'].append(':other:test')
        result = self.run_regression(passing)
        self.assertFalse(result['accepted'])
        self.assertIn('regression task :other:test needs actual passing Gradle Test execution',
                      ' '.join(result['failures']))
        self.goal['checks'][0]['tasks'] = [':test']
        self.assertFalse(self.run_regression(passing)['accepted'])
        self.goal['checks'][0]['tasks'] = [':module:test']
        self.assertTrue(self.run_regression(passing)['accepted'])

    def test_regression_rejects_aggregate_or_missing_direct_test_execution(self):
        self.regression()
        passing = '<testsuite tests="1" failures="0" errors="0"><testcase name="behavior"/></testsuite>'
        self.goal['checks'][0]['tasks'] = [':module:check']
        result = self.run_regression(passing, stdout='> Task :module:test NO-SOURCE\n> Task :module:check')
        self.assertFalse(result['accepted'])
        self.assertIn('actual passing Gradle Test execution', ' '.join(result['failures']))
        self.goal['checks'][0]['tasks'] = [':module:test']
        for observed in ({}, {':module:test': {'executed': False, 'tests': 0}},
                         {':module:test': {'executed': True, 'tests': 1, 'failures': 0, 'skipped': 1}}):
            with self.subTest(observed=observed):
                self.assertFalse(self.run_regression(passing, execution=observed)['accepted'])

    def test_regression_reports_must_map_to_every_executed_test_task(self):
        self.regression()
        passing = '<testsuite tests="1" failures="0" errors="0"><testcase name="behavior"/></testsuite>'
        valid = {'executed': True, 'tests': 1, 'failures': 0, 'skipped': 0,
                 'report_directory': '/workspace/module/build/test-results/test'}
        for directory in ('/workspace/other/build/test-results/test', '/outside/test', '/workspace/../test'):
            with self.subTest(directory=directory):
                result = self.run_regression(passing, execution={':module:test': {**valid, 'report_directory': directory}})
                self.assertFalse(result['accepted'])
        self.goal['checks'][0]['tasks'].append(':other:test')
        result = self.run_regression(passing, stdout='> Task :module:test\n> Task :other:test',
                                    execution={':module:test': valid, ':other:test': {**valid,
                                               'report_directory': '/workspace/other/build/test-results/test'}})
        self.assertFalse(result['accepted'])
        self.assertIn(':other:test has no declared report', ' '.join(result['failures']))

    def test_workflow_12_requires_complete_self_check_and_reviewed_structural_task(self):
        self.task.update(validation_kind='structural', structural_reason='Only JSON serialization syntax is changed',
                         validation_checks=self.goal['checks'])
        self.command = replace(self.command, payload={**self.command.payload, 'development_task': self.task},
                               options={'workflow_version': 12})
        self.assertFalse(self.validate()['accepted'])
        report = json.loads(self.report.read_text())
        review = {'state': 'passed', 'reviewed_paths': ['metadata/output.json'],
                  'checks': ['metadata-json'], 'summary': 'Checked JSON syntax and task ownership.'}
        report['self_check'] = review
        self.report.write_text(json.dumps(report))
        self.assertTrue(self.validate()['accepted'])
        for key, value in [('state', 'pending'), ('summary', ''), ('reviewed_paths', ['other/file']),
                           ('checks', []), ('checks', ['metadata-json', 'fake'])]:
            self.report.write_text(json.dumps({**report, 'self_check': {**review, key: value}}))
            with self.subTest(key=key, value=value):
                self.assertFalse(self.validate()['accepted'])
