"""Current final cleanup routing and durable mutation recovery; no game acceptance claim."""
from dataclasses import replace
import json
from pathlib import Path
import subprocess
import tempfile
import time
import unittest
from unittest.mock import patch

from modport.cleanup import CodeCleanupHandler, FinalCleanupHandler
from modport.contracts import OperationInput, OperationResult
from modport.evidence import atomic_json
from modport.memory_admission import MemorySnapshot
from modport.models import Budget, MigrationRequest
from modport.operations import MigrationOperations
from modport.payload_storage import unpack_input
from modport.workflow import WORKFLOW_VERSION, compile_migration_workflow, stage_routes


class FinalCleanupTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        request = MigrationRequest('example', 'https://example.invalid/source.git',
            '1.20.1', '1.21.1', budget=Budget(max_seconds=120, max_agent_assignments=20))
        self.header = {'request': request.to_dict(),
            'definition': compile_migration_workflow(request).to_dict(),
            'run_dir': str(self.root), 'initial_refs': {}, 'prior_findings': [],
            'registry_revision': 'host-registry', 'rubric_sha256': 'host-provenance',
            'deadline_epoch': time.time() + 120, 'continuation': {}}
        self.host = MigrationOperations(memory_probe=lambda: MemorySnapshot(
            64 * 1024**3, 64 * 1024**3, 'fixture'))
        self.app = self.host._new_application()
        self.snapshot = {'run_id': 'final-run', 'state': 'running',
            'tasks': {}, 'waits': {}, 'application_state': self.app}
        atomic_json(self.root / 'lock.json', {'contract': {
            'test_evidence': {'target.case': {'evidence_kind': 'runtime',
                'test_source_files': ['src/test/java/Case.java']}},
            'behaviors': [{'assertion_contracts': [{'assertion_id': 'assert.case',
                'test_ids': ['target.case']}]}]}})
        self.lock = {'path': 'lock.json', 'media_type': 'application/json'}

    def command(self, stage):
        return OperationInput('final-run', stage, stage, 'final-run:' + stage + ':1',
            str(self.root), options={'workflow_version': WORKFLOW_VERSION,
                'business_gates_disabled': True}, artifact_refs={'functional_contract_lock': self.lock})

    def result(self, stage, outputs=None, status='completed', error=None):
        command = self.command(stage)
        result = OperationResult(status, command.run_id, command.task_id, stage,
            command.command_id, outputs or {}, error_code=error)
        self.app['effective'][stage] = result.to_dict()
        return result

    def accepted(self):
        self.result('target_contract_freeze', {'artifact_refs': {'functional_contract_lock': self.lock}})
        self.result('target_build', {'build_status': 'completed', 'build_executed': True})
        self.result('code_review', {'verdict': 'rejected', 'business_diagnostics': ['advisory finding']},
            status='failed', error='code_review_rejected')
        self.result('acceptance_build', {'process_executed': True,
            'case_results': {'target.case': {'status': 'passed', 'test_outcome': 'passed'}},
            'assertion_results': {'assert.case': {'status': 'passed'}},
            'evidence_records': {'target.case': {'evidence_kind': 'runtime', 'path': 'witness.json'}}})
        self.result('gap_review')

    def scheduled(self, operations):
        return [OperationInput.from_dict(unpack_input(self.root, operation['command']['payload']))
            for operation in operations if operation['kind'] in {'add_task', 'new_attempt'}]

    def successor(self, stage):
        return self.host._flowthrough_schedule_successor(self.snapshot, self.header,
            self.app, stage, self.command(stage).command_id)

    def test_current_full_route_and_immutable_artifact_mode(self):
        self.assertEqual(37, WORKFLOW_VERSION)
        main, _, next_stage, _ = stage_routes(self.header)
        self.assertEqual('final_cleanup', next_stage['gap_review'])
        self.assertEqual('target_build', next_stage['final_cleanup'])
        self.assertEqual(main.index('delivery') - 1, main.index('final_cleanup'))
        for mode, scope in [('artifact_verification', 'full'), ('migration', 'compile_package'),
                            ('skill_generation', 'full')]:
            request = MigrationRequest('example', 'https://example.invalid/source.git',
                '1.20.1', '1.21.1', workflow_mode=mode, validation_scope=scope,
                skill_kind='java' if mode == 'skill_generation' else None,
                budget=Budget(max_seconds=120))
            definition = compile_migration_workflow(request).to_dict()
            self.assertNotIn('final_cleanup_policy', definition)
            self.assertNotIn('final_cleanup', definition.get('main_stages', []))

    def test_final_cleanup_once_then_fresh_validation_before_delivery(self):
        self.accepted()
        self.assertEqual(['final_cleanup'], [command.stage_id for command in self.scheduled(self.successor('gap_review'))])
        self.assertEqual('scheduled', self.app['final_cleanup']['phase'])
        self.result('final_cleanup', {'candidate_after': 'host-collected'})
        self.app['effective']['test_execute.g1.join'] = self.result('test_execute').to_dict()
        self.assertEqual(['target_build'], [command.stage_id for command in self.scheduled(self.successor('final_cleanup'))])
        self.assertIn('target_contract_freeze', self.app['effective'])
        for stage in ('target_build', 'code_review', 'acceptance_build', 'test_execute.g1.join', 'gap_review'):
            self.assertNotIn(stage, self.app['effective'])
        self.assertEqual('revalidating', self.app['final_cleanup']['phase'])
        self.accepted()
        commands = self.scheduled(self.successor('gap_review'))
        self.assertEqual(['delivery'], [command.stage_id for command in commands])
        self.assertEqual('complete', self.app['final_cleanup']['phase'])
        self.assertEqual(['delivery'], [command.stage_id for command in self.scheduled(self.successor('gap_review'))])

    def test_missing_or_skipped_frozen_case_never_delivers(self):
        for phase in (None, 'revalidating', 'complete'):
            with self.subTest(phase=phase):
                self.app = self.host._new_application()
                self.accepted()
                if phase:
                    self.app['final_cleanup'] = {'phase': phase}
                self.app['effective']['acceptance_build']['outputs']['case_results']['target.case']['test_outcome'] = 'skipped'
                self.assertEqual('failed', self.successor('gap_review')[-1]['state'])
                self.assertEqual('required_target_acceptance_incomplete', self.app['terminal_reason'])

    def test_cleanup_failure_is_terminal_despite_diagnostic_gate_policy(self):
        self.accepted()
        self.app['final_cleanup'] = {'phase': 'scheduled'}
        self.result('final_cleanup', status='failed', error='cleanup_integrity')
        self.assertEqual('failed', self.successor('final_cleanup')[-1]['state'])
        self.assertEqual('cleanup_integrity', self.app['terminal_reason'])

    def test_exhausted_final_assignment_cannot_finish_as_success(self):
        self.accepted()
        self.app['agent_assignments'] = self.header['request']['budget']['max_agent_assignments']
        self.assertEqual('failed', self.successor('gap_review')[-1]['state'])
        self.assertEqual('agent_assignment_budget_exhausted', self.app['terminal_reason'])

    def test_actual_rejected_review_producer_remains_diagnostic(self):
        from modport.handlers import ReviewHandler
        self.accepted()
        command = self.command('code_review')
        workspace = self.root / 'worktree'
        review_path = workspace / '.modport/code-review.json'
        review_path.parent.mkdir(parents=True)

        def reviewer(value):
            review_path.write_text('Reviewer found a maintainability issue.\n'
                '```json\n{"verdict":"rejected","findings":[{"finding_id":"review.one",'
                '"severity":"minor","summary":"Simplify duplicate branch",'
                '"evidence":"Example.java repeats branch", "requested_change":"Remove repetition"}]}\n```\n')
            return OperationResult('completed', value.run_id, value.task_id, value.stage_id, value.command_id)

        with patch('modport.handlers.CodexStageHandler', return_value=reviewer), \
                patch('modport.handlers._acceptance_rubric_for', return_value={'rubric_id': 'host', 'rubric_version': 1}), \
                patch('modport.rework_tools.refresh_review_command', side_effect=lambda value: value):
            outcome = ReviewHandler(baseline=False)(command)
        self.assertEqual('failed', outcome.status, outcome.detail)
        self.assertEqual('code_review_rejected', outcome.error_code)
        self.app['effective']['code_review'] = outcome.to_dict()
        self.assertEqual('passed', self.host._final_cleanup_acceptance(self.header, self.app)['status'])
        self.assertEqual(['final_cleanup'], [item.stage_id for item in self.scheduled(self.successor('gap_review'))])

    def test_fresh_review_routes_to_frozen_execution_without_designing_tests(self):
        self.accepted()
        self.app['final_cleanup'] = {'phase': 'revalidating'}
        command = self.command('code_review')
        review = self.result('code_review', {'verdict': 'rejected'})
        self.app['active_stage'] = command.task_id
        self.snapshot['tasks'][command.task_id] = {'attempts': [{'state': 'succeeded',
            'command': {'execution_id': command.command_id, 'payload': command.to_dict()},
            'result': {'value': review.to_dict()}}]}
        with patch('modport.regression.start_regression', side_effect=AssertionError('no redesign')):
            commands = self.scheduled(self.host._advance_without_business_gates(
                self.snapshot, self.header, self.app))
        self.assertEqual(['acceptance_preflight'], [item.stage_id for item in commands])
        self.assertTrue(commands[0].options['final_cleanup_revalidation'])
        self.assertEqual(self.lock, commands[0].artifact_refs['functional_contract_lock'])

    def test_saved_recovery_edge_cannot_repeat_cleanup_after_revalidation(self):
        self.accepted()
        self.app['final_cleanup'] = {'phase': 'revalidating'}
        self.app['flowthrough_resume'] = {'stage': 'gap_review', 'location': 'main',
            'next_stage': 'final_cleanup', 'command_id': 'before-crash'}
        commands = self.scheduled(self.host._advance_without_business_gates(
            self.snapshot, self.header, self.app))
        self.assertEqual(['delivery'], [item.stage_id for item in commands])
        self.assertEqual('complete', self.app['final_cleanup']['phase'])

    def test_final_delivery_requires_regular_nonempty_jar(self):
        self.accepted()
        self.app['final_cleanup'] = {'phase': 'complete'}
        self.result('delivery', {'jars': []})
        self.assertEqual('failed', self.host._flowthrough_finish(self.app, header=self.header)[-1]['state'])
        self.assertEqual('final_delivery_package_missing', self.app['terminal_reason'])
        artifact = self.root / 'worktree/build/libs/example.jar'
        artifact.parent.mkdir(parents=True)
        artifact.write_bytes(b'packaged-test-fixture')
        self.app['terminal_reason'] = None
        self.result('delivery', {'jars': [{'path': 'worktree/build/libs/example.jar'}]})
        self.assertEqual('succeeded', self.host._flowthrough_finish(self.app, header=self.header)[-1]['state'])
        artifact.unlink()
        artifact.symlink_to(self.root / 'lock.json')
        self.app['terminal_reason'] = None
        self.assertEqual('failed', self.host._flowthrough_finish(self.app, header=self.header)[-1]['state'])

    def test_terminal_fallback_requires_final_revalidation(self):
        self.accepted()
        operations = self.host._flowthrough_finish(self.app, header=self.header)
        self.assertEqual('failed', operations[-1]['state'])
        self.assertEqual('final_cleanup_revalidation_incomplete', self.app['terminal_reason'])

    def test_settled_checkpoint_replay_never_mutates_twice(self):
        handler = FinalCleanupHandler()
        command = self.command('final_cleanup')
        result = OperationResult('completed', command.run_id, command.task_id,
            command.stage_id, command.command_id, {'candidate_after': 'host-result'})
        with patch.object(CodeCleanupHandler, '__call__', return_value=result) as delegate:
            first = handler(command)
            second = handler(replace(command, command_id='final-run:final_cleanup:2'))
        self.assertEqual(1, delegate.call_count)
        self.assertEqual('completed', first.status)
        self.assertEqual('final-run:final_cleanup:2', second.command_id)
        self.assertTrue(second.outputs['cleanup_replayed'])

    def git(self, *arguments):
        result = subprocess.run(['git', *arguments], cwd=self.root / 'worktree',
            text=True, capture_output=True, check=True)
        return result.stdout.strip()

    def prepared_patch(self):
        workspace = self.root / 'worktree'
        workspace.mkdir()
        self.git('init', '-q')
        self.git('config', 'user.name', 'Cleanup test')
        self.git('config', 'user.email', 'test@localhost')
        code = workspace / 'Example.java'
        code.write_text('class Example { int value = 1; }\n')
        self.git('add', '--all')
        self.git('commit', '-qm', 'candidate')
        before = self.git('rev-parse', 'HEAD')
        code.write_text('class Example { final int value = 1; }\n')
        delta = subprocess.run(['git', 'diff', '--binary', '--full-index', '--no-ext-diff', '--no-textconv', '--no-renames'], cwd=workspace,
            text=True, capture_output=True, check=True).stdout
        self.git('checkout', '--', 'Example.java')
        patch_path = self.root / 'cleanup.patch'
        patch_path.write_text(delta)
        command = self.command('final_cleanup')
        agent = OperationResult('completed', command.run_id, command.task_id,
            command.stage_id, command.command_id)
        handler = FinalCleanupHandler()
        handler.prepare_integration(command, self.root, before=before, collected='host-candidate',
            changed_paths=['Example.java'], patch_ref={'path': 'cleanup.patch'}, report_ref=None,
            workspace='workspaces/final', agent_result=agent.to_dict())
        return handler, command, code, patch_path

    def test_prepared_patch_recovery_reuses_published_patch_without_model(self):
        handler, command, code, _ = self.prepared_patch()
        with patch.object(CodeCleanupHandler, '__call__', side_effect=AssertionError('model must not rerun')):
            result = handler(command)
            replay = handler(command)
        self.assertEqual('completed', result.status, result.detail)
        self.assertTrue(result.outputs['cleanup_recovered'])
        self.assertTrue(replay.outputs['cleanup_replayed'])
        self.assertEqual('2', self.git('rev-list', '--count', 'HEAD'))
        self.assertIn('final int value', code.read_text())

    def test_committed_delta_recovers_without_duplicate_commit(self):
        handler, command, code, patch_path = self.prepared_patch()
        self.git('apply', '--index', str(patch_path))
        self.git('commit', '-qm', 'Integrate development task code-cleanup')
        result = handler(command)
        self.assertEqual('completed', result.status, result.detail)
        self.assertEqual('2', self.git('rev-list', '--count', 'HEAD'))
        self.assertIn('final int value', code.read_text())

    def test_pending_exact_patch_recovers_but_unrelated_same_path_edit_is_preserved(self):
        handler, command, code, patch_path = self.prepared_patch()
        self.git('apply', '--index', str(patch_path))
        code.write_text('class Example { final int value = 2; }\n')
        result = handler(command)
        self.assertEqual('failed', result.status)
        self.assertEqual('cleanup_recovery_required', result.error_code)
        self.assertIn('value = 2', code.read_text())
        self.assertEqual('1', self.git('rev-list', '--count', 'HEAD'))

    def test_same_subject_commit_with_unrelated_content_is_never_replayed_as_cleanup(self):
        handler, command, code, _ = self.prepared_patch()
        code.write_text('class Example { final int value = 2; }\n')
        self.git('add', '--all')
        self.git('commit', '-qm', 'Integrate development task code-cleanup')
        result = handler(command)
        self.assertEqual('failed', result.status)
        self.assertIn('value = 2', code.read_text())
        self.assertEqual('2', self.git('rev-list', '--count', 'HEAD'))

    def test_missing_active_marker_recovers_latest_post_cleanup_stage(self):
        self.accepted()
        self.result('final_cleanup')
        self.app['final_cleanup'] = {'phase': 'revalidating'}
        self.app['history'] = [{'stage': 'final_cleanup', 'execution_id': self.command('final_cleanup').command_id},
            {'stage': 'target_build', 'execution_id': self.command('target_build').command_id}]
        commands = self.scheduled(self.host._advance_without_business_gates(
            self.snapshot, self.header, self.app))
        self.assertEqual(['code_review'], [item.stage_id for item in commands])
        self.assertIn('acceptance_build', self.app['effective'])

    def test_actual_freeze_and_runtime_snapshot_outputs_survive_cleanup_in_place(self):
        from modport.handlers import _snapshot_stage_output
        from modport.target_contract import TargetContractFreezeHandler
        from test_target_contract import requirements, target_contract
        workspace = self.root / 'worktree'
        declaration = target_contract()
        atomic_json(workspace / '.modport/functional-contract.json', declaration)
        harness = workspace / declaration['test_evidence']['target.damage']['test_source_files'][0]
        harness.parent.mkdir(parents=True)
        harness.write_text('class HealthGameTest {}\n')
        code = workspace / 'Example.java'
        code.write_text('class Example { int value = 1; }\n')
        self.git('init', '-q')
        self.git('config', 'user.name', 'Cleanup test')
        self.git('config', 'user.email', 'test@localhost')
        self.git('add', '--all')
        self.git('commit', '-qm', 'candidate')
        atomic_json(self.root / 'requirements.json', {'schema_version': 1,
            'requirements': requirements(), 'source_commit': 'host-original'})
        freeze = replace(self.command('target_contract_freeze'), artifact_refs={
            'behavior_requirements': {'path': 'requirements.json'}})
        frozen = TargetContractFreezeHandler()(freeze)
        self.assertEqual('completed', frozen.status, frozen.detail)
        frozen_bytes = (workspace / '.modport/functional-contract.json').read_bytes()
        evidence = workspace / '.modport/evidence/target.damage.json'
        atomic_json(evidence, {'evidence_kind': 'runtime', 'observed_health': 19})
        _, runtime_ref = _snapshot_stage_output(self.root, workspace,
            self.command('acceptance_build'), '.modport/evidence/target.damage.json')
        runtime_bytes = evidence.read_bytes()
        command = replace(self.command('final_cleanup'), artifact_refs={
            **frozen.outputs['artifact_refs'], 'target_runtime_evidence:target.damage': runtime_ref})

        def cleanup(value):
            clone = self.root / value.options['workspace']
            self.assertEqual(frozen_bytes, (clone / '.modport/functional-contract.json').read_bytes())
            (clone / 'Example.java').write_text('class Example { final int value = 1; }\n')
            return OperationResult('completed', value.run_id, value.task_id, value.stage_id, value.command_id)

        with patch('modport.handlers.CodexStageHandler', return_value=cleanup):
            outcome = FinalCleanupHandler()(command)
        self.assertEqual('completed', outcome.status, outcome.detail)
        self.assertEqual(['Example.java'], outcome.outputs['changed_paths'])
        self.assertEqual(frozen_bytes, (workspace / '.modport/functional-contract.json').read_bytes())
        self.assertEqual(runtime_bytes, evidence.read_bytes())
        self.assertIn('final int value', code.read_text())
        self.assertIn('.modport/functional-contract.json', self.git('status', '--porcelain'))

    def test_formal_sdk_recovery_publishes_missing_receipt_from_final_checkpoint(self):
        from modport.application_state_storage import hydrate_run_snapshot
        original_lock = (self.root / 'lock.json').read_bytes()
        self.root = self.root / 'formal-recovery'

        class CrashAfterIntegration:
            __execution_kernel_revision__ = 'final-cleanup-crash-fixture-v37'

            def __call__(inner, operation):
                checkpoint = Path(operation.run_dir) / 'artifacts/final-cleanup/checkpoint.json'
                published_patch = json.loads(checkpoint.read_text())
                result = FinalCleanupHandler()(operation)
                if result.status != 'completed':
                    raise AssertionError(result.detail)
                # Represent the crash window after Git commit but before the
                # settled journal update and ordinary SDK stage receipt.
                atomic_json(checkpoint, published_patch)
                raise RuntimeError('fixture crash after integration before journal settlement and stage receipt')

        class Build:
            __execution_kernel_revision__ = 'final-cleanup-followup-build-fixture-v37'

            def __call__(inner, operation):
                return OperationResult('completed', operation.run_id, operation.task_id,
                    operation.stage_id, operation.command_id)

        owner = MigrationOperations(handlers={'modport.final_cleanup': CrashAfterIntegration(),
            'modport.target_build': Build()}, isolation_mode='thread', memory_probe=lambda: MemorySnapshot(
                64 * 1024**3, 64 * 1024**3, 'fixture'))
        request = MigrationRequest('example', 'https://example.invalid/source.git',
            '1.20.1', '1.21.1', budget=Budget(max_seconds=180, max_agent_assignments=20))
        owner.submit(request, run_dir=self.root, run_id='final-run')
        (self.root / 'lock.json').write_bytes(original_lock)
        handler, _, code, _ = self.prepared_patch()
        with owner.session(self.root, 'final-run') as (_, header, runtime, sdk):
            app = owner._new_application()
            app['final_cleanup'] = {'phase': 'scheduled'}
            app['effective']['target_contract_freeze'] = self.result('target_contract_freeze', {
                'artifact_refs': {'functional_contract_lock': self.lock}}).to_dict()
            changes = owner._schedule(sdk.get_run('final-run'), header, app,
                'final_cleanup', dependencies=[])
            state = sdk.get_run('final-run')
            sdk.apply_operations('final-run', command_id='seed-final-cleanup',
                expected_revision=state['revision'], operations=changes, application_state=app)
            sdk.flush()
            runtime.run_once()
            sdk.sync()
            recovery, = sdk.inspect_recoveries('final-run')
            self.assertEqual('final_cleanup', recovery.task_id)
            original_input = recovery.execution.command.payload
            execution_id = recovery.execution.execution_id
            original_deadline = header['deadline_epoch']
            original_assignments = hydrate_run_snapshot(self.root, sdk.get_run('final-run'))['application_state']['agent_assignments']
        receipt_path = self.root / 'artifacts/executions' / execution_id / 'receipt.json'
        self.assertFalse(receipt_path.exists())
        self.assertEqual('2', self.git('rev-list', '--count', 'HEAD'))
        recovered = owner.recover(self.root, 'final-run')
        self.assertTrue(receipt_path.is_file())
        receipt = json.loads(receipt_path.read_text())
        self.assertEqual('completed', receipt['response']['status'])
        self.assertTrue(receipt['response']['outputs']['cleanup_recovered'])
        self.assertEqual('2', self.git('rev-list', '--count', 'HEAD'))
        state = recovered.snapshot
        with owner.session(self.root, 'final-run') as (_, header, runtime, sdk):
            runtime.run_once()
            sdk.sync()
            owner.tick(sdk, header)
            state = hydrate_run_snapshot(self.root, sdk.get_run('final-run'))
        self.assertEqual(original_input, state['tasks']['final_cleanup']['attempts'][0]['command']['payload'])
        self.assertEqual(original_deadline, state['input']['deadline_epoch'])
        self.assertEqual(original_assignments, state['application_state']['agent_assignments'])
        self.assertEqual('revalidating', state['application_state']['final_cleanup']['phase'])
        self.assertIn('target_build', state['tasks'])
        self.assertIn('final int value', code.read_text())

    def test_unrelated_dirty_product_is_preserved_and_refuses_cleanup(self):
        handler, command, code, _ = self.prepared_patch()
        code.write_text('class Example { int unrelated = 7; }\n')
        outcome = handler(command)
        self.assertEqual('failed', outcome.status)
        self.assertIn('unrelated = 7', code.read_text())
        self.assertEqual('1', self.git('rev-list', '--count', 'HEAD'))

    def test_final_cleanup_preserves_frozen_tests_and_artifact_product(self):
        handler = FinalCleanupHandler()
        with self.assertRaisesRegex(ValueError, 'frozen tests'):
            handler.prepare_integration(self.command('final_cleanup'), self.root,
                changed_paths=['src/test/java/Case.java'])
        command = replace(self.command('final_cleanup'), options={'validation_policy': {'scope': 'artifact_verification'}})
        self.assertEqual('artifact_product_immutable', handler(command).error_code)
        self.assertFalse((self.root / 'artifacts/final-cleanup').exists())


if __name__ == '__main__':
    unittest.main()
