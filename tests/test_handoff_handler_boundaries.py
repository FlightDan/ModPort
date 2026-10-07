"""Host failure/recovery boundaries with every model/project process mocked."""
from copy import deepcopy
from dataclasses import replace
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from modport import handlers
from modport.contracts import OperationResult
from modport.evidence import atomic_json, verified_path
from modport.operations import MigrationOperations
from modport.repair_evidence import snapshot_repair_evidence
import test_handlers as handler_fixtures


class HandoffHandlerBoundaryTests(unittest.TestCase):
    def test_reopen_ignores_other_repair_cycles_and_preserves_current_feedback(self):
        context = {'repair_scope': 'contract', 'repair_generation': 3,
                   'failure_execution_id': 'current-build-failure',
                   'current_failure': {'detail': 'current source failure'}, 'artifact_refs': {}}
        current_feedback = {'stage': 'contract_diagnose', 'execution_id': 'current-format',
                            'result': {'detail': 'current correction'}}
        for mismatch in ({'repair_generation': 2}, {'failure_execution_id': 'older-failure'}):
            for existing in (None, current_feedback):
                with self.subTest(mismatch=mismatch, existing=existing):
                    old_context = {**context, **mismatch}
                    old_result = {'stage_id': 'contract_diagnose', 'command_id': 'old-format',
                                  'status': 'failed', 'error_code': 'planning_output_invalid'}
                    attempt = {'command': {'execution_id': 'old-format', 'payload': {
                        'payload': {'repair_context': old_context,
                                    'repair_generation': old_context['repair_generation']}}},
                        'result': {'value': old_result}}
                    app = MigrationOperations._new_application()
                    app.update(repair_context=deepcopy(context), format_context=deepcopy(existing))
                    app['effective']['contract_diagnose'] = {'status': 'failed'}
                    state = {'application_state': app,
                             'tasks': {'contract_diagnose': {'attempts': [attempt]}}}
                    reopened = MigrationOperations._reopen_application(Path('unused'), state,
                        stage='contract_diagnose', deadline_epoch=1000, max_agent_assignments=10)
                    self.assertEqual(reopened['format_context'], existing)
                    self.assertEqual(reopened['repair_context'], context)
                    self.assertEqual(reopened['rework_context'], context['current_failure'])
                    app['format_context'] = {'stage': 'contract_diagnose', 'execution_id': 'old-format',
                                             'result': old_result}
                    reopened = MigrationOperations._reopen_application(Path('unused'), state,
                        stage='contract_diagnose', deadline_epoch=1000, max_agent_assignments=10)
                    self.assertIsNone(reopened['format_context'])

    def test_reopen_success_supersedes_older_same_cycle_format_error(self):
        context = {'repair_scope': 'contract', 'repair_generation': 3,
                   'failure_execution_id': 'build-failure',
                   'current_failure': {'detail': 'build error'}, 'artifact_refs': {}}
        def attempt(identifier, status, error=None):
            return {'command': {'execution_id': identifier, 'payload': {'payload': {
                'repair_context': context, 'repair_generation': 3}}}, 'result': {'value': {
                'stage_id': 'contract_diagnose', 'command_id': identifier,
                'status': status, 'error_code': error}}}
        old = attempt('bad-format', 'failed', 'planning_output_invalid')
        app = MigrationOperations._new_application()
        app.update(repair_context=context, format_context={'stage': 'contract_diagnose',
            'execution_id': 'bad-format', 'result': old['result']['value']})
        app['effective']['contract_diagnose'] = {'status': 'failed'}
        state = {'application_state': app, 'tasks': {'contract_diagnose': {'attempts': [
            old, attempt('accepted', 'completed'), attempt('interrupted', 'blocked', 'agent_timeout')]}}}
        reopened = MigrationOperations._reopen_application(Path('unused'), state,
            stage='contract_diagnose', deadline_epoch=1000, max_agent_assignments=10)
        self.assertIsNone(reopened['format_context'])
        state['tasks']['contract_diagnose']['attempts'].append(
            attempt('new-format-error', 'failed', 'planning_output_invalid'))
        reopened = MigrationOperations._reopen_application(Path('unused'), state,
            stage='contract_diagnose', deadline_epoch=1000, max_agent_assignments=10)
        self.assertEqual('new-format-error', reopened['format_context']['execution_id'])

    def test_review_nonobject_outputs_keep_actual_model_logs_and_snapshots(self):
        for baseline in (False, True):
            for invalid in ([], None, 42, 'not a report'):
                with self.subTest(baseline=baseline, invalid=invalid), tempfile.TemporaryDirectory() as directory:
                    root = Path(directory)
                    if baseline:
                        handler_fixtures.HandlerTests._write_contract_fixture(root)
                    stage = 'contract_review' if baseline else 'code_review'
                    workspace = root / ('baseline' if baseline else 'worktree')
                    (workspace / '.modport').mkdir(parents=True, exist_ok=True)
                    command = handler_fixtures.HandlerTests._command(root, stage)
                    real_exec = handlers._exec
                    def execute(args, **kwargs):
                        if args[0] != 'codex':
                            return real_exec(args, **kwargs)
                        atomic_json(workspace / ('.modport/' + stage.replace('_', '-') + '.json'), invalid)
                        message = {'type': 'item.completed', 'item': {'type': 'agent_message', 'text': 'report written'}}
                        return subprocess.CompletedProcess(args, 0, json.dumps(message) + '\n')
                    # Contract fixture has synthetic baseline bookkeeping; this test owns only report validation.
                    with patch('modport.handlers._exec', side_effect=execute), \
                         patch('modport.handlers._baseline_changes_are_isolated', return_value=(True, [])):
                        result = handlers.ReviewHandler(baseline=baseline)(command)
                    self.assertEqual(result.error_code, 'review_invalid', result.detail)
                    self.assertIn('explicit approved/rejected routing decision', result.detail)
                    self.assertTrue(result.outputs.get('log'))
                    self.assertTrue(result.outputs.get('last_message'))
                    refs = result.outputs['artifact_refs']
                    saved = [ref for key, ref in refs.items() if key.endswith(stage.replace('_', '-') + '.json')]
                    self.assertEqual(len(saved), 1)
                    self.assertEqual(json.loads(verified_path(root, saved[0]).read_text()), invalid)
                    copied = snapshot_repair_evidence(root, result.to_dict())
                    self.assertTrue(any(ref['path'].startswith('artifacts/repair-evidence/')
                                        for ref in copied['outputs']['artifact_refs'].values()))

    def characterize(self, extra):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        _, contract = handler_fixtures.HandlerTests._write_contract_fixture(root)
        command = handler_fixtures.HandlerTests._command(root, 'contract_verify')
        test_id = 'game_test.behavior_1'
        declaration = contract['test_evidence'][test_id]
        def sandbox(root, worktree, args, **kwargs):
            return ['MOCK', kwargs['environment']['MODPORT_EVIDENCE_NONCE']]
        def execute(args, *, cwd, log, timeout=None):
            nonce = args[1]
            record = {'test_id': test_id, 'evidence_kind': 'runtime', 'executor': 'gametest',
                      'source_fingerprint': 'immutable-source-commit', 'execution_nonce': nonce,
                      'execution_inputs': ['fixture baseline'], 'runtime_operations': declaration['runtime_operations'],
                      'runtime_witnesses': [{'operation': op, 'event_index': index, 'invocation': 'fixture-event',
                          'observations': {'fixture': True}, 'execution_nonce': nonce}
                          for index, op in enumerate(declaration['runtime_operations'])],
                      'observations': {'fixture': True}, 'status': 'passed'}
            atomic_json(root / 'baseline' / declaration['path'], record)
            stdout = extra + f'MODPORT_RUNTIME_WITNESS {nonce} {test_id}\nBUILD SUCCESSFUL\n'
            log.write_text(stdout)
            return subprocess.CompletedProcess(args, 0, stdout)
        with patch('modport.handlers._sandboxed_build_command', side_effect=sandbox), \
             patch('modport.handlers._forge_baseline_init', return_value='mock.gradle'), \
             patch('modport.handlers._exec', side_effect=execute):
            return handlers.BaselineContractVerificationHandler()(command)

    def test_no_source_applies_only_to_exact_declared_task(self):
        control = self.characterize('')
        self.assertEqual(control.status, 'completed', control.detail)
        for output in ('> Task :processTestResources NO-SOURCE\n',
                       'Explanation mentions NO-SOURCE\n',
                       '> Task :other:runGameTestServer NO-SOURCE\n'):
            result = self.characterize(output)
            self.assertEqual(result.status, 'completed', result.detail)
            self.assertEqual(result.outputs['no_source_tasks'], [])
        declared = control.outputs['tasks'][0]
        result = self.characterize('> Task :' + declared.lstrip(':') + ' NO-SOURCE\n')
        self.assertEqual(result.error_code, 'baseline_contract_failed')
        self.assertEqual(result.outputs['record_errors'], [])
        self.assertEqual(result.outputs['no_source_tasks'], [':' + declared.lstrip(':')])

    def test_timeout_partial_log_survives_result_and_repair_snapshot(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'baseline').mkdir()
            (root / 'baseline/gradlew').write_text('fixture, never run')
            command = handler_fixtures.HandlerTests._command(root, 'baseline_build')
            def execute(args, *, cwd, log, timeout=None):
                log.write_text('partial compiler diagnostic before timeout')
                raise subprocess.TimeoutExpired(args, timeout, output=log.read_text())
            with patch('modport.handlers._sandboxed_build_command', return_value=['MOCK']), \
                 patch('modport.handlers._forge_baseline_init', return_value='mock.gradle'), \
                 patch('modport.handlers._exec', side_effect=execute):
                result = handlers.GradleHandler(baseline=True, tasks=('build',), name='baseline-build')(command)
            self.assertEqual(result.error_code, 'gradle_timeout')
            self.assertTrue(result.outputs.get('log'))
            copied = snapshot_repair_evidence(root, result.to_dict())
            refs = copied['outputs']['artifact_refs']
            self.assertTrue(refs)
            for ref in refs.values():
                self.assertEqual(verified_path(root, ref).read_text(), 'partial compiler diagnostic before timeout')

    def test_reopen_host_context_and_manifest_reach_second_planner_turn(self):
        from test_planning import PlanningTests
        from test_planning_operations import PlanningPolicyTests
        from modport.development import _artifact
        from modport.planning import PlanningHandler
        from modport.rubric import acceptance_rubric
        fixture = PlanningTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        shutil.copytree(fixture.work, fixture.root / 'baseline')
        payload = fixture.repair_payload('contract_diagnose')
        for key in ('agent_rules', 'evidence_protocol'):
            fixture.refs[key] = _artifact(fixture.command('contract_diagnose', 'rules-' + key), key + '.md', b'Fixture rules')
        payload['repair_context']['artifact_refs'] = deepcopy(fixture.refs)
        prior_command = replace(fixture.command('contract_diagnose', 'prior-diagnosis'), payload=payload)
        prior = OperationResult('failed', prior_command.run_id, prior_command.task_id, prior_command.stage_id,
            prior_command.command_id, outputs={'validation_error': {'code': 'invalid_type',
                'path': '/failure_analysis/previous_attempts_analysis', 'actual_type': 'object', 'expected_type': 'string'}},
            detail='previous_attempts_analysis expected string, got object', error_code='planning_output_invalid')
        policy = PlanningPolicyTests()
        policy.setUp()
        self.addCleanup(policy.doCleanups)
        policy.header.update(run_dir=str(fixture.root), initial_refs=deepcopy(fixture.refs))
        policy.snapshot['run_id'] = 'run'
        app = policy.operations._new_application()
        app.update(payload)
        app['effective']['contract_diagnose'] = prior.to_dict()
        # Latest task result must override stale/missing projection feedback.
        app['format_context'] = {'stage': 'target_diagnose', 'execution_id': 'unrelated'}
        policy.snapshot.update(application_state=app, tasks={'contract_diagnose': {'attempts': [{
            'state': 'succeeded', 'command': {'execution_id': prior.command_id, 'payload': prior_command.to_dict()},
            'result': {'value': prior.to_dict()}}]}})
        reopened = MigrationOperations._reopen_application(fixture.root, policy.snapshot,
            stage='contract_diagnose', deadline_epoch=10**12, max_agent_assignments=100)
        operations = policy.operations._schedule(policy.snapshot, policy.header, reopened, 'contract_diagnose', dependencies=[])
        command, = policy.scheduled(operations)
        self.assertEqual(command.payload['format_context']['execution_id'], prior.command_id)
        self.assertEqual(command.payload['repair_context']['current_failure'], payload['repair_context']['current_failure'])
        prompts = []
        real_exec = handlers._exec
        def execute(args, **kwargs):
            if args[0] != 'codex':
                return real_exec(args, **kwargs)
            prompts.append(kwargs['input_text'])
            events = []
            if len(prompts) == 1:
                events.append({'type': 'thread.started',
                               'thread_id': '00000000-0000-0000-0000-000000000228'})
            events.append({'type': 'item.completed', 'item': {
                'type': 'agent_message',
                'text': '# Plan\nInspect prior format feedback.' if len(prompts) == 1 else '{}'}})
            return subprocess.CompletedProcess(
                args, 0, '\n'.join(json.dumps(event) for event in events) + '\n')
        with patch('modport.handlers._acceptance_rubric_for', return_value=acceptance_rubric()), \
             patch('modport.handlers._exec', side_effect=execute):
            result = PlanningHandler()(command)
        self.assertEqual(len(prompts), 2, result.detail)
        self.assertIn('Turn 1 of 2', prompts[0])
        self.assertIn('Turn 2 of 2', prompts[1])
        for expected in ('Complete host failure context', 'ColorList reload race',
                         'Host routing context (do not echo)', 'planning-input-manifest.json',
                         'Original preceding reports'):
            self.assertIn(expected, prompts[1])
        manifest_line = next(line for line in prompts[1].splitlines()
                             if line.startswith('Planning input manifest (relative to run_dir): '))
        manifest_ref = json.loads(manifest_line.split(': ', 1)[1])
        manifest = json.loads(verified_path(fixture.root, manifest_ref).read_text())
        feedback = manifest['format_feedback']
        self.assertEqual(feedback['previous_execution_id'], 'prior-diagnosis')
        self.assertEqual(feedback['validation_error']['code'], 'invalid_type')
        self.assertEqual(feedback['validation_error']['path'],
                         '/failure_analysis/previous_attempts_analysis')
        self.assertEqual(feedback['validation_error']['actual_type'], 'object')
        self.assertEqual(feedback['validation_error']['expected_type'], 'string')
