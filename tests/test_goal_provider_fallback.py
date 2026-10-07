"""Provider fallback stays inside one durable goal and never replays tool work."""
import unittest
import test_goal_runtime as peer
from unittest.mock import patch
from modport.workflow import WORKFLOW_VERSION


class FakeOpenCode(peer.FakeOpenCode):
    def require_model(self, **kwargs):
        if self.disconnect_primary and kwargs["model"].startswith("bailian/"):
            raise OpenCodeError("OpenCode provider 'bailian' is not connected")
        return super().require_model(**kwargs)

    def providers(self, **kwargs):
        return {"all": [{"id": "openai", "models": {"gpt-6-luna": {"limit": {"context": 7000, "input": 6000, "output": 500}}}}]}

    def ownership_record(self):
        return {"pid": self.process.pid, "birth": None, "kind": "opencode-server"}
from modport.opencode_runtime import OpenCodeResponseError, OpenCodeError


class GoalProviderFallbackTests(unittest.TestCase):
    def setUp(self):
        peer.GoalRuntimeTests.setUp(self)
        self.patch.stop()
        self.current_patch = patch("modport.goal_runtime.OpenCodeServer", FakeOpenCode)
        self.current_patch.start()
        self.addCleanup(self.current_patch.stop)
        FakeOpenCode.instances = []
        FakeOpenCode.sessions_by_root = {}
        FakeOpenCode.behaviors = []
        FakeOpenCode.disconnect_primary = False

    run_goal = peer.GoalRuntimeTests.run_goal

    def set_selection(self):
        self.command.options.update(workflow_version=WORKFLOW_VERSION, model_policy={
            'default': {'model': 'bailian/deepseek-v4.1-flash', 'reasoning_effort': 'max',
                'fallback': {'model': 'openai/gpt-6-luna', 'reasoning_effort': 'max'}}})
        self.command.run_dir = str(self.root)
        self.command.run_id = 'goal-provider'

    @staticmethod
    def unavailable():
        return OpenCodeResponseError({'name': 'APIError', 'data': {
            'message': 'provider unavailable', 'statusCode': 503}}, {})

    def test_definitive_provider_failure_uses_fallback_in_same_session(self):
        self.set_selection()
        FakeOpenCode.behaviors = [self.unavailable(), {}]
        result = self.run_goal()
        self.assertEqual(0, result.returncode, result.metadata)
        calls = FakeOpenCode.instances[-1].send_calls
        self.assertEqual(['bailian/deepseek-v4.1-flash', 'openai/gpt-6-luna'],
                         [call['model'] for call in calls])
        self.assertEqual(calls[0]['session_id'], calls[1]['session_id'])
        self.assertNotEqual(calls[0]['message_id'], calls[1]['message_id'])
        self.assertEqual('provider_unavailable', result.metadata['provider_fallback']['reason'])
        self.assertIn('code-simplifier', calls[1]['text'])

    def test_provider_error_after_tool_work_does_not_replay(self):
        self.set_selection()
        FakeOpenCode.behaviors = [{'parts': [{'type': 'tool', 'state': {'status': 'completed'}}],
                                   'raise_after': self.unavailable()}]
        result = self.run_goal()
        self.assertNotEqual(0, result.returncode)
        self.assertEqual(1, len(FakeOpenCode.instances[-1].send_calls))
        self.assertNotIn('provider_fallback', result.metadata)

    def test_timeout_and_model_blocked_do_not_switch_provider(self):
        self.set_selection()
        FakeOpenCode.behaviors = [TimeoutError('ambiguous request')]
        result = self.run_goal()
        self.assertNotEqual(0, result.returncode)
        self.assertNotIn('provider_fallback', result.metadata)

    def test_failed_fallback_is_not_retried(self):
        self.set_selection()
        FakeOpenCode.behaviors = [self.unavailable(), self.unavailable()]
        result = self.run_goal()
        self.assertNotEqual(0, result.returncode)
        self.assertEqual(2, len(FakeOpenCode.instances[-1].send_calls))
        self.assertEqual(1, len(result.metadata['attempts']))

    def test_fallback_accounts_for_failed_prompt_and_smaller_catalog(self):
        self.set_selection()
        FakeOpenCode.behaviors = [self.unavailable(), {}]
        result = self.run_goal(session_context_budget={'context_window': 9000,
            'input_token_budget': 7000, 'output_token_reserve': 1000, 'tool_token_reserve': 1000})
        self.assertEqual(0, result.returncode, result.metadata)
        self.assertEqual(5500, result.metadata['provider_fallback']['context_budget']['input_token_budget'])
        self.assertGreater(result.metadata['context_preflight']['prior_tokens'], 0)
        self.assertEqual('failed_provider_transcript_bound', result.metadata['context_preflight']['prior_source'])

    def test_initial_discovery_fallback_checks_smaller_prompt_capacity(self):
        self.set_selection()
        FakeOpenCode.disconnect_primary = True
        from modport.goal_runtime import run_goal
        result = run_goal(command=self.command, root=self.root, worktree=self.worktree,
            prompt='x' * 6000, objective='Bounded objective', timeout=30,
            session_context_budget={'context_window': 9000, 'input_token_budget': 7000,
                'output_token_reserve': 1000, 'tool_token_reserve': 1000},
            validate=lambda: {'accepted': True, 'failures': [], 'evidence': {}})
        self.assertNotEqual(0, result.returncode)
        self.assertIn('context preflight exceeded', result.metadata['error'])
        self.assertEqual([], FakeOpenCode.instances[-1].send_calls)

    def test_resume_discovery_checks_pending_tool_work_before_fallback(self):
        self.set_selection()
        FakeOpenCode.behaviors = [{'parts': [{'type': 'tool', 'state': {'status': 'completed'}}],
            'store_assistant': True, 'raise_after': TimeoutError('response ambiguous')}]
        first = self.run_goal()
        self.assertNotEqual(0, first.returncode)
        self.command.options['native_goal_resume'] = True
        FakeOpenCode.disconnect_primary = True
        resumed = self.run_goal()
        self.assertNotEqual(0, resumed.returncode)
        self.assertNotIn('provider_fallback', resumed.metadata)
        self.assertEqual([], FakeOpenCode.instances[-1].send_calls)
