"""Only real evidence and an exhausted window allow one bounded successor."""
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
import tempfile
import unittest
from modport.progress_runner import continue_with_progress


class ProgressRunnerTests(unittest.TestCase):
    def exercise(self, tangible, reason='budget_exhausted'):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        root = Path(td.name)
        ops = Mock()
        start = SimpleNamespace(run_id='next', snapshot={'state': 'running', 'input': {'started_at': 0}})
        stopped = SimpleNamespace(run_id='next', snapshot={'state': 'failed',
            'input': {'started_at': 0, 'deadline_epoch': 8}, 'application_state': {'terminal_reason': reason}})
        extended = SimpleNamespace(run_id='next:progress-extension', snapshot={'state': 'succeeded',
            'input': {'started_at': 8, 'deadline_epoch': 12}, 'application_state': {}})
        ops.execute.side_effect = [stopped, extended]
        delta = {'tangible_progress': tangible, 'inventory': {'closed': int(tangible)}}
        with patch('modport.progress_runner.continue_from_planner', side_effect=[start, extended]) as continuation, \
             patch('modport.progress_runner.inspect_runtime', return_value={}), \
             patch('modport.progress_runner.collect_progress_evidence', return_value={}), \
             patch('modport.progress_runner.compare_progress_evidence', return_value=delta):
            result = continue_with_progress(ops, root, 'old', next_run_id='next', reason='approved',
                initial_seconds=8, maximum_seconds=12, clock=lambda: 8)
        return result, continuation

    def test_progress_permits_four_hour_equivalent_extension(self):
        result, calls = self.exercise(True)
        self.assertEqual('next:progress-extension', result.run_id)
        self.assertEqual(4, calls.call_args.kwargs['additional_seconds'])
        self.assertEqual(2, calls.call_count)

    def test_revision_only_does_not_extend(self):
        result, calls = self.exercise(False)
        self.assertEqual('next', result.run_id)
        self.assertEqual(1, calls.call_count)

    def test_nonbudget_failure_is_not_retried(self):
        _, calls = self.exercise(True, reason='agent_failed')
        self.assertEqual(1, calls.call_count)

    def test_replay_resumes_existing_extension_without_resetting_budget(self):
        from modport.evidence import atomic_json
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            policy = root / 'artifacts/progress-continuations/next/policy.json'
            policy.parent.mkdir(parents=True)
            extension = {'next_run_id': 'next:progress-extension',
                         'reason': 'recorded progress', 'additional_seconds': 4}
            atomic_json(policy, {'request': {'source_run_id': 'old', 'next_run_id': 'next',
                'reason': 'approved', 'initial_seconds': 8, 'maximum_seconds': 12},
                'started_at': 0, 'segment_started': True, 'baseline': {}, 'extension': extension})
            run = SimpleNamespace(run_id=extension['next_run_id'], snapshot={'state': 'failed',
                'input': {'started_at': 8, 'deadline_epoch': 12},
                'application_state': {'terminal_reason': 'budget_exhausted'}})
            ops = Mock()
            ops.execute.return_value = run
            with patch('modport.progress_runner.continue_from_planner', return_value=run) as continuation, \
                 patch('modport.progress_runner.inspect_runtime', return_value={}), \
                 patch('modport.progress_runner.collect_progress_evidence', return_value={}), \
                 patch('modport.progress_runner.compare_progress_evidence', return_value={'tangible_progress': True}):
                continue_with_progress(ops, root, 'old', next_run_id='next', reason='approved',
                    initial_seconds=8, maximum_seconds=12, clock=lambda: 12)
            self.assertEqual(1, continuation.call_count)
            self.assertEqual('next', continuation.call_args.args[2])
            self.assertEqual(4, continuation.call_args.kwargs['additional_seconds'])
