"""Planning is a separate conversation turn, never a completed coder goal."""
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from modport.goal_runtime import run_goal
from test_goal_runtime import FakeTransport, done


def message(text):
    return {'method': 'item/completed', 'params': {'threadId': 'native-thread',
        'item': {'type': 'agentMessage', 'text': text}}}


class RecordingTransport(FakeTransport):
    history_turns = []
    history_items = []

    def request(self, method, params):
        if method in {'thread/turns/list', 'thread/items/list'}:
            self.calls.append((method, params))
            rows = self.history_turns if method == 'thread/turns/list' else self.history_items
            if method == 'thread/items/list' and params.get('turnId') is not None:
                rows = [row for row in rows if row['turnId'] == params['turnId']]
            return {'data': list(rows), 'nextCursor': None, 'backwardsCursor': None}
        return super().request(method, params)

    def event(self):
        event = super().event()
        self.calls.append(('observed', event))
        return event


class NativeDialogueTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.plan = self.root / 'dialogue' / 'plan.md'
        self.command = SimpleNamespace(command_id='two-turn', options={'workflow_version': 17})
        RecordingTransport.instances = []
        RecordingTransport.events = []
        RecordingTransport.statuses = []
        RecordingTransport.history_turns = []
        RecordingTransport.history_items = []
        self.validate = Mock(return_value={'accepted': True})
        transport = patch('modport.goal_runtime._Transport', RecordingTransport)
        transport.start()
        self.addCleanup(transport.stop)

    def execute(self, on_report=None):
        return run_goal(command=self.command, root=self.root, worktree=self.root,
            planning_prompt='Write only the task plan.', plan_path=self.plan,
            prompt='Execute the task plan.', objective='Bounded objective',
            validate=self.validate, timeout=30, on_report=on_report)

    def test_plan_precedes_goal_and_only_execution_is_validated(self):
        RecordingTransport.events = [message('# Plan\nInspect adapter.'), done('plan'),
                                     message('Implemented adapter.'), done('execute')]
        RecordingTransport.statuses = ['complete']
        result = self.execute()
        self.assertEqual(0, result.returncode, result.metadata)
        self.assertEqual('# Plan\nInspect adapter.', self.plan.read_text())
        self.validate.assert_called_once_with()
        calls = RecordingTransport.instances[0].calls
        starts = [row[1] for row in calls if isinstance(row, tuple) and row[0] == 'turn/start']
        self.assertEqual(['Write only the task plan.', 'Execute the task plan.'],
                         [params['input'][0]['text'] for params in starts])
        self.assertTrue(all(params['threadId'] == 'native-thread' for params in starts))
        plan_done = next(index for index, row in enumerate(calls)
                         if row == ('observed', done('plan')))
        goal_set = next(index for index, row in enumerate(calls)
                       if isinstance(row, tuple) and row[0] == 'thread/goal/set')
        self.assertLess(plan_done, goal_set)
        self.assertEqual(['execute'], [turn['id'] for turn in result.metadata['turns']])
        self.assertEqual(['plan'], [turn['id'] for turn in result.metadata['planning_turns']])
        self.assertNotIn('Inspect adapter', result.stdout)
        self.assertIn('Implemented adapter', result.stdout)

    def test_empty_plan_is_diagnostic_and_still_executes(self):
        RecordingTransport.events = [done('plan'), done('execute')]
        RecordingTransport.statuses = ['complete']
        result = self.execute()
        self.assertEqual(0, result.returncode)
        self.assertEqual('', self.plan.read_text())
        self.assertIn('planning reply was empty', result.metadata['business_diagnostics'])
        self.validate.assert_called_once_with()

    def test_failed_plan_never_starts_or_validates_the_goal(self):
        event = done('plan')
        event['params']['turn']['status'] = 'failed'
        RecordingTransport.events = [event]
        result = self.execute()
        self.assertNotEqual(0, result.returncode)
        self.validate.assert_not_called()
        self.assertFalse(any(isinstance(row, tuple) and row[0] == 'thread/goal/set'
                             for row in RecordingTransport.instances[0].calls))

    def test_explicit_recovery_during_plan_does_not_expect_a_goal(self):
        RecordingTransport.events = [TimeoutError('interrupted during plan')]
        first = self.execute()
        self.assertEqual('plan', first.metadata['dialogue_phase'])
        pending = first.metadata['pending_turn']
        turn_id = pending['turn_id']
        RecordingTransport.history_turns = [{'id': turn_id, 'status': 'inProgress'}]
        RecordingTransport.history_items = [{'turnId': turn_id, 'item': {
            'id': 'user-plan', 'type': 'userMessage', 'clientId': pending['client_user_message_id']}}]
        self.command.options['native_goal_resume'] = True
        RecordingTransport.events = [message('Recovered plan'), done(turn_id), done('execute')]
        RecordingTransport.statuses = ['complete']
        resumed = self.execute()
        self.assertEqual(0, resumed.returncode, resumed.metadata)
        self.assertEqual('Recovered plan', self.plan.read_text())
        self.validate.assert_called_once_with()
        calls = RecordingTransport.instances[-1].calls
        first_goal_get = next(i for i, row in enumerate(calls)
                              if isinstance(row, tuple) and row[0] == 'thread/goal/get')
        plan_done = calls.index(('observed', done(turn_id)))
        self.assertLess(plan_done, first_goal_get)
        starts = [row for row in calls if isinstance(row, tuple) and row[0] == 'turn/start']
        self.assertEqual(1, len(starts), 'recovery must adopt the in-progress planning turn')

    def test_recovery_after_plan_preserves_it_and_starts_execution(self):
        RecordingTransport.events = [message('Saved plan'), done('plan')]
        original = RecordingTransport.request

        def interrupted(transport, method, params):
            if method == 'thread/goal/set':
                raise KeyboardInterrupt()
            return original(transport, method, params)

        with patch.object(RecordingTransport, 'request', interrupted):
            first = self.execute()
        self.assertEqual('execute_pending', first.metadata['dialogue_phase'])
        self.command.options['native_goal_resume'] = True
        RecordingTransport.events = [message('Result'), done('execute')]
        RecordingTransport.statuses = ['active', 'complete']
        resumed = self.execute()
        self.assertEqual(0, resumed.returncode, resumed.metadata)
        self.assertEqual('Saved plan', self.plan.read_text())
        starts = [row[1] for row in RecordingTransport.instances[-1].calls
                  if isinstance(row, tuple) and row[0] == 'turn/start']
        self.assertEqual(['Execute the task plan.'], [row['input'][0]['text'] for row in starts])

    def test_plan_file_failure_recovers_completed_turn_without_repeating_plan(self):
        RecordingTransport.events = [message('Plan waiting for disk.'), done('turn')]
        original = Path.write_text

        def failed_write(path, *args, **kwargs):
            if path == self.plan:
                raise OSError('injected plan write failure')
            return original(path, *args, **kwargs)

        with patch.object(Path, 'write_text', failed_write):
            first = self.execute()
        self.assertEqual('failed', first.metadata['status'])
        self.assertEqual('plan', first.metadata['dialogue_phase'])
        self.assertEqual([], first.metadata['planning_turns'])
        self.validate.assert_not_called()
        pending = first.metadata['pending_turn']
        turn_id = pending['turn_id']
        RecordingTransport.history_turns = [{'id': turn_id, 'status': 'completed'}]
        RecordingTransport.history_items = [{'turnId': turn_id, 'item': {
            'id': 'user-plan', 'type': 'userMessage', 'clientId': pending['client_user_message_id']}}]
        self.command.options['native_goal_resume'] = True
        RecordingTransport.events = [message('Execution result'), done('execute')]
        RecordingTransport.statuses = ['complete']
        resumed = self.execute()
        self.assertEqual(0, resumed.returncode, resumed.metadata)
        self.assertEqual('Plan waiting for disk.', self.plan.read_text())
        starts = [row[1] for row in RecordingTransport.instances[-1].calls
                  if isinstance(row, tuple) and row[0] == 'turn/start']
        self.assertEqual(['Execute the task plan.'], [row['input'][0]['text'] for row in starts])
        self.assertEqual(1, len(resumed.metadata['planning_turns']))

    def test_uncertain_missing_turn_never_resends_on_recovery(self):
        RecordingTransport.events = [TimeoutError('reply unavailable')]
        first = self.execute()
        self.assertIn('pending_turn', first.metadata)
        self.command.options['native_goal_resume'] = True
        resumed = self.execute()
        self.assertEqual('failed', resumed.metadata['status'])
        self.assertFalse(any(isinstance(row, tuple) and row[0] == 'turn/start'
                             for row in RecordingTransport.instances[-1].calls))
        self.validate.assert_not_called()

    def test_recovery_after_acceptance_retains_final_public_report(self):
        RecordingTransport.events = [message('Saved plan'), done('plan'),
                                     message('Actual completed handoff.'), done('execute')]
        RecordingTransport.statuses = ['complete']
        first = self.execute()
        self.assertEqual(0, first.returncode)
        self.command.options['native_goal_resume'] = True
        resumed = self.execute()
        self.assertEqual(0, resumed.returncode, resumed.metadata)
        self.assertEqual(first.stdout, resumed.stdout)
        self.assertIn('Actual completed handoff.', resumed.stdout)
        self.assertNotIn('Saved plan', resumed.stdout)

    def test_report_is_materialized_before_candidate_validation(self):
        RecordingTransport.events = [message('Only the plan'), done('plan'),
                                     message('Final handoff'), done('execute')]
        RecordingTransport.statuses = ['complete']
        report = self.root / 'handoff.md'
        self.validate.side_effect = lambda: {'accepted': report.read_text() == 'Final handoff'}
        callback = Mock(side_effect=lambda text: report.write_text(text))
        result = self.execute(on_report=callback)
        self.assertEqual(0, result.returncode, result.metadata)
        callback.assert_called_once_with('Final handoff')
        self.validate.assert_called_once_with()

    def test_response_lost_after_server_applies_plan_does_not_start_another_plan(self):
        original = RecordingTransport.request

        def lost_response(transport, method, params):
            result = original(transport, method, params)
            if method == 'turn/start':
                RecordingTransport.history_turns = [{'id': 'persisted-plan', 'status': 'completed'}]
                RecordingTransport.history_items = [
                    {'turnId': 'persisted-plan', 'item': {'id': 'user-plan', 'type': 'userMessage',
                     'clientId': params['clientUserMessageId']}},
                    {'turnId': 'persisted-plan', 'item': {'id': 'plan-text', 'type': 'agentMessage',
                     'text': 'Plan completed on server.'}},
                ]
                raise KeyboardInterrupt()
            return result

        with patch.object(RecordingTransport, 'request', lost_response):
            first = self.execute()
        self.assertEqual('plan', first.metadata['pending_turn']['phase'])
        self.command.options['native_goal_resume'] = True
        RecordingTransport.events = [message('Final handoff'), done('execute')]
        RecordingTransport.statuses = ['complete']
        resumed = self.execute()
        self.assertEqual(0, resumed.returncode, resumed.metadata)
        self.assertEqual('Plan completed on server.', self.plan.read_text())
        starts = [row[1] for row in RecordingTransport.instances[-1].calls
                  if isinstance(row, tuple) and row[0] == 'turn/start']
        self.assertEqual(['Execute the task plan.'], [row['input'][0]['text'] for row in starts])

    def test_response_lost_after_server_executes_does_not_repeat_work(self):
        original = RecordingTransport.request
        RecordingTransport.events = [message('Plan'), done('plan')]

        def lost_response(transport, method, params):
            result = original(transport, method, params)
            if method == 'turn/start' and params['input'][0]['text'] == 'Execute the task plan.':
                RecordingTransport.history_turns = [{'id': 'persisted-execution', 'status': 'completed'}]
                RecordingTransport.history_items = [
                    {'turnId': 'persisted-execution', 'item': {'id': 'user-execution', 'type': 'userMessage',
                     'clientId': params['clientUserMessageId']}},
                    {'turnId': 'persisted-execution', 'item': {'id': 'execution-text', 'type': 'agentMessage',
                     'text': 'Work already completed.'}},
                ]
                raise KeyboardInterrupt()
            return result

        with patch.object(RecordingTransport, 'request', lost_response):
            first = self.execute()
        self.assertEqual('execute', first.metadata['pending_turn']['phase'])
        self.command.options['native_goal_resume'] = True
        RecordingTransport.events = []
        RecordingTransport.statuses = ['complete', 'complete']
        resumed = self.execute()
        self.assertEqual(0, resumed.returncode, resumed.metadata)
        self.assertIn('Work already completed.', resumed.stdout)
        self.assertFalse(any(isinstance(row, tuple) and row[0] == 'turn/start'
                             for row in RecordingTransport.instances[-1].calls))
        self.validate.assert_called_once_with()


if __name__ == '__main__':
    unittest.main()
