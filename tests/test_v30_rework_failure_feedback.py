"""v30 coder failures reach the reviewer without empty candidate builds."""
import json
import tempfile
import unittest
from pathlib import Path

from modport.contracts import OperationInput, OperationResult
from modport.rework_orchestration import (
    ReviewReworkOrchestration,
    project_rework_responses,
)


class Host(ReviewReworkOrchestration):
    clock = staticmethod(lambda: 1)

    def __init__(self):
        self.scheduled = []

    def _schedule(self, snapshot, header, app, stage, **kwargs):
        self.scheduled.append(stage)
        return [{'kind': 'dispatch', 'task_id': kwargs['task_id']}]


class V30ReworkFailureFeedbackTests(unittest.TestCase):
    def _state(self, directory, outputs):
        host = Host()
        command = OperationInput('run', 'child', 'agent_rework', 'child-exec', directory)
        review = OperationInput('run', 'review', 'code_review', 'review-exec', directory,
            options={'workflow_version': 30})
        result = OperationResult('failed', 'run', 'child', 'agent_rework', 'child-exec',
            outputs=outputs, detail='supervised coder rework needs its authenticated source goal',
            error_code='coder_rework_invalid')
        record = {
            'state': 'running',
            'task_id': 'child',
            'reviewer_execution_id': 'review-exec',
            'target_stage': 'coder',
            'target_agent': 'coder-a',
            'reviewer_stage': 'code_review',
            'request_id': 'request',
            'updates': [],
        }
        app = {
            'review_rework': {'requests': {'review-exec/request': record},
                              'latest_targets': {}, 'sequence': 1},
            'cancel_sent': [],
            'processed': [],
            'history': [],
            'effective': {},
        }
        snapshot = {'tasks': {
            'child': {'attempts': [{
                'state': 'succeeded',
                'command': {'execution_id': command.command_id,
                            'payload': command.to_dict()},
                'result': {'value': result.to_dict()},
            }]},
            'review': {'attempts': [{
                'state': 'running',
                'command': {'execution_id': review.command_id,
                            'payload': review.to_dict()},
            }]},
        }}
        header = {'run_dir': directory, 'definition': {'workflow_version': 30}}
        return host, snapshot, header, app, record

    def test_pre_coder_failure_is_returned_without_empty_candidate_build(self):
        with tempfile.TemporaryDirectory() as directory:
            host, snapshot, header, app, record = self._state(
                directory, {'artifact_refs': {}})

            operations = host._review_rework_decision(snapshot, header, app)

            self.assertEqual([], operations)
            self.assertEqual([], host.scheduled)
            self.assertEqual('failed', record['state'])
            self.assertEqual(
                'coder: supervised coder rework needs its authenticated source goal',
                record['error'])
            self.assertEqual('coder_rework_invalid', record['updates'][0]['result']['error_code'])
            self.assertEqual('failed', app['effective']['coder-a']['status'])

            project_rework_responses(directory, app)
            response_path = (Path(directory) / 'artifacts/rework-tools/review-exec/responses/request.json')
            response = json.loads(response_path.read_text(encoding='utf-8'))
            self.assertEqual(record['error'], response['error'])
            self.assertIn('supervised coder rework needs its authenticated source goal',
                          response['text'])

    def test_failed_integrated_candidate_still_gets_fresh_build(self):
        with tempfile.TemporaryDirectory() as directory:
            host, snapshot, header, app, record = self._state(directory, {
                'before_head': 'a' * 40,
                'after_head': 'b' * 40,
                'integration_status': 'integrated',
                'artifact_refs': {},
            })

            operations = host._review_rework_decision(snapshot, header, app)

            self.assertEqual(['target_build'], host.scheduled)
            self.assertEqual('dispatch', operations[0]['kind'])
            self.assertEqual('target_build', record['followup_stage'])
            self.assertEqual('failed', record['updates'][0]['result']['status'])
            self.assertEqual('failed', app['effective']['coder-a']['status'])

    def test_failed_rework_with_no_integrated_changes_skips_build(self):
        with tempfile.TemporaryDirectory() as directory:
            head = 'a' * 40
            host, snapshot, header, app, record = self._state(directory, {
                'before_head': head,
                'after_head': head,
                'integration_status': 'no_changes',
                'artifact_refs': {},
            })

            operations = host._review_rework_decision(snapshot, header, app)

            self.assertEqual([], operations)
            self.assertEqual([], host.scheduled)
            self.assertEqual('failed', record['state'])
            self.assertEqual(
                'coder: supervised coder rework needs its authenticated source goal',
                record['error'])


if __name__ == '__main__':
    unittest.main()
