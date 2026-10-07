"""Current integration merges preserve real Git candidates and original deltas."""

from dataclasses import replace
import json
from pathlib import Path
import subprocess
import tempfile
import time
import unittest

from modport.contracts import OperationInput, OperationResult
from modport.development import DevelopmentIntegrateHandler, validate_plan
from modport.workflow import WORKFLOW_VERSION


def git(workspace, *arguments):
    result = subprocess.run(
        ['git', '-c', 'core.hooksPath=/dev/null', '-c', 'user.name=Fixture',
         '-c', 'user.email=fixture@localhost', *arguments], cwd=workspace,
        text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, check=True)
    return result.stdout.strip()


class CurrentIntegrationMergeTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / 'run'
        self.worktree = self.root / 'worktree'
        self.worktree.mkdir(parents=True)
        git(self.worktree, 'init', '-q')
        (self.worktree / 'shared.txt').write_text('original shared\n')
        (self.worktree / 'feature.txt').write_text('original feature\n')
        contract = self.worktree / '.modport' / 'target-contract.json'
        contract.parent.mkdir()
        contract.write_text('{"cases": ["original"]}\n')
        git(self.worktree, 'add', '.')
        git(self.worktree, 'commit', '-qm', 'Initial source')
        self.base = git(self.worktree, 'rev-parse', 'HEAD')
        self.tasks = [
            {'id': 'shared', 'objective': 'Migrate shared behavior', 'owned_paths': ['shared.txt'], 'dependencies': []},
            {'id': 'feature', 'objective': 'Migrate another behavior', 'owned_paths': ['feature.txt'], 'dependencies': []},
            {'id': 'contract', 'objective': 'Retain executable target mapping',
             'owned_paths': ['.modport/target-contract.json'], 'dependencies': []},
        ]
        self.plan = validate_plan({'schema_version': 1, 'base_commit': self.base,
                                   'shared_paths': [], 'tasks': self.tasks},
                                  workflow_version=WORKFLOW_VERSION)
        (self.root / 'plan.json').write_text(json.dumps(self.plan))
        self.plan_ref = {'path': 'plan.json', 'metadata': {'development_base': self.base}}
        self.results = []
        values = ('migrated shared\n', 'migrated feature\n', '{"cases": ["retained-target-case"]}\n')
        for task, value in zip(self.plan['tasks'], values):
            clone = self.root / ('author-' + task['id'])
            git(self.root, 'clone', '-q', '--no-hardlinks', '--', str(self.worktree), str(clone))
            path = task['owned_paths'][0]
            (clone / path).write_text(value)
            git(clone, 'add', '--', path)
            git(clone, 'commit', '-qm', 'Author delta')
            patch_path = self.root / (task['id'] + '.patch')
            git(clone, 'diff', '--binary', '--full-index', '--no-ext-diff', '--no-textconv',
                '--no-renames', '--output=' + str(patch_path), self.base, 'HEAD', '--')
            patch_ref = {'path': patch_path.relative_to(self.root).as_posix(), 'metadata': {
                'task_id': task['id'], 'generation': 1, 'base': self.base, 'paths': [path]}}
            self.results.append(OperationResult(
                'completed', 'run', 'coder.' + task['id'], 'coder', 'author-' + task['id'],
                outputs={'development_task_id': task['id'], 'artifact_refs': {'coder_patch': patch_ref}}).to_dict())
        self.command = OperationInput(
            'run', 'development_integrate', 'development_integrate', 'integrate-1', str(self.root),
            payload={'development_base': self.base, 'development_generation': 1,
                     'development_results': self.results, 'execution_development_plan': self.plan,
                     'goal_scope': 'migration'},
            options={'workflow_version': WORKFLOW_VERSION, 'workspace': 'worktree',
                     'deadline_epoch': time.time() + 600},
            artifact_refs={'development_plan': self.plan_ref})

    def integrate(self, command=None):
        return DevelopmentIntegrateHandler()(command or self.command)

    def conflict(self):
        (self.worktree / 'shared.txt').write_text('user shared\n')
        git(self.worktree, 'add', 'shared.txt')
        git(self.worktree, 'commit', '-qm', 'User edit')
        result = self.integrate()
        self.assertEqual('integration_merge_required', result.error_code, result.detail)
        record = json.loads((self.root / result.outputs['integration_merge_ref']['path']).read_text())
        return result, record

    def resolution(self, outcome, record, content='user shared and migrated shared\n', command=None):
        clone = self.root / 'repair-author'
        git(self.root, 'clone', '-q', '--no-hardlinks', '--', str(self.root / record['workspace']), str(clone))
        git(clone, 'checkout', '--detach', record['merge_head'])
        (clone / 'shared.txt').write_text(content)
        git(clone, 'add', 'shared.txt')
        git(clone, 'commit', '-qm', 'Resolve conflict')
        patch_path = self.root / 'resolution.patch'
        git(clone, 'diff', '--binary', '--full-index', '--no-ext-diff', '--no-textconv',
            '--no-renames', '--output=' + str(patch_path), record['merge_head'], 'HEAD', '--')
        command = command or self.command
        return replace(command, command_id='integrate-2', attempt=2,
                       payload={**command.payload, 'integration_resolution': {
                           'merge_ref': outcome.outputs['integration_merge_ref'],
                           'coder_patch': {'path': 'resolution.patch'},
                           'coder_execution_id': 'repair-coder'}})

    def assert_original_deltas(self):
        self.assertEqual('migrated feature\n', (self.worktree / 'feature.txt').read_text())
        self.assertEqual({'cases': ['retained-target-case']}, json.loads(
            (self.worktree / '.modport/target-contract.json').read_text()))

    def test_advanced_committed_and_dirty_user_candidate_keeps_all_deltas(self):
        (self.worktree / 'user.txt').write_text('committed user work\n')
        git(self.worktree, 'add', 'user.txt')
        git(self.worktree, 'commit', '-qm', 'User committed work')
        (self.worktree / 'dirty-user.txt').write_text('unfinished user work\n')
        result = self.integrate()
        self.assertEqual('completed', result.status, result.detail)
        self.assertEqual('committed user work\n', (self.worktree / 'user.txt').read_text())
        self.assertEqual('unfinished user work\n', (self.worktree / 'dirty-user.txt').read_text())
        self.assertEqual('migrated shared\n', (self.worktree / 'shared.txt').read_text())
        self.assert_original_deltas()

    def test_real_conflict_materializes_isolated_snapshot_and_preserves_candidate(self):
        outcome, record = self.conflict()
        self.assertEqual('user shared\n', (self.worktree / 'shared.txt').read_text())
        self.assertEqual('original feature\n', (self.worktree / 'feature.txt').read_text())
        self.assertEqual('', git(self.worktree, 'status', '--porcelain'))
        self.assertNotEqual('worktree', record['workspace'])
        self.assertIn('<<<<<<<', (self.root / record['workspace'] / 'shared.txt').read_text())
        self.assertEqual('migrated feature\n', (self.root / record['workspace'] / 'feature.txt').read_text())
        self.assertEqual({'cases': ['retained-target-case']}, json.loads(
            (self.root / record['workspace'] / '.modport/target-contract.json').read_text()))
        self.assertEqual({'shared', 'feature', 'contract'}, set(record['original_task_ids']))
        self.assertNotIn('sha256', outcome.outputs['integration_merge_ref'])

    def test_returned_coder_correction_publishes_full_original_merge(self):
        outcome, record = self.conflict()
        result = self.integrate(self.resolution(outcome, record))
        self.assertEqual('completed', result.status, result.detail)
        self.assertEqual('user shared and migrated shared\n', (self.worktree / 'shared.txt').read_text())
        self.assert_original_deltas()

    def test_returned_resolution_reconflicts_with_newer_user_edit_without_losing_it(self):
        outcome, record = self.conflict()
        command = self.resolution(outcome, record)
        (self.worktree / 'shared.txt').write_text('newer user decision\n')
        (self.worktree / 'new-user.txt').write_text('new user file\n')
        result = self.integrate(command)
        self.assertEqual('integration_merge_required', result.error_code, result.detail)
        self.assertEqual('newer user decision\n', (self.worktree / 'shared.txt').read_text())
        self.assertEqual('new user file\n', (self.worktree / 'new-user.txt').read_text())
        newest = json.loads((self.root / result.outputs['integration_merge_ref']['path']).read_text())
        self.assertEqual('migrated feature\n', (self.root / newest['workspace'] / 'feature.txt').read_text())

    def test_already_applied_original_patches_are_harmless(self):
        result = self.integrate()
        self.assertEqual('completed', result.status, result.detail)
        repeated = self.integrate(replace(self.command, command_id='integrate-repeat', attempt=2))
        self.assertEqual('completed', repeated.status, repeated.detail)
        self.assertEqual(result.outputs['head'], repeated.outputs['head'])
        self.assert_original_deltas()

    def test_merge_clone_never_follows_symlink_outside_run(self):
        outside = self.root.parent / 'outside'
        outside.mkdir()
        (self.root / 'workspaces').mkdir()
        (self.root / 'workspaces/integration-merges').symlink_to(outside, target_is_directory=True)
        result = self.integrate()
        self.assertEqual('failed', result.status, result.detail)
        self.assertEqual([], list(outside.iterdir()))
        self.assertEqual('original shared\n', (self.worktree / 'shared.txt').read_text())

    def test_preparation_wrapper_propagates_actual_conflict_and_consumes_resolution(self):
        from modport.preparation_execution import PreparationIntegrateHandler
        (self.worktree / 'shared.txt').write_text('user shared\n')
        git(self.worktree, 'add', 'shared.txt')
        git(self.worktree, 'commit', '-qm', 'User edit')
        command = replace(self.command, task_id='development_prepare_integrate',
                          stage_id='development_prepare_integrate', command_id='prepare-integrate-1',
                          payload={**self.command.payload, 'development_kind': 'preparation'})
        outcome = PreparationIntegrateHandler()(command)
        self.assertEqual('integration_merge_required', outcome.error_code, outcome.detail)
        record = json.loads((self.root / outcome.outputs['integration_merge_ref']['path']).read_text())
        original = json.loads((self.root / record['command_ref']['path']).read_text())
        self.assertEqual(command.stage_id, original['stage_id'])
        self.assertEqual('worktree', original['options']['workspace'])
        repaired = self.resolution(outcome, record, command=command)
        result = PreparationIntegrateHandler()(repaired)
        self.assertEqual('completed', result.status, result.detail)
        self.assert_original_deltas()

    def test_modify_delete_conflict_requires_coder_even_without_text_markers(self):
        author = self.root / 'author-shared'
        git(author, 'rm', 'shared.txt')
        git(author, 'commit', '-qm', 'Delete migrated file')
        git(author, 'diff', '--binary', '--full-index', '--no-ext-diff', '--no-textconv',
            '--no-renames', '--output=' + str(self.root / 'shared.patch'), self.base, 'HEAD', '--')
        (self.worktree / 'shared.txt').write_text('user shared\n')
        git(self.worktree, 'add', 'shared.txt')
        git(self.worktree, 'commit', '-qm', 'User edit')
        outcome = self.integrate()
        self.assertEqual('integration_merge_required', outcome.error_code, outcome.detail)
        self.assertEqual('user shared\n', (self.worktree / 'shared.txt').read_text())

    def test_binary_conflict_requires_coder_without_text_markers(self):
        author = self.root / 'author-shared'
        (author / 'shared.txt').write_bytes(b'\0coder binary\0')
        git(author, 'add', 'shared.txt')
        git(author, 'commit', '-qm', 'Author binary')
        git(author, 'diff', '--binary', '--full-index', '--no-ext-diff', '--no-textconv',
            '--no-renames', '--output=' + str(self.root / 'shared.patch'), self.base, 'HEAD', '--')
        (self.worktree / 'shared.txt').write_bytes(b'\0user binary\0')
        git(self.worktree, 'add', 'shared.txt')
        git(self.worktree, 'commit', '-qm', 'User binary')
        outcome = self.integrate()
        self.assertEqual('integration_merge_required', outcome.error_code, outcome.detail)
        self.assertEqual(b'\0user binary\0', (self.worktree / 'shared.txt').read_bytes())

    def test_supplied_invalid_patch_binding_is_explicit_failure(self):
        self.results[0]['outputs']['artifact_refs']['coder_patch']['metadata']['task_id'] = 'another-task'
        command = replace(self.command, payload={**self.command.payload, 'development_results': self.results})
        outcome = self.integrate(command)
        self.assertEqual('failed', outcome.status, outcome.detail)
        self.assertIn('task/generation mismatch', outcome.detail)
        self.assertEqual('original feature\n', (self.worktree / 'feature.txt').read_text())

    def test_apply_preserves_unrelated_staged_reports_and_credentials(self):
        from modport.development import _apply
        report = self.worktree / '.modport/goal-reports/fixture.md'
        report.parent.mkdir()
        report.write_text('fixture report\n')
        (self.worktree / 'token.json').write_text('{"fixture": "not-a-real-credential"}\n')
        git(self.worktree, 'add', '--', '.modport/goal-reports/fixture.md', 'token.json')
        _apply(self.command, self.worktree, self.root / 'feature.patch', self.plan['tasks'][1])
        committed = git(self.worktree, 'ls-tree', '-r', '--name-only', 'HEAD').splitlines()
        self.assertNotIn('token.json', committed)
        self.assertNotIn('.modport/goal-reports/fixture.md', committed)
        staged = git(self.worktree, 'diff', '--cached', '--name-only').splitlines()
        self.assertEqual(['.modport/goal-reports/fixture.md', 'token.json'], staged)
        self.assertEqual('migrated feature\n', (self.worktree / 'feature.txt').read_text())

    def test_actual_conflict_scheduler_coder_export_and_original_integration_handoff(self):
        from types import SimpleNamespace
        from unittest.mock import patch
        from dispatcher_sdk.execution_kernel import (
            BudgetEnvelope, ClockCheckpoint, DeadlineConstraint, ExecutionCommandV2,
        )
        from modport import integration_repair
        from modport.development import CoderHandler
        from modport.execution_budget import current_deadline_budget, execution_budget
        from modport.handlers import _result
        from modport.operations import MigrationOperations
        from modport.payload_storage import unpack_input
        from modport.workflow import WorkflowDefinition

        outcome, record = self.conflict()
        deadline = self.command.options['deadline_epoch']
        owner = MigrationOperations(clock=time.time)
        request = {'workflow_mode': 'migration', 'budget': {
            'max_agent_assignments': 5, 'max_tokens': None,
            'max_rework_rounds': 0, 'execution_max_attempts': 1}}
        header = {'run_id': 'run', 'run_dir': str(self.root), 'request': request,
                  'definition': WorkflowDefinition(request).to_dict(),
                  'deadline_epoch': deadline, 'registry_revision': 'current-handoff-fixture',
                  'rubric_sha256': 'host-owned-provenance', 'prior_findings': [], 'initial_refs': {}}
        app = owner._new_application()
        original_sdk = {'execution_id': self.command.command_id, 'payload': self.command.to_dict()}
        snapshot = {'run_id': 'run', 'tasks': {self.command.task_id: {'attempts': [{
            'state': 'succeeded', 'command': original_sdk,
            'result': {'value': outcome.to_dict()}}]}}}
        scheduled = integration_repair.start_or_resume(owner, snapshot, header, app,
                                                       self.command, outcome)
        dispatch = next(op for op in scheduled if op['kind'] == 'add_task')
        sdk_command = ExecutionCommandV2.from_dict(dispatch['command'])
        coder = OperationInput.from_dict(unpack_input(self.root, sdk_command.payload))
        self.assertEqual(record['workspace'], coder.payload['development_source_workspace'])
        self.assertEqual(record['merge_head'], coder.payload['development_base'])
        self.assertEqual(deadline, coder.options['deadline_epoch'])
        self.assertEqual(1, app['agent_assignments'])

        class Context:
            def __init__(self):
                self.command = sdk_command
                self.lease = SimpleNamespace(expires_at=deadline)
                self.envelope = BudgetEnvelope((DeadlineConstraint(
                    'sdk-execution', 'execution', deadline, 0),), self.sample())

            @staticmethod
            def sample():
                return ClockCheckpoint(time.time(), time.monotonic(), 'integration-handoff', 'boot')

            @property
            def budget(self):
                self.envelope = self.envelope.recheckpoint(sample=self.sample())
                return self.envelope.view(sample=self.envelope.checkpoint)

        observations = []

        def scripted_model(handler, operation):
            observations.append(operation.command_id)
            self.assertEqual(coder.command_id, operation.command_id)
            self.assertIn('actual integration conflicts', handler.prompt)
            self.assertIn('shared.patch', handler.native_goal['objective'])
            self.assertIn('shared.txt', handler.native_goal['objective'])
            budget = current_deadline_budget(operation)
            self.assertIsNotNone(budget)
            self.assertLessEqual(budget.effective_deadline, deadline)
            workspace = self.root / operation.options['workspace']
            self.assertNotEqual(workspace, self.worktree)
            self.assertIn('<<<<<<<', (workspace / 'shared.txt').read_text())
            self.assertEqual('migrated feature\n', (workspace / 'feature.txt').read_text())
            self.assertEqual({'cases': ['retained-target-case']}, json.loads(
                (workspace / '.modport/target-contract.json').read_text()))
            (workspace / 'shared.txt').write_text('user shared and migrated shared\n')
            verdict = handler.goal_validator()
            return _result(operation, 'completed', outputs={'native_goal': {
                'host_accepted': verdict['accepted'], 'producer_stopped': True}})

        with execution_budget(Context()), patch('modport.handlers.CodexStageHandler.__call__', scripted_model):
            coder_result = CoderHandler()(coder)
        self.assertEqual('completed', coder_result.status, coder_result.detail)
        self.assertEqual([coder.command_id], observations)
        self.assertIn('coder_patch', coder_result.outputs['artifact_refs'])
        self.assertEqual(['shared.txt'], coder_result.outputs['paths'])
        self.assertEqual('user shared\n', (self.worktree / 'shared.txt').read_text())
        snapshot['tasks'][coder.task_id] = {'attempts': [{
            'state': 'succeeded',
            'command': {**dispatch['command'], 'payload': coder.to_dict()},
            'result': {'value': coder_result.to_dict()}}]}
        scheduled = integration_repair.start_or_resume(owner, snapshot, header, app)
        dispatched = next(op for op in scheduled if op['kind'] == 'new_attempt')
        integration = OperationInput.from_dict(unpack_input(self.root, dispatched['command']['payload']))
        self.assertEqual(coder.command_id, integration.payload['integration_resolution']['coder_execution_id'])
        self.assertEqual(deadline, integration.options['deadline_epoch'])
        self.assertEqual(1, app['agent_assignments'])
        result = self.integrate(integration)
        self.assertEqual('completed', result.status, result.detail)
        self.assertEqual('user shared and migrated shared\n', (self.worktree / 'shared.txt').read_text())
        self.assert_original_deltas()

    def _integration_continuation(self, *, inherited):
        from modport.continuation import prepare_application
        from modport.operations import MigrationOperations
        from modport.payload_storage import unpack_input
        from modport.workflow import WorkflowDefinition

        owner = MigrationOperations(clock=time.time)
        previous_segment = 'carried-segment'
        deadline = self.command.options['deadline_epoch']
        original = replace(self.command, command_id='original-segment:development_integrate:1',
                           options={**self.command.options, 'workflow_version': WORKFLOW_VERSION - 1},
                           prior_findings=({'detail': 'Retain the original stage-specific finding'},),
                           upstream_results={'original-producer': {'retained': 'upstream input'}})
        failed = OperationResult('failed', original.run_id, original.task_id, original.stage_id,
            original.command_id, detail='HEAD differs from development_base',
            error_code='development_integration_invalid').to_dict()
        tail = replace(original, task_id='target_contract_freeze', stage_id='target_contract_freeze',
                       command_id=previous_segment + ':target_contract_freeze:1', payload={})
        tail_failure = OperationResult('failed', tail.run_id, tail.task_id, tail.stage_id,
            tail.command_id, detail='Missing original target contract contribution',
            error_code='target_contract_invalid').to_dict()
        source_app = owner._new_application()
        source_app.update(agent_assignments=38, terminal_reason='user_cancelled', user_cancelled=True,
                          effective={'development_integrate': failed, 'target_contract_freeze': tail_failure},
                          history=[{'execution_id': original.command_id}, {'execution_id': tail.command_id}],
                          integration_repair={'phase': 'failed', 'old_diagnostic': True})
        request = {'workflow_mode': 'migration', 'budget': {
            'max_agent_assignments': 40, 'max_tokens': None,
            'max_rework_rounds': 0, 'execution_max_attempts': 1}}
        source_header = {'run_id': previous_segment, 'logical_run_id': 'run', 'run_dir': str(self.root),
                         'watchdog_policy': {'enabled': False}}
        tasks = {tail.task_id: {'attempts': [{'state': 'succeeded',
            'command': {'execution_id': tail.command_id, 'payload': tail.to_dict()},
            'result': {'value': tail_failure}}]}}
        if inherited:
            catalog = self.root / 'artifacts/continuations' / previous_segment / 'rework-sources.json'
            catalog.parent.mkdir(parents=True)
            catalog.write_text(json.dumps({'schema_version': 1, 'sources': [{
                'execution_id': original.command_id, 'task_id': original.task_id,
                'target_agent': original.task_id, 'stage_id': original.stage_id,
                'terminal_state': 'succeeded', 'operation': original.to_dict()}]}))
            source_header['continuation'] = {'support_refs': {'continuation:rework_sources': {
                'path': catalog.relative_to(self.root).as_posix(),
                'metadata': {'next_run_id': previous_segment}}}}
        else:
            tasks[original.task_id] = {'attempts': [{'state': 'succeeded',
                'command': {'execution_id': original.command_id, 'payload': original.to_dict()},
                'result': {'value': failed}}]}
        state = {'run_id': previous_segment, 'state': 'cancelled', 'revision': 9,
                 'tasks': tasks, 'waits': {}, 'application_state': source_app, 'input': source_header}
        app = prepare_application(self.root, state, start_stage='development_integrate',
                                  target_workflow_version=WORKFLOW_VERSION)
        self.assertEqual(source_app['history'], app['history'])
        self.assertEqual({'phase': 'failed', 'old_diagnostic': True}, app['integration_repair_history'][-1])
        self.assertNotIn('integration_repair', app)
        descriptor = app['integration_replay']
        self.assertEqual(original.to_dict(), json.loads(
            (self.root / descriptor['command_ref']['path']).read_text()))
        successor = {'run_id': 'successor', 'logical_run_id': 'run', 'run_dir': str(self.root),
                     'request': request, 'definition': WorkflowDefinition(request).to_dict(),
                     'deadline_epoch': deadline, 'registry_revision': 'current-replay-fixture',
                     'rubric_sha256': 'host-owned-provenance', 'prior_findings': [], 'initial_refs': {},
                     'watchdog_policy': {'enabled': False}}
        snapshot = {'run_id': 'successor', 'tasks': {}, 'waits': {}}
        scheduled = owner._advance_without_business_gates(snapshot, successor, app)
        self.assertEqual(['add_task', 'dispatch'], [operation['kind'] for operation in scheduled])
        dispatch = scheduled[0]
        replay = OperationInput.from_dict(unpack_input(self.root, dispatch['command']['payload']))
        self.assertEqual('development_integrate', replay.stage_id)
        self.assertNotEqual(original.command_id, replay.command_id)
        self.assertEqual('run', replay.run_id)
        self.assertEqual(WORKFLOW_VERSION, replay.options['workflow_version'])
        self.assertEqual(deadline, replay.options['deadline_epoch'])
        self.assertEqual(38, app['agent_assignments'])
        self.assertFalse(successor['watchdog_policy']['enabled'])
        self.assertEqual(self.results, replay.payload['development_results'])
        self.assertEqual(original.upstream_results['original-producer'], replay.upstream_results['original-producer'])
        self.assertEqual(self.plan_ref, replay.artifact_refs['development_plan'])
        self.assertEqual(original.prior_findings, replay.prior_findings)
        self.assertNotIn('integration_replay', app)
        self.assertNotIn('flowthrough_resume', app)
        from modport.development import _base
        self.assertEqual((self.base, 1), _base(replay))
        return owner, successor, app, snapshot, dispatch, replay

    def test_explicit_continuation_replays_inherited_integration_and_restores_all_original_deltas(self):
        (self.worktree / 'user.txt').write_text('new committed user contribution\n')
        git(self.worktree, 'add', 'user.txt')
        git(self.worktree, 'commit', '-qm', 'Later user work')
        (self.worktree / 'dirty-user.txt').write_text('new dirty user contribution\n')
        owner, header, app, snapshot, dispatch, replay = self._integration_continuation(inherited=True)
        outcome = self.integrate(replay)
        self.assertEqual('completed', outcome.status, outcome.detail)
        self.assertEqual('migrated shared\n', (self.worktree / 'shared.txt').read_text())
        self.assert_original_deltas()
        self.assertEqual('new committed user contribution\n', (self.worktree / 'user.txt').read_text())
        self.assertEqual('new dirty user contribution\n', (self.worktree / 'dirty-user.txt').read_text())
        snapshot['tasks'][replay.task_id] = {'attempts': [{'state': 'succeeded',
            'command': dispatch['command'], 'result': {'value': outcome.to_dict()}}]}
        scheduled = owner._advance_without_business_gates(snapshot, header, app)
        from modport.payload_storage import unpack_input
        cleanup = OperationInput.from_dict(unpack_input(self.root, scheduled[0]['command']['payload']))
        self.assertEqual('code_cleanup', cleanup.stage_id)

    def test_explicit_continuation_replays_local_sdk_command_and_preserves_raw_failure(self):
        owner, header, app, snapshot, dispatch, replay = self._integration_continuation(inherited=False)
        # The consumer must see the original supplied reference and report its
        # concrete error rather than treating an invalid patch as an empty merge.
        (self.root / 'shared.patch').unlink()
        outcome = self.integrate(replay)
        self.assertEqual('failed', outcome.status, outcome.detail)
        snapshot['tasks'][replay.task_id] = {'attempts': [{'state': 'succeeded',
            'command': dispatch['command'], 'result': {'value': outcome.to_dict()}}]}
        scheduled = owner._advance_without_business_gates(snapshot, header, app)
        self.assertEqual(['finish'], [operation['kind'] for operation in scheduled])
        self.assertEqual('failed', scheduled[0]['state'])
        self.assertEqual(outcome.error_code, app['terminal_reason'])
        self.assertEqual('original feature\n', (self.worktree / 'feature.txt').read_text())


if __name__ == '__main__':
    unittest.main()
