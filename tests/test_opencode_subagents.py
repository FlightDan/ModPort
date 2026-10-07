"""Native helper selection and host permission boundaries."""
import copy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from modport.model_policy import resolve_model_selection, update_model_config
from modport.opencode_agent import run_agent
from modport.opencode_subagents import subagent_profiles, TASK_PERMISSION
from test_opencode_agent import _Server


class SubagentTests(unittest.TestCase):
    def setUp(self):
        self.policy = {
            'default': {'model': 'gpt-6-luna', 'reasoning_effort': 'max'},
            'roles': {'coder': {'model': 'gpt-6.1-sol', 'reasoning_effort': 'high'}},
            'stages': {},
        }

    def test_default_tracks_coder_and_explicit_override_is_independent(self):
        self.assertEqual(self.policy['roles']['coder'], resolve_model_selection(self.policy, 'subagent'))
        self.policy['stages']['coder'] = {'model': 'coder-stage', 'reasoning_effort': 'low'}
        self.assertEqual(self.policy['stages']['coder'], resolve_model_selection(self.policy, 'subagent'))
        changed = update_model_config(self.policy, 'subagent', 'gpt-6-luna', 'max')
        self.assertEqual(self.policy['default'], resolve_model_selection(changed, 'subagent'))
        self.assertNotIn('subagent', self.policy['roles'])
        self.assertEqual(self.policy['roles']['coder'], changed['roles']['coder'])

    def test_profiles_preserve_parent_denies_and_keep_rework_with_parent(self):
        permissions = {'edit': 'deny', 'read': {'*': 'allow', '*.key': 'deny'},
                       'external_directory': 'deny', 'webfetch': 'deny',
                       'modport_sandbox_run_project_command': 'deny', 'task': TASK_PERMISSION,
                       'modport_rework_request_rework': 'allow'}
        original = copy.deepcopy(permissions)
        profiles = subagent_profiles(model_policy=self.policy, model='parent',
                                     variant='max', permissions=permissions)
        for profile in profiles.values():
            self.assertEqual('openai/gpt-6.1-sol', profile['model'])
            self.assertEqual('high', profile['variant'])
            for name in ('edit', 'bash', 'shell', 'terminal', 'task', 'external_directory',
                         'webfetch', 'modport_sandbox_run_project_command', 'modport_rework_*'):
                self.assertEqual('deny', profile['permission'][name])
            self.assertEqual('deny', profile['permission']['read']['*.key'])
        self.assertEqual(original, permissions)

    def test_execution_exposes_task_but_planning_and_tool_free_calls_do_not(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            server = _Server()
            with patch.object(server, 'tool_ids', return_value=['read', 'edit', 'task', 'bash']), \
                    patch('modport.opencode_agent.OpenCodeServer.start', return_value=server) as start:
                run_agent(prompt='Execute', planning_prompt='Plan', plan_path=root / 'plan.md',
                          cwd=root, log=root / 'agent.log', model='gpt-6-luna', variant='max',
                          timeout=10, read_only=True)
            config = start.call_args.kwargs['config']
            self.assertEqual(TASK_PERMISSION, config.permission['task'])
            self.assertEqual('deny', config.agent['general']['permission']['edit'])
            self.assertFalse(server.turns[0][2]['tools']['task'])
            self.assertNotEqual(False, server.turns[1][2]['tools'].get('task'))
            tool_free = _Server()
            with patch.object(tool_free, 'tool_ids', return_value=['task', 'read']), \
                    patch('modport.opencode_agent.OpenCodeServer.start', return_value=tool_free) as start:
                run_agent(prompt='Preflight', cwd=root, log=root / 'preflight.log',
                          model='gpt-6-luna', variant='max', timeout=10, no_tools=True)
            self.assertEqual({}, start.call_args.kwargs['config'].agent)
            self.assertEqual('deny', start.call_args.kwargs['config'].permission['task'])
            self.assertFalse(tool_free.turns[0][2]['tools']['task'])

    def test_explore_cannot_use_a_writable_parents_command_tool(self):
        profiles = subagent_profiles(model_policy=self.policy, model='parent', variant='high',
            permissions={'modport_sandbox_run_project_command': 'allow'}, inherit_coder=True)
        self.assertNotIn('model', profiles['general'])
        self.assertEqual('allow', profiles['general']['permission']['modport_sandbox_run_project_command'])
        self.assertEqual('deny', profiles['explore']['permission']['modport_sandbox_run_project_command'])
        self.assertEqual('deny', profiles['explore']['permission']['edit'])
        self.policy['roles']['subagent'] = self.policy['default']
        profiles = subagent_profiles(model_policy=self.policy, model='parent', variant='high',
                                    permissions={}, inherit_coder=True)
        self.assertEqual('openai/gpt-6-luna', profiles['general']['model'])
        self.assertEqual('max', profiles['general']['variant'])
