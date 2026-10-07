"""Current progress policy reaches nested authors without adding a work budget."""
from dataclasses import replace
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from modport.contracts import OperationInput, OperationResult
from modport.evidence import atomic_json
from modport.memory_admission import MemorySnapshot
from modport.models import Budget, MigrationRequest
from modport.operations import MigrationOperations
from modport.payload_storage import unpack_input
from modport.progress_policy import progress_supervised
from modport.rework_mcp import PendingCall, ReworkServer, Session
from modport.rework_orchestration import project_rework_responses
from modport.rework_tools import interactive_review_timeout_cap, prepare_session
from modport.workflow import WORKFLOW_VERSION, WorkflowDefinition


class ProgressReworkDeadlineTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        (self.root / 'worktree').mkdir()
        request = MigrationRequest('progress', 'https://example.invalid/mod.git',
            '1.20.1', '26.1.2', budget=Budget(max_seconds=14400,
                max_agent_assignments=10, max_rework_rounds=1))
        self.header = {'run_dir': str(self.root), 'request': request.to_dict(),
            'definition': WorkflowDefinition(request.to_dict()).to_dict(),
            'deadline_epoch': time.time() + 14400,
            'initial_refs': {}, 'prior_findings': [],
            'registry_revision': 'a' * 64, 'rubric_sha256': 'b' * 64}
        self.assertTrue(progress_supervised(self.header),
                        'Tests require the current progress-supervised workflow.')
        self.host = MigrationOperations(memory_probe=lambda: MemorySnapshot(
            64 * 1024 ** 3, 64 * 1024 ** 3, 'current-policy'))
        self.app = self.host._new_application()
        self.snapshot = {'run_id': 'progress', 'state': 'running', 'tasks': {},
                         'waits': {}, 'application_state': self.app}
        options = {'workflow_version': WORKFLOW_VERSION,
            'progress_supervision_policy': self.header['definition']['progress_supervision_policy'],
            'deadline_epoch': self.header['deadline_epoch']}
        self.author = OperationInput('progress', 'coder.a', 'coder', 'coder-original',
                                     str(self.root), options=options)
        result = OperationResult('completed', 'progress', 'coder.a', 'coder',
                                 self.author.command_id)
        self.reviewer = OperationInput('progress', 'code_review', 'code_review',
            'review-original', str(self.root), options=options,
            upstream_results={self.author.task_id: result.to_dict()},
            payload={'review_rework_targets': [{'target_agent': self.author.task_id,
                'stage': 'coder', 'description': 'Repair source behavior'}]})
        self.snapshot['tasks'] = {
            self.author.task_id: {'attempts': [{'state': 'succeeded',
                'command': {'execution_id': self.author.command_id,
                            'payload': self.author.to_dict()},
                'result': {'value': result.to_dict()}}]},
            self.reviewer.task_id: {'attempts': [{'state': 'running',
                'command': {'execution_id': self.reviewer.command_id,
                            'payload': self.reviewer.to_dict()}}]}}

    def test_ordinary_assignment_uses_remaining_original_run_deadline(self):
        command = replace(self.reviewer, payload={})
        deadline = self.header['deadline_epoch']
        self.assertEqual(14400, interactive_review_timeout_cap(command, now=deadline - 14400))
        self.assertEqual(45, interactive_review_timeout_cap(command, now=deadline - 45))
        self.assertEqual(0, interactive_review_timeout_cap(command, now=deadline + 1))

    def test_missing_deadline_does_not_reinstate_an_assignment_time_limit(self):
        command = replace(self.reviewer, options={**self.reviewer.options,
                                                  'deadline_epoch': None})
        with self.assertRaisesRegex(ValueError, 'original Run deadline'):
            interactive_review_timeout_cap(command)

    def test_session_producer_preserves_original_deadline_across_transport_restart(self):
        path = prepare_session(self.reviewer, self.root / 'worktree', 30000)
        self.assertEqual(self.header['deadline_epoch'], Session.load(path).deadline_epoch)
        earlier = self.header['deadline_epoch'] - 100
        document = json.loads(path.read_text())
        document['deadline_epoch'] = earlier
        atomic_json(path, document)
        prepare_session(self.reviewer, self.root / 'worktree', 30000)
        self.assertEqual(earlier, Session.load(path).deadline_epoch)

    def _session(self):
        return prepare_session(self.reviewer, self.root / 'worktree',
                               interactive_review_timeout_cap(self.reviewer))

    def _request(self, path):
        request = {'request_id': 'repair', 'run_id': 'progress',
            'reviewer_execution_id': self.reviewer.command_id,
            'target_agent': self.author.task_id, 'instructions': 'Fix actual behavior.'}
        atomic_json(path.parent / 'requests/repair.json', request)

    def test_author_round_count_does_not_stop_current_nested_dispatch(self):
        path = self._session()
        self._request(path)
        family = 'review_rework:' + self.author.task_id
        self.app['rounds'][family] = 20
        changes = self.host._review_rework_decision(self.snapshot, self.header, self.app)
        addition = next(change for change in changes if change['kind'] == 'add_task')
        child = OperationInput.from_dict(unpack_input(self.root, addition['command']['payload']))
        self.assertEqual('agent_rework', child.stage_id)
        self.assertEqual(21, self.app['rounds'][family])
        self.assertEqual(1, self.app['agent_assignments'])
        self.assertGreater(addition['command']['timeout_seconds'], 7200)
        self.assertLessEqual(child.options['deadline_epoch'], self.header['deadline_epoch'])
        self.assertTrue(progress_supervised(child))

    def test_assignment_budget_still_stops_nested_dispatch(self):
        path = self._session()
        self._request(path)
        self.app['agent_assignments'] = self.header['request']['budget']['max_agent_assignments']
        self.assertEqual([], self.host._review_rework_decision(
            self.snapshot, self.header, self.app))
        record = self.app['review_rework']['requests']['review-original/repair']
        self.assertEqual('failed', record['state'])
        self.assertIn('Agent assignment budget exhausted', record['error'])

    def test_native_goal_returns_diagnostics_without_a_round_count_stop(self):
        from modport.goal_runtime import run_goal
        from tests.test_goal_runtime import FakeOpenCode

        FakeOpenCode.instances = []
        FakeOpenCode.sessions_by_root = {}
        FakeOpenCode.behaviors = [{'text': 'Candidate ready for host inspection.'}]
        FakeOpenCode.connected = True
        FakeOpenCode.transient_mcp_timeouts = 0
        FakeOpenCode.model_connected = True
        class CurrentOpenCode(FakeOpenCode):
            def ownership_record(self):
                return {'pid': self.process.pid, 'birth': self.process_birth,
                        'cwd': str(self.cwd), 'executable': self.executable}

        request = self.header['request']
        command = replace(self.author,
            options={**self.author.options, 'host_collect_candidate': True,
                     'host_settlement_deadline_epoch': self.header['deadline_epoch']},
            payload={'request': {**request,
                'budget': {**request['budget'], 'max_rework_rounds': 0}}})
        with patch('modport.goal_runtime.OpenCodeServer', CurrentOpenCode):
            result = run_goal(command=command, root=self.root,
                worktree=self.root / 'worktree', prompt='Preserve source behavior.',
                objective='Complete the assigned repair.',
                validate=lambda: {'accepted': False,
                    'failures': ['Host runtime witness remains missing'], 'evidence': {}},
                timeout=interactive_review_timeout_cap(command))
        self.assertEqual(0, result.returncode, result.metadata)
        self.assertFalse(result.metadata['host_accepted'])
        self.assertEqual('completed_with_diagnostics', result.metadata['status'])
        self.assertNotIn('stop_reason', result.metadata)
        self.assertEqual(1, len(result.metadata['validation_records']))
        self.assertLessEqual(result.metadata['deadline_epoch'], self.header['deadline_epoch'])
        self.assertEqual(self.header['deadline_epoch'], result.metadata['host_deadline_epoch'])

    def test_producer_mcp_host_child_and_returned_failure_share_original_window(self):
        path = self._session()
        session = Session.load(path)
        server = ReworkServer(session, poll_interval=0.01)
        pending = PendingCall(1)
        result = []
        worker = threading.Thread(target=lambda: result.append(server._request_rework(
            pending, {'target_agent': self.author.task_id,
                      'instructions': 'Repair the source behavior.'})))
        worker.start()
        self.addCleanup(worker.join, 2)
        self.addCleanup(server._stop.set)
        bound = time.monotonic() + 2
        request_paths = []
        while not request_paths and time.monotonic() < bound:
            request_paths = list(session.requests_dir.glob('*.json'))
            if not request_paths:
                time.sleep(0.01)
        self.assertEqual(1, len(request_paths))
        request = json.loads(request_paths[0].read_text())
        self.assertGreater(request['response_deadline_epoch'], time.time() + 7200)
        changes = self.host._review_rework_decision(self.snapshot, self.header, self.app)
        addition = next(change for change in changes if change['kind'] == 'add_task')
        child = OperationInput.from_dict(unpack_input(self.root, addition['command']['payload']))
        self.assertEqual(request['response_deadline_epoch'], child.options['deadline_epoch'])
        self.assertEqual(self.author.command_id,
                         child.payload['reviewer_rework']['source_execution_id'])
        self.assertGreater(addition['command']['timeout_seconds'], 7200)
        failure = OperationResult('failed', child.run_id, child.task_id,
            child.stage_id, child.command_id, detail='Author transport rejected the credential',
            error_code='author_authentication_failed')
        self.snapshot['tasks'][child.task_id] = {'attempts': [{'state': 'succeeded',
            'command': {**addition['command'], 'payload': child.to_dict()},
            'result': {'value': failure.to_dict()}}]}
        self.assertEqual([], self.host._review_rework_decision(
            self.snapshot, self.header, self.app))
        project_rework_responses(self.root, self.app)
        worker.join(2)
        self.assertFalse(worker.is_alive())
        self.assertTrue(result[0]['isError'])
        self.assertIn(failure.detail, result[0]['content'][0]['text'])
        self.assertEqual(1, self.app['agent_assignments'])


if __name__ == '__main__':
    unittest.main()
