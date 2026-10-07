"""Stateful progress routing boundaries; actual SDK dispatch has its own probe."""
from copy import deepcopy
from pathlib import Path
import tempfile
import time
import unittest

from modport.contracts import OperationInput, OperationResult
from modport.models import Budget, MigrationRequest
from modport.operations import MigrationOperations
from modport.workflow import compile_migration_workflow, WORKFLOW_VERSION


class ProgressRoutingTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        (self.root / 'baseline/.modport/tests').mkdir(parents=True)
        self.source = self.root / 'baseline/.modport/tests/Test.java'
        self.source.write_text('class Test {}')
        self.now = time.time()
        self.owner = MigrationOperations(clock=lambda: self.now)
        request = MigrationRequest('probe', 'https://example.invalid/probe', '1.20.1', '1.21.1',
            workflow_mode='artifact_verification', budget=Budget(max_seconds=100000, max_agent_assignments=12))
        definition = compile_migration_workflow(request).to_dict()
        self.header = {'run_id': 'probe', 'run_dir': str(self.root), 'request': request.to_dict(),
            'definition': definition, 'registry_revision': 'host-registry', 'initial_refs': {},
            'prior_findings': [], 'rubric_sha256': 'host-provided', 'deadline_epoch': self.now + 100000}
        self.app = self.owner._new_application()
        self.snapshot = {'run_id': 'probe', 'state': 'running', 'tasks': {}, 'waits': {},
                         'application_state': self.app}

    def author(self, task_id, assignment=1, stage='contract_draft', payload=None):
        command = OperationInput('probe', task_id, stage, 'probe:' + task_id + ':1', str(self.root),
            payload=payload or {}, options={'workflow_version': WORKFLOW_VERSION,
                'agent_assignment': assignment, 'deadline_epoch': self.header['deadline_epoch'],
                'progress_supervision_policy': self.header['definition']['progress_supervision_policy']})
        self.snapshot['tasks'][task_id] = {'attempts': [{'state': 'running',
            'command': {'execution_id': command.command_id, 'payload': command.to_dict()}}]}
        self.app['agent_assignments'] = max(self.app['agent_assignments'], assignment)
        return command

    def observe(self, advance=600):
        self.now += advance
        return self.owner._progress_supervision_decision(self.snapshot, self.header, self.app)

    def test_parent_waiting_on_planned_resource_child_is_still_supervised(self):
        parent = self.author('review', stage='contract_review')
        self.app['review_rework'] = {'requests': {'request': {
            'state': 'running', 'waiting_resources': True, 'reviewer_execution_id': parent.command_id,
            'task_id': 'child'}}}
        self.snapshot['tasks']['child'] = {'attempts': [{'state': 'planned', 'command': {'execution_id': 'child'}}]}
        self.observe(0)
        self.observe()
        self.observe()
        actions = self.observe()
        scheduled = next(row for row in actions if row['kind'] == 'add_task')
        packet = scheduled['command']['payload']['payload']['progress_supervision']
        self.assertEqual(parent.command_id, packet['target_execution_id'])
        self.assertEqual('planned', packet['observation']['rework_waits'][0]['child_state'])

    def test_two_idle_executions_get_independent_reviews_and_do_not_duplicate(self):
        self.author('one', assignment=1)
        self.author('two', assignment=2)
        self.observe(0)
        self.observe()
        self.observe()
        actions = self.observe()
        scheduled = [row for row in actions if row['kind'] == 'add_task']
        self.assertEqual(2, len(scheduled))
        self.assertEqual(2, len(self.app['progress_supervision']['reviews']))
        self.assertEqual([], self.observe())

    def test_active_rework_leaf_suppresses_duplicate_parent_review(self):
        parent = self.author('review', assignment=1, stage='contract_review')
        child = self.author('child', assignment=2, payload={'reviewer_rework': {
            'reviewer_execution_id': parent.command_id}})
        self.observe(0)
        self.observe()
        self.observe()
        actions = self.observe()
        self.assertEqual(1, sum(row['kind'] == 'add_task' for row in actions))
        self.assertEqual(child.command_id, next(iter(self.app['progress_supervision']['reviews'].values()))['target_execution_id'])

    def test_failed_sdk_state_commit_keeps_previous_observation_for_retry(self):
        author = self.author('author')
        self.observe(0)
        committed = deepcopy(self.app)
        self.source.write_text('class Test { int changed = 1; }')
        self.observe()
        self.assertTrue(self.app['progress_supervision']['executions'][author.command_id]['last_observation']['useful_progress'])
        # Re-evaluate from the last SDK commit after a rejected CAS, while the
        # uncommitted sidecar remains on disk. Its other slot must be intact.
        self.app = committed
        self.observe(0)
        self.assertTrue(self.app['progress_supervision']['executions'][author.command_id]['last_observation']['useful_progress'])

    def test_invalid_supervisor_response_never_authorizes_cancel(self):
        self.author('author')
        self.observe(0)
        self.observe()
        self.observe()
        actions = self.observe()
        added = next(row for row in actions if row['kind'] == 'add_task')
        command = OperationInput.from_dict(added['command']['payload'])
        result = OperationResult('completed', command.run_id, command.task_id,
            command.stage_id, command.command_id,
            outputs={'progress_supervisor_decision': {'decision': 'terminate'}})
        self.snapshot['tasks'][added['task_id']] = {'attempts': [{'state': 'succeeded',
            'command': added['command'], 'result': {'value': result.to_dict()}}]}
        self.assertFalse(any(row['kind'] == 'cancel' for row in self.observe(0)))
        self.assertEqual('invalid', next(iter(self.app['progress_supervision']['reviews'].values()))['status'])


if __name__ == '__main__':
    unittest.main()
