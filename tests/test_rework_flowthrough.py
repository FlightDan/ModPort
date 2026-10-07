"""Late coder handoffs remain useful even when their review has finished."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from modport.contracts import OperationInput, OperationResult
from modport.rework_orchestration import ReviewReworkOrchestration
from modport.evidence import atomic_json


class Host(ReviewReworkOrchestration):
    clock = staticmethod(lambda: 1)

    def _schedule(self, snapshot, header, app, stage, **kwargs):
        self.scheduled = stage
        return [{'kind': 'dispatch', 'task_id': kwargs['task_id']}]


class ReworkFlowthroughTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.header = {'run_dir': self.temp.name, 'definition': {'workflow_version': 17}}
        self.command = OperationInput('run', 'child', 'agent_rework', 'child-exec', self.temp.name)
        self.attempt = {'state': 'running', 'command': {
            'execution_id': self.command.command_id, 'payload': self.command.to_dict()}}
        self.snapshot = {'tasks': {'child': {'attempts': [self.attempt]}}}
        self.record = {'state': 'running', 'task_id': 'child',
            'reviewer_execution_id': 'closed-review', 'target_stage': 'coder',
            'target_agent': 'coder-a', 'reviewer_stage': 'code_review',
            'request_id': 'request', 'updates': []}
        self.app = {'review_rework': {'requests': {'request': self.record},
            'latest_targets': {}, 'sequence': 1}, 'cancel_sent': [],
            'processed': [], 'history': [], 'effective': {}}
        self.host = Host()

    def test_v17_finished_reviewer_does_not_cancel_running_author(self):
        operations = self.host._review_rework_decision(self.snapshot, self.header, self.app)
        self.assertEqual([], operations)
        self.assertTrue(self.record['caller_closed'])
        self.assertEqual([], self.app['cancel_sent'])
        self.assertEqual('running', self.record['state'])

    def test_legacy_finished_reviewer_retains_cancellation(self):
        self.header['definition']['workflow_version'] = 16
        operations = self.host._review_rework_decision(self.snapshot, self.header, self.app)
        self.assertEqual('cancel', operations[0]['kind'])

    def test_expired_tool_cancels_running_followup_verification(self):
        self.record['followup_stage'] = 'target_build'
        directory = Path(self.temp.name) / 'artifacts/rework-tools/closed-review/requests'
        atomic_json(directory / 'request.cancel.json', {'reason': 'tool wait expired'})
        operations = self.host._review_rework_decision(self.snapshot, self.header, self.app)
        self.assertEqual([{'kind': 'cancel', 'task_id': 'child',
                           'reason': 'reviewer tool call closed'}], operations)
        self.assertEqual(['child-exec'], self.app['cancel_sent'])

    def test_expired_tool_preserves_finished_author_without_new_verification(self):
        directory = Path(self.temp.name) / 'artifacts/rework-tools/closed-review/requests'
        atomic_json(directory / 'request.cancel.json', {'reason': 'tool wait expired'})
        result = OperationResult('completed', 'run', 'child', 'agent_rework', 'child-exec',
            outputs={'after_head': 'b' * 40})
        self.attempt.update(state='succeeded', result={'value': result.to_dict()})
        operations = self.host._review_rework_decision(self.snapshot, self.header, self.app)
        self.assertEqual([], operations)
        self.assertFalse(hasattr(self.host, 'scheduled'))
        self.assertEqual('failed', self.record['state'])
        self.assertEqual(result.to_dict(), self.record['updates'][0]['result'])
        self.assertIn('expired', self.record['error'])

    def test_failed_coder_still_builds_candidate_and_preserves_failure(self):
        result = OperationResult('failed', 'run', 'child', 'agent_rework', 'child-exec',
            outputs={'after_head': 'b' * 40, 'integration_status': 'integrated'},
            detail='native coder blocked after writing useful changes', error_code='agent_failed')
        self.attempt.update(state='succeeded', result={'value': result.to_dict()})
        operations = self.host._review_rework_decision(self.snapshot, self.header, self.app)
        self.assertEqual('target_build', self.host.scheduled)
        self.assertEqual('dispatch', operations[0]['kind'])
        self.assertEqual('failed', self.record['updates'][0]['result']['status'])
        self.assertEqual('failed', self.app['effective']['coder-a']['status'])
        self.assertNotIn('stop_reason', self.app)

    def test_expired_rework_budget_keeps_failure_without_starting_verification(self):
        self.header['definition']['workflow_version'] = 25
        self.record.update(target_stage='contract_draft', target_agent='contract_restore',
            target_scope='contract', reviewer_stage='contract_review', queue_deadline_epoch=0)
        result = OperationResult('failed', 'run', 'child', 'agent_rework', 'child-exec',
            detail='run wall-clock budget exhausted', error_code='budget_exhausted')
        self.attempt.update(state='succeeded', result={'value': result.to_dict()})
        operations = self.host._review_rework_decision(self.snapshot, self.header, self.app)
        self.assertEqual([], operations)
        self.assertFalse(hasattr(self.host, 'scheduled'))
        self.assertEqual('failed', self.record['state'])
        self.assertEqual('budget_exhausted', self.record['updates'][0]['result']['error_code'])
        self.assertEqual('failed', self.app['effective']['contract_restore']['status'])
        self.assertNotIn('stop_reason', self.app)

    def test_expired_rework_budget_preserves_candidate_without_followup(self):
        self.header['definition']['workflow_version'] = 25
        self.record.update(queue_deadline_epoch=0)
        result = OperationResult('failed', 'run', 'child', 'agent_rework', 'child-exec',
            outputs={'after_head': 'b' * 40, 'integration_status': 'integrated'},
            detail='run wall-clock budget exhausted', error_code='budget_exhausted')
        self.attempt.update(state='succeeded', result={'value': result.to_dict()})
        self.assertEqual([], self.host._review_rework_decision(self.snapshot, self.header, self.app))
        self.assertFalse(hasattr(self.host, 'scheduled'))
        self.assertEqual('b' * 40, self.app['effective']['coder-a']['outputs']['after_head'])
        self.assertEqual('failed', self.record['state'])
        self.assertNotIn('stop_reason', self.app)

    def test_published_request_is_dispatched_once_after_reviewer_final(self):
        class LateHost(Host):
            clock = staticmethod(lambda: 0)

            def _memory_capacity(self, *args, **kwargs):
                return True

            def _refs(self, *args):
                return {}

            def _charge_rework(self, header, app, family):
                app['rounds'][family] = app['rounds'].get(family, 0) + 1

            def _schedule(self, snapshot, header, app, stage, **kwargs):
                return [{'kind': 'add_task', 'task_id': kwargs['task_id']}]

        target = {'target_agent': 'coder-a', 'stage': 'coder', 'execution_id': 'original'}
        original = OperationInput('run', 'coder-a', 'coder', 'original', self.temp.name)
        reviewer = OperationInput('run', 'review', 'code_review', 'closed-review', self.temp.name,
            options={'workflow_version': 17}, payload={'review_rework_targets': [target]})
        self.snapshot['tasks'] = {command.task_id: {'attempts': [{'state': 'succeeded',
            'command': {'execution_id': command.command_id, 'payload': command.to_dict()}}]}
            for command in (original, reviewer)}
        directory = Path(self.temp.name) / 'artifacts/rework-tools/closed-review'
        atomic_json(directory / 'session.json', {'run_id': 'run',
            'reviewer_execution_id': 'closed-review', 'workspace': 'worktree', 'deadline_epoch': 100})
        atomic_json(directory / 'requests/late-request.json', {'run_id': 'run',
            'reviewer_execution_id': 'closed-review', 'request_id': 'late-request',
            'target_agent': 'coder-a', 'instructions': 'repair A'})
        self.app['review_rework']['requests'] = {}
        self.app.update(agent_assignments=1, rounds={})
        self.header['request'] = {'budget': {'max_agent_assignments': 10, 'max_rework_rounds': 3}}
        host = LateHost()
        with patch('modport.rework_orchestration.rework_targets', return_value=[target]):
            first = host._review_rework_decision(self.snapshot, self.header, self.app)
            second = host._review_rework_decision(self.snapshot, self.header, self.app)
        self.assertEqual('add_task', first[0]['kind'])
        self.assertEqual([], second)
        self.assertEqual(1, self.app['rounds']['review_rework:coder-a'])


if __name__ == '__main__':
    unittest.main()
