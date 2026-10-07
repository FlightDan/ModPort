"""Regression coverage for revised DAGs and continued patch provenance."""
import copy
from dataclasses import replace
import json
from hashlib import sha256
import unittest
from unittest.mock import patch
import test_development as fixtures
from modport import development
from modport.handlers import _result


class HandoffIdentityTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.IsolatedDevelopmentTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)

    def command(self, name, patches=()):
        f = self.fixture
        command = f.command('coder', name, patches=patches)
        return replace(command, options={**command.options, 'workflow_version': 17})

    def test_scheduled_plan_takes_precedence_over_stale_report(self):
        command = self.command('b')
        plan = copy.deepcopy(self.fixture.frozen.outputs)
        tasks = plan['development_tasks']
        tasks[1]['objective'] = 'Revised caller'
        tasks[1]['dependencies'] = []
        command = replace(command, payload={**command.payload,
            'execution_development_plan': {'base_commit': self.fixture.base, 'tasks': tasks}})
        actual = development._plan(command)
        self.assertEqual('Revised caller', actual['tasks'][1]['objective'])
        self.assertEqual([], actual['tasks'][1]['dependencies'])

    def test_dependency_identity_is_not_list_position(self):
        f = self.fixture
        first = f.coder('a')
        real = first.outputs['artifact_refs']['coder_patch']
        # An unrelated ref before the real dependency used to shift zip pairing.
        extra = {**real, 'metadata': {**real['metadata'], 'task_id': 'unrelated'}}
        command = self.command('b', [extra, real])
        def author(handler, cmd):
            work = f.root / cmd.options['workspace']
            self.assertEqual('changed a\n', (work / 'a.txt').read_text())
            (work / 'b.txt').write_text('caller\n')
            return _result(cmd, 'completed')
        with patch('modport.handlers.CodexStageHandler.__call__', author):
            result = f.registry['coder'](command)
        self.assertEqual('completed', result.status, result.detail)
        self.assertEqual(['a'], result.outputs['dependency_patch_tasks'])

    def test_duplicate_dependency_identity_is_not_silently_chosen(self):
        f = self.fixture
        first = f.coder('a')
        ref = first.outputs['artifact_refs']['coder_patch']
        command = self.command('b', [ref, ref])
        def author(handler, cmd):
            self.assertEqual('original a\n', (f.root / cmd.options['workspace'] / 'a.txt').read_text())
            return _result(cmd, 'completed')
        with patch('modport.handlers.CodexStageHandler.__call__', author):
            result = f.registry['coder'](command)
        self.assertTrue(any('duplicate dependency' in row for row in result.outputs['business_diagnostics']))

    def test_continued_integration_accepts_only_bound_carried_result(self):
        f = self.fixture
        first = f.coder('a').to_dict()
        command = f.command('development_integrate', 'join', results=[first])
        bound = sha256(json.dumps(first, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
        command = replace(command, run_id='successor', options={'workflow_version': 17},
            payload={**command.payload, 'carried_development_results': {first['command_id']: bound}})
        result = f.registry['development_integrate'](command)
        self.assertEqual('completed', result.status, result.detail)
        self.assertIn('a', result.outputs['integrated_task_ids'])
        self.assertEqual('changed a\n', (f.work / 'a.txt').read_text())

    def test_workspace_epoch_is_contained_and_distinct(self):
        self.assertEqual('workspaces/development/g1/a-segment-' + 'a'*16,
            development.development_workspace(1, 'a', {'development_workspace_epoch': 'a'*16}))
        with self.assertRaises(ValueError):
            development.development_workspace(1, 'a', {'development_workspace_epoch': '../escape'})
