import unittest

from modport.goal_progress import observe_goal_item


class GoalProgressTests(unittest.TestCase):
    def test_repeated_observation_is_diagnostic_not_stagnation_proof(self):
        state = {}
        for i in range(3):
            observe_goal_item(state, {'type': 'commandExecution', 'id': str(i),
                                     'command': 'check', 'aggregatedOutput': 'same', 'exitCode': 1})
        self.assertEqual(3, state['repeated_diagnostic']['count'])
        self.assertFalse(state['repeated_diagnostic']['no_progress_conclusion'])
        self.assertFalse(state['repeated_diagnostic']['candidate_identity_verified'])
        self.assertNotIn('status', state)
        self.assertNotIn('aggregatedOutput', str(state))

    def test_bounded_history_and_file_change_resets_repetition_window(self):
        state = {}
        for i in range(100):
            observe_goal_item(state, {'type': 'commandExecution', 'command': str(i)})
        self.assertEqual(32, len(state['observations']))
        observe_goal_item(state, {'type': 'fileChange', 'changes': []})
        self.assertEqual([], state['observations'])
        self.assertEqual(1, state['file_change_events'])
