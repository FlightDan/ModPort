"""Stopped coder edits remain authenticated across a budget continuation."""
from dataclasses import replace
from hashlib import sha256
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from modport.contracts import OperationInput, OperationResult
from modport.development import _check_ref, _apply
from modport.partial_recovery import (recover_interrupted_coders,
    carry_overwritten_coder_patches, carry_failed_rework_instructions)


def git(root, *args):
    return subprocess.check_output(['git', '-c', 'user.name=Test',
        '-c', 'user.email=test@example.invalid', *args], cwd=root,
        stderr=subprocess.DEVNULL).decode().strip()


class PartialRecoveryTests(unittest.TestCase):
    def test_stopped_coder_patch_is_frozen_and_replayed_into_new_checkout(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            original = root / 'worktree'
            original.mkdir()
            git(original, 'init', '-q')
            (original / 'a.txt').write_text('original\n')
            git(original, 'add', '.')
            git(original, 'commit', '-qm', 'base')
            base = git(original, 'rev-parse', 'HEAD')
            task = {'id': 'a', 'owned_paths': ['a.txt']}
            old = OperationInput('logical', 'coder.g1.a', 'coder', 'run:coder.g1.a:1', str(root),
                payload={'development_task': task, 'dependency_patches': []},
                options={'workspace': 'workspaces/development/g1/a', 'workflow_version': 17,
                         'deadline_epoch': 1}, artifact_refs={'development_plan': {'path': 'p'}})
            workspace = root / old.options['workspace']
            workspace.parent.mkdir(parents=True)
            git(root, 'clone', '-q', str(original), str(workspace))
            (workspace / 'a.txt').write_text('interrupted\n')
            record = {'run_id': 'logical', 'command_id': old.command_id,
                      'workspace': old.options['workspace'], 'task_id': old.task_id,
                      'generation': 1, 'base_commit': base, 'task': task,
                      'plan_ref': old.artifact_refs['development_plan'],
                      'dependency_refs': [], 'start_commit': base,
                      'start_tree': git(workspace, 'rev-parse', base + '^{tree}')}
            envelope = {'record': record, 'sha256': sha256(json.dumps(
                record, sort_keys=True, separators=(',', ':')).encode()).hexdigest()}
            setup = root / 'artifacts/executions' / old.command_id / 'coder-setup.json'
            setup.parent.mkdir(parents=True)
            setup.write_text(json.dumps(envelope))
            native = root / 'artifacts/native-goals' / sha256(old.command_id.encode()).hexdigest()[:24]
            native.mkdir(parents=True)
            (native / 'state.json').write_text(json.dumps({'command_id': old.command_id,
                'worktree': str(workspace), 'producer_stopped': True,
                'status': 'timed_out', 'owned_pid': 111}))
            state = {'run_id': 'run', 'revision': 6,
                     'input': {'logical_run_id': 'logical'}, 'tasks': {old.task_id: {
                'attempts': [{'state': 'succeeded', 'command': {'payload': old.to_dict()}}]}}}
            group = {'generation': 1, 'base': base, 'execution_payload': {
                'interrupted_development_work': [{'task_id': 'a', 'command_id': old.command_id,
                                                  'workspace': old.options['workspace']}]}}
            with patch('modport.partial_recovery._previous_process_alive', return_value=False):
                refs = recover_interrupted_coders(root, state, group)
                self.assertEqual(refs, recover_interrupted_coders(root, state, group))
            ref = refs['a']
            self.assertEqual('run:coder.g1.a:1', ref['metadata']['source_command_id'])
            self.assertIn('interrupted', (root / ref['path']).read_text())
            new = replace(old, command_id='next:coder.g1.a:1', options={'workflow_version': 17})
            resumed = root / 'resumed'
            git(root, 'clone', '-q', str(original), str(resumed))
            _apply(new, resumed, _check_ref(new, ref, task, base, 1), task)
            self.assertEqual('interrupted\n', (resumed / 'a.txt').read_text())
            prior = OperationResult('completed', 'logical', old.task_id, 'coder',
                'before:coder.g1.a:1', outputs={'artifact_refs': {'coder_patch': ref}})
            previous = {'run_id': 'before', 'input': {'logical_run_id': 'logical'}, 'application_state': {
                'effective': {old.task_id: prior.to_dict()}}}
            state['application_state'] = {'effective': {old.task_id: OperationResult(
                'failed', 'logical', old.task_id, 'coder', old.command_id,
                error_code='rework_execution_failed').to_dict()}}
            group['tasks'] = [task]
            group['scheduled'] = []
            state['tasks'][old.task_id]['attempts'][-1]['state'] = 'failed'
            self.assertEqual({'a': ref}, carry_overwritten_coder_patches(
                root, previous, state, group))
            current_ref = {**ref, 'metadata': {**ref['metadata'],
                                              'execution_id': old.command_id}}
            current_result = replace(prior, command_id=old.command_id,
                                     outputs={'artifact_refs': {'coder_patch': current_ref}})
            state['tasks'][old.task_id]['attempts'][-1]['result'] = {
                'value': current_result.to_dict()}
            state['tasks'][old.task_id]['attempts'][-1]['state'] = 'succeeded'
            self.assertEqual({'a': current_ref}, carry_overwritten_coder_patches(
                root, previous, state, group))
            state['tasks'][old.task_id]['attempts'][-1]['result']['value']['command_id'] = 'forged'
            with self.assertRaisesRegex(ValueError, 'SDK command differs'):
                carry_overwritten_coder_patches(root, previous, state, group)
            state['application_state']['review_rework'] = {'requests': {'explicit': {
                'request_id': 'explicit', 'target_agent': old.task_id,
                'state': 'failed', 'sequence': 4,
                'instructions': 'Repair a.txt:1 using the new API',
                'error': 'target_build failed'}}}
            self.assertEqual('Repair a.txt:1 using the new API',
                carry_failed_rework_instructions(state, group)['a']['instructions'])
            (workspace / 'a.txt').write_text('changed after freeze\n')
            with patch('modport.partial_recovery._previous_process_alive', return_value=False):
                with self.assertRaises(ValueError):
                    recover_interrupted_coders(root, state, group)

    def test_alive_producer_cannot_be_collected(self):
        with tempfile.TemporaryDirectory() as temporary:
            group = {'generation': 1, 'base': '0' * 40, 'execution_payload': {
                'interrupted_development_work': [{'task_id': 'a', 'command_id': 'run:coder.g1.a:1',
                                                  'workspace': 'workspaces/a'}]}}
            with self.assertRaises(ValueError):
                recover_interrupted_coders(temporary, {'run_id': 'run', 'tasks': {}}, group)
