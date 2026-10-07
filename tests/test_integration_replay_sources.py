"""A retained child correction cannot replace its complete author at a join."""

from copy import deepcopy
from dataclasses import replace
import json
from pathlib import Path
import subprocess
import tempfile
import time
import unittest

from modport.continuation import prepare_application
from modport.contracts import OperationInput, OperationResult
from modport.development import DevelopmentIntegrateHandler, validate_plan
from modport.memory_admission import MemorySnapshot
from modport.models import Budget, MigrationRequest
from modport.operations import MigrationOperations
from modport.payload_storage import unpack_input
from modport.workflow import WORKFLOW_VERSION, WorkflowDefinition


def git(workspace, *arguments):
    process = subprocess.run(['git', '-c', 'core.hooksPath=/dev/null',
        '-c', 'user.name=Fixture', '-c', 'user.email=fixture@localhost', *arguments],
        cwd=workspace, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, check=True)
    return process.stdout.strip()


class IntegrationReplaySourceTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.worktree = self.root / 'worktree'
        self.worktree.mkdir()
        git(self.worktree, 'init', '-q')
        (self.worktree / 'feature.txt').write_text('source feature\n')
        (self.worktree / 'client.txt').write_text('source client\n')
        git(self.worktree, 'add', '.')
        git(self.worktree, 'commit', '-qm', 'Original source')
        self.base = git(self.worktree, 'rev-parse', 'HEAD')
        self.plan = validate_plan({'schema_version': 1, 'base_commit': self.base,
            'shared_paths': [], 'tasks': [{'id': 'feature', 'objective': 'Migrate feature and contract',
                'owned_paths': ['feature.txt', 'client.txt', '.modport/functional-contract.json'],
                'dependencies': []}]}, workflow_version=WORKFLOW_VERSION)
        self.task = self.plan['tasks'][0]
        (self.root / 'plan.json').write_text(json.dumps(self.plan))
        self.plan_ref = {'path': 'plan.json', 'metadata': {'development_base': self.base}}
        author = self.root / 'author'
        git(self.root, 'clone', '-q', '--no-hardlinks', '--', str(self.worktree), str(author))
        (author / 'feature.txt').write_text('migrated feature\n')
        (author / '.modport').mkdir()
        (author / '.modport/functional-contract.json').write_text('{"cases": ["retained"]}\n')
        git(author, 'add', '.')
        git(author, 'commit', '-qm', 'Complete author contribution')
        git(author, 'diff', '--binary', '--full-index', '--no-ext-diff', '--no-textconv',
            '--no-renames', '--output=' + str(self.root / 'author.patch'), self.base, 'HEAD', '--')
        full_patch = {'path': 'author.patch', 'metadata': {'task_id': 'feature',
            'generation': 1, 'base': self.base,
            'paths': ['feature.txt', '.modport/functional-contract.json']}}
        self.author = OperationInput('logical', 'coder.g1.feature', 'coder', 'author:1', str(self.root),
            payload={'development_task': self.task, 'development_base': self.base,
                     'development_generation': 1}, artifact_refs={'development_plan': self.plan_ref})
        self.full_result = OperationResult('completed', self.author.run_id, self.author.task_id,
            self.author.stage_id, self.author.command_id, outputs={'development_task_id': 'feature',
                'artifact_refs': {'coder_patch': full_patch}}).to_dict()
        # Reproduce the old publication error: the child repairs only the
        # shared client's file, before the complete author joins the product.
        (self.worktree / 'client.txt').write_text('later client correction\n')
        git(self.worktree, 'add', 'client.txt')
        git(self.worktree, 'commit', '-qm', 'Narrow child correction')
        git(self.worktree, 'diff', '--binary', '--full-index', '--no-ext-diff', '--no-textconv',
            '--no-renames', '--output=' + str(self.root / 'child.patch'), self.base, 'HEAD', '--')
        narrow_patch = {'path': 'child.patch', 'metadata': {'task_id': 'feature',
            'generation': 2, 'base': self.base, 'paths': ['client.txt']}}
        self.child = OperationInput('logical', 'agent-rework.child', 'agent_rework', 'child:1',
            str(self.root), payload={'rework_original_command': self.author.to_dict(),
                'reviewer_rework': {'source_execution_id': self.author.command_id,
                                   'target_agent': self.author.task_id}},
            upstream_results={self.author.task_id: self.full_result})
        self.narrow_result = OperationResult('completed', self.child.run_id, self.child.task_id,
            self.child.stage_id, self.child.command_id, outputs={'development_task_id': 'feature',
                'target_execution_id': self.author.command_id,
                'artifact_refs': {'coder_patch': narrow_patch}}).to_dict()
        self.integration = OperationInput('logical', 'development_integrate', 'development_integrate',
            'previous:development_integrate:1', str(self.root),
            payload={'development_base': self.base, 'development_generation': 1,
                     'development_results': [self.narrow_result],
                     'execution_development_plan': self.plan, 'goal_scope': 'migration'},
            options={'workspace': 'worktree', 'workflow_version': WORKFLOW_VERSION},
            artifact_refs={'development_plan': self.plan_ref})
        failed = OperationResult('failed', self.integration.run_id, self.integration.task_id,
            self.integration.stage_id, self.integration.command_id,
            error_code='integration_conflict', detail='integration HEAD differs from development_base')
        self.freeze = replace(self.integration, task_id='target_contract_freeze',
            stage_id='target_contract_freeze', command_id='previous:target_contract_freeze:1', payload={})
        missing = OperationResult('failed', self.freeze.run_id, self.freeze.task_id,
            self.freeze.stage_id, self.freeze.command_id, error_code='target_contract_invalid',
            detail='worktree/.modport/functional-contract.json is missing')
        request = MigrationRequest('fixture', 'https://example.invalid/fixture.git',
            '1.20.1', '26.1.2', budget=Budget(max_seconds=3600, max_agent_assignments=10)).to_dict()
        self.header = {'run_id': 'previous', 'logical_run_id': 'logical', 'run_dir': str(self.root),
            'request': request, 'definition': WorkflowDefinition(request).to_dict(),
            'deadline_epoch': time.time() + 3600, 'registry_revision': 'fixture',
            'rubric_sha256': 'host-provenance', 'initial_refs': {}, 'prior_findings': [],
            'watchdog_policy': {'enabled': False}}
        self.owner = MigrationOperations(isolation_mode='thread',
            memory_probe=lambda: MemorySnapshot(64 * 1024**3, 64 * 1024**3, 'fixture capacity'))
        app = self.owner._new_application()
        app.update(agent_assignments=3, terminal_reason='target_contract_invalid',
            effective={'development_integrate': failed.to_dict(),
                       'target_contract_freeze': missing.to_dict()},
            history=[{'execution_id': self.integration.command_id},
                     {'execution_id': self.freeze.command_id}])
        self.state = {'run_id': 'previous', 'state': 'failed', 'revision': 4, 'input': self.header,
            'application_state': app, 'waits': {}, 'tasks': {
                self.freeze.task_id: self.attempt(self.freeze, missing.to_dict()),
                self.integration.task_id: self.attempt(self.integration, failed.to_dict()),
                self.child.task_id: self.attempt(self.child, self.narrow_result)}}

    @staticmethod
    def attempt(command, result):
        return {'attempts': [{'state': 'succeeded',
            'command': {'execution_id': command.command_id, 'payload': command.to_dict()},
            'result': {'value': result}}]}

    def archive_producers(self):
        path = self.root / 'artifacts/continuations/previous/rework-sources.json'
        path.parent.mkdir(parents=True)
        sources = []
        for command in (self.integration, self.child):
            sources.append({'target_agent': command.task_id, 'task_id': command.task_id,
                'execution_id': command.command_id, 'stage_id': command.stage_id,
                'terminal_state': 'succeeded', 'operation': command.to_dict()})
            self.state['tasks'].pop(command.task_id)
        path.write_text(json.dumps({'schema_version': 1, 'sources': sources}))
        self.header['continuation'] = {'support_refs': {'continuation:rework_sources': {
            'path': path.relative_to(self.root).as_posix(), 'metadata': {'next_run_id': 'previous'}}}}

    def replay(self, *, start_stage='target_contract_freeze'):
        before = deepcopy(self.state)
        app = prepare_application(self.root, self.state, start_stage=start_stage,
                                  target_workflow_version=WORKFLOW_VERSION)
        successor = {**self.header, 'run_id': 'successor'}
        snapshot = {'run_id': 'successor', 'tasks': {}, 'waits': {}}
        operations = self.owner._advance_without_business_gates(snapshot, successor, app)
        self.assertEqual(before, self.state)
        addition = next(row for row in operations if row['kind'] == 'add_task')
        command = OperationInput.from_dict(unpack_input(self.root, addition['command']['payload']))
        self.assertEqual('development_integrate', command.stage_id)
        self.assertEqual([self.full_result], command.payload['development_results'])
        self.assertEqual([{'selected_execution_id': self.child.command_id,
                           'original_execution_id': self.author.command_id,
                           'development_task_id': 'feature'}],
                         command.payload['integration_replay_rebindings'])
        self.assertEqual(self.header['deadline_epoch'], command.options['deadline_epoch'])
        self.assertEqual(3, app['agent_assignments'])
        self.assertEqual([self.narrow_result], self.integration.payload['development_results'])
        return command, app, successor, snapshot, addition

    def assert_restored_candidate(self, *, start_stage='target_contract_freeze'):
        command, _, _, _, _ = self.replay(start_stage=start_stage)
        outcome = DevelopmentIntegrateHandler()(command)
        self.assertEqual('completed', outcome.status, outcome.detail)
        self.assertEqual('migrated feature\n', (self.worktree / 'feature.txt').read_text())
        self.assertEqual('later client correction\n', (self.worktree / 'client.txt').read_text())
        self.assertEqual({'cases': ['retained']}, json.loads(
            (self.worktree / '.modport/functional-contract.json').read_text()))

    def test_sdk_child_inputs_restore_full_contract_and_preserve_correction(self):
        self.assert_restored_candidate()

    def test_archived_child_inputs_restore_full_contract_and_preserve_correction(self):
        self.archive_producers()
        self.assert_restored_candidate()

    def test_default_continuation_restores_archived_full_contract_before_failed_consumer(self):
        self.archive_producers()
        self.assert_restored_candidate(start_stage=None)

    def test_actual_overlap_reaches_integration_repair_without_losing_main_correction(self):
        (self.worktree / 'feature.txt').write_text('later feature correction\n')
        git(self.worktree, 'add', 'feature.txt')
        git(self.worktree, 'commit', '-qm', 'Concurrent candidate correction')
        command, app, header, snapshot, addition = self.replay()
        outcome = DevelopmentIntegrateHandler()(command)
        self.assertEqual('integration_merge_required', outcome.error_code, outcome.detail)
        self.assertEqual('later feature correction\n', (self.worktree / 'feature.txt').read_text())
        snapshot['tasks'][command.task_id] = {'attempts': [{'state': 'succeeded',
            'command': addition['command'], 'result': {'value': outcome.to_dict()}}]}
        app['effective'][command.stage_id] = outcome.to_dict()
        operations = self.owner._flowthrough_schedule_successor(snapshot, header, app,
            command.stage_id, command.command_id)
        task = next(row for row in operations if row['kind'] == 'add_task')
        repair = OperationInput.from_dict(unpack_input(self.root, task['command']['payload']))
        self.assertEqual('coder', repair.stage_id)
        self.assertEqual(outcome.outputs['integration_merge_ref'], repair.payload['integration_merge_ref'])
        self.assertEqual(header['deadline_epoch'], repair.options['deadline_epoch'])

    def test_missing_original_result_retains_identity_failure_and_predecessor(self):
        self.child = replace(self.child, upstream_results={})
        self.state['tasks'][self.child.task_id] = self.attempt(self.child, self.narrow_result)
        before = deepcopy(self.state)
        with self.assertRaisesRegex(ValueError, 'original author result is missing or conflicting'):
            self.replay()
        self.assertEqual(before, self.state)
        self.assertFalse((self.worktree / '.modport/functional-contract.json').exists())

    def test_mismatched_source_execution_does_not_restore_an_unrelated_author(self):
        self.child = replace(self.child, payload={**self.child.payload,
            'reviewer_rework': {'source_execution_id': 'unrelated-author:1',
                               'target_agent': self.author.task_id}})
        self.state['tasks'][self.child.task_id] = self.attempt(self.child, self.narrow_result)
        with self.assertRaisesRegex(ValueError, 'original author identity differs'):
            self.replay()


if __name__ == '__main__':
    unittest.main()
