"""Explicit watchdog repair reaches the normal coder revival consumer."""
from copy import deepcopy
from dataclasses import replace
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import time
import unittest
from unittest.mock import patch

from modport import coder_revival
from modport.contracts import OperationInput, OperationResult
from modport.memory_admission import MemorySnapshot
from modport.models import Budget, MigrationRequest
from modport.operations import MigrationOperations
from modport.payload_storage import unpack_input
from modport.watchdog_coder import restart_planner, revive
from modport.workflow import WORKFLOW_VERSION, compile_migration_workflow


class WatchdogCoderTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        request = MigrationRequest('watchdog', 'https://example.invalid/mod.git', '1.20.1', '26.1.2',
            budget=Budget(max_seconds=3600, max_agent_assignments=30), max_parallel_coders=3)
        self.owner = MigrationOperations(memory_probe=lambda: MemorySnapshot(64 * 1024 ** 3, 64 * 1024 ** 3, 'fixture'))
        self.header = {'run_id': 'run', 'logical_run_id': 'run', 'run_dir': str(self.root),
            'request': request.to_dict(), 'definition': compile_migration_workflow(request).to_dict(),
            'deadline_epoch': time.time() + 3600, 'registry_revision': 'host-registry',
            'initial_refs': {}, 'prior_findings': [], 'rubric_sha256': 'host-rubric-provenance'}
        self.plan = {'path': 'artifacts/development-plan.json', 'metadata': {'execution_id': 'planner'}}
        (self.root / 'artifacts').mkdir()
        (self.root / self.plan['path']).write_text('Host development plan evidence.\n')
        tasks = [{'id': name, 'objective': 'Deliver ' + name, 'owned_paths': ['src/' + name],
                  'dependencies': ['b'] if name == 'a' else [], 'acceptance': ['Deliver ' + name]}
                 for name in ('a', 'b', 'c')]
        self.group = {'kind': 'development', 'generation': 1, 'tasks': tasks,
            'base': 'host-base-commit', 'parallel': True, 'members': [],
            'scheduled': ['a', 'b', 'c'], 'results': {}, 'goal_scheduled': ['a', 'b', 'c'],
            'entry_stage': 'implementation', 'goal_scope': 'migration',
            'planning_context': {}, 'artifact_refs': {'development_plan': self.plan}, 'execution_payload': {}}
        self.app = self.owner._new_application()
        self.app['active_group'] = self.group
        self.app['agent_assignments'] = 4
        self.snapshot = {'run_id': 'run', 'revision': 7, 'generation': 0, 'state': 'running', 'tasks': {}}
        self.commands = {}
        self.outcomes = {}
        for task in tasks:
            name = task['id']
            (self.root / 'artifacts' / (name + '.patch')).write_text('Host patch evidence for ' + name + '\n')
            (self.root / 'artifacts' / (name + '-goal.json')).write_text(json.dumps({'task': name}) + '\n')
            task_id = coder_revival.coder_id(self.group, name)
            command = OperationInput('run', task_id, 'coder', 'run:' + task_id + ':1', str(self.root),
                payload={'development_task': task, 'goal_scope': 'migration',
                         'development_base': self.group['base'], 'development_generation': 1},
                artifact_refs={'development_plan': self.plan}, options={'workflow_version': WORKFLOW_VERSION})
            outcome = OperationResult('failed' if name == 'a' else 'completed', 'run', task_id,
                'coder', command.command_id, error_code='dependency_patch_conflict' if name == 'a' else None,
                outputs={'artifact_refs': {'coder_patch': {'path': 'artifacts/' + name + '.patch'}}})
            self.commands[name], self.outcomes[name] = command, outcome
            self.snapshot['tasks'][task_id] = {'attempts': [{'state': 'succeeded',
                'command': {'execution_id': command.command_id, 'payload': command.to_dict()},
                'result': {'value': outcome.to_dict()}}]}
            self.group['members'].append(task_id)
            self.group['results'][task_id] = outcome.to_dict()
            self.group['results']['goal.g1.' + name] = {'outputs': {'artifact_refs': {
                'coder_goal': {'path': 'artifacts/' + name + '-goal.json'}}}}
            if name != 'a':
                self.app['processed'].append(command.command_id)
        self.decision = {'incident_id': 'watchdog-a', 'action': 'repair_resume',
            'reason': 'The dependency patch failed before agent entry.',
            'instruction': 'Resolve the retained conflict against B and then verify the affected method.',
            'wait_for': [], 'stop_category': None}
        self.repair_ref = {'path': 'artifacts/diagnostic-repairs/supervisor/a.json',
            'metadata': {'task_id': 'a', 'plan_ref': self.plan, 'producer_execution_id': 'run:supervisor:1',
                         'run_id': 'run', 'applicable': True}}
        supervisor = OperationResult('completed', 'run', 'supervisor', 'supervisor', 'run:supervisor:1',
            outputs={'watchdog_decision': self.decision, 'diagnostic_repairs': [self.repair_ref]})
        self.episode = {'incident_id': 'watchdog-a', 'supervisor_result': supervisor.to_dict()}

    def invoke(self):
        return revive(self.owner, self.snapshot, self.header, self.app, self.episode,
                      self.commands['a'], self.outcomes['a'], self.decision)

    def test_real_group_consumer_dispatches_fresh_coder_with_supervisor_instruction(self):
        deadline, used = self.header['deadline_epoch'], self.app['agent_assignments']
        self.assertTrue(self.invoke())
        hold = coder_revival.state(self.group)['holds']['a']
        self.assertEqual('ready', hold['status'])
        self.assertEqual({'b': self.commands['b'].command_id}, hold['dependency_executions'])
        self.assertIn(self.commands['a'].command_id, self.app['processed'])
        operations = self.owner._flowthrough_group_decision(self.snapshot, self.header, self.app)
        fresh = next(operation for operation in operations if operation['kind'] == 'new_attempt')
        payload = unpack_input(self.root, fresh['command']['payload'])
        command = OperationInput.from_dict(payload)
        self.assertEqual('run:coder.g1.a:2', command.command_id)
        self.assertEqual(self.decision['instruction'], command.payload['coder_revival']['instruction'])
        self.assertEqual('run:supervisor:1', command.payload['coder_revival']['planner_execution_id'])
        self.assertEqual([self.repair_ref], command.payload['diagnostic_repair_refs'])
        self.assertEqual([{'path': 'artifacts/b.patch'}], command.payload['dependency_patches'])
        self.assertEqual(self.plan, command.artifact_refs['development_plan'])
        self.assertIn('-segment-', command.options['workspace'])
        self.assertEqual(deadline, command.options['deadline_epoch'])
        self.assertEqual(used + 1, self.app['agent_assignments'])
        self.assertEqual('running', hold['status'])
        self.assertEqual(self.outcomes['a'].to_dict(), self.snapshot['tasks'][command.task_id]['attempts'][0]['result']['value'])
        reference = command.payload['coder_revival']['request_ref']
        self.assertNotIn('sha256', reference)
        request = json.loads((self.root / reference['path']).read_text())
        self.assertEqual(self.commands['a'].command_id, request['results']['a']['command_id'])
        self.assertEqual(self.commands['b'].command_id, request['results']['b']['command_id'])

    def test_stale_active_or_live_producer_never_installs_ready_hold(self):
        cases = [('active', None), ('superseded', None), ('dependency_alive', 'b')]
        for kind, alive in cases:
            with self.subTest(kind=kind):
                snapshot = deepcopy(self.snapshot)
                app = deepcopy(self.app)
                attempt = snapshot['tasks'][self.commands['a'].task_id]['attempts'][-1]
                if kind == 'active':
                    attempt['state'] = 'running'
                elif kind == 'superseded':
                    attempt['command']['execution_id'] = 'run:coder.g1.a:2'
                before = deepcopy(app)
                with patch('modport.coder_revival._producer_alive',
                           side_effect=lambda header, result: result.get('command_id') == self.commands['b'].command_id if alive else False):
                    with self.assertRaises(ValueError):
                        revive(self.owner, snapshot, self.header, app, self.episode,
                               self.commands['a'], self.outcomes['a'], self.decision)
                self.assertEqual(before, app)

    def test_superseded_plan_and_unbound_supervisor_result_are_rejected(self):
        command = replace(self.commands['a'], artifact_refs={'development_plan': {'path': 'other.json'}})
        with self.assertRaisesRegex(ValueError, 'plan reference'):
            revive(self.owner, self.snapshot, self.header, self.app, self.episode,
                   command, self.outcomes['a'], self.decision)
        episode = deepcopy(self.episode)
        episode['supervisor_result']['outputs']['watchdog_decision']['instruction'] = 'different instruction'
        with self.assertRaisesRegex(ValueError, 'exact completed supervisor'):
            revive(self.owner, self.snapshot, self.header, self.app, episode,
                   self.commands['a'], self.outcomes['a'], self.decision)

    def test_replay_preserves_existing_ready_request_and_does_not_charge_assignment(self):
        self.assertTrue(self.invoke())
        before = deepcopy(self.app)
        self.assertTrue(self.invoke())
        self.assertEqual(before, self.app)

    def test_explicit_repair_releases_only_causally_resolved_planner_hold(self):
        revival = coder_revival.state(self.group)
        revival['terminal_error'] = 'coder_revival_planner_unavailable'
        revival['terminal_blocked'] = ['a']
        self.assertTrue(self.invoke())
        operations = self.owner._flowthrough_group_decision(self.snapshot, self.header, self.app)
        self.assertTrue(any(row['kind'] == 'new_attempt' and row['task_id'] == 'coder.g1.a'
                            for row in operations))

    def test_independent_unavailable_planner_blocks_remain_explicit(self):
        revival = coder_revival.state(self.group)
        revival.update(terminal_error='coder_revival_planner_unavailable', terminal_blocked=['a', 'c'])
        preserved = {'status': 'unavailable', 'result': {'command_id': 'failed-planner'}}
        revival['requests']['unrelated'] = preserved
        # Existing requests include host-owned references in diagnostic history.
        preserved.update(request={'results': {}}, request_ref={'path': 'artifacts/unrelated.json'})
        before = deepcopy(preserved)
        self.assertTrue(self.invoke())
        revival = coder_revival.state(self.group)
        self.assertEqual(['c'], revival['terminal_blocked'])
        self.assertEqual('coder_revival_planner_unavailable', revival['terminal_error'])
        self.assertEqual(before, revival['requests']['unrelated'])
        operations = self.owner._flowthrough_group_decision(self.snapshot, self.header, self.app)
        self.assertTrue(any(row['kind'] == 'new_attempt' and row['task_id'] == 'coder.g1.a'
                            for row in operations))

    def failed_planner(self):
        task_id = 'revival.g1.failed'
        request = {'request_id': task_id, 'generation': 1, 'base_commit': self.group['base'],
            'trigger_execution_ids': [self.commands['a'].command_id],
            'requested_tasks': ['a'], 'required_tasks': ['a'], 'tasks': deepcopy(self.group['tasks']),
            'results': {name: outcome.to_dict() for name, outcome in self.outcomes.items()},
            'attempts': {'a': 1}, 'prior_decisions': [], 'execution_evidence': {}}
        reference = {'path': 'artifacts/failed-planner-request.json', 'media_type': 'application/json'}
        (self.root / reference['path']).write_text(json.dumps(request))
        command = OperationInput('run', task_id, coder_revival.STAGE, 'run:' + task_id + ':1',
            str(self.root), payload={'revival_request': request},
            artifact_refs={'coder_revival_request': reference, 'development_plan': self.plan},
            options={'workflow_version': WORKFLOW_VERSION})
        result = OperationResult('failed', 'run', task_id, coder_revival.STAGE, command.command_id,
                                 error_code='provider_authentication_failed', detail='Raw provider failure')
        record = {'status': 'unavailable', 'request': request, 'request_ref': reference,
                  'result': result.to_dict(), 'feedback': result.detail}
        revival = coder_revival.state(self.group)
        revival.update(terminal_error='coder_revival_planner_unavailable', terminal_blocked=['a'])
        revival['requests'][task_id] = record
        coder_revival.record_result(self.group, 'a', self.outcomes['a'].to_dict())
        self.snapshot['tasks'][task_id] = {'attempts': [{'state': 'succeeded',
            'command': {'execution_id': command.command_id, 'payload': command.to_dict()},
            'result': {'value': result.to_dict()}}]}
        return command, record

    def test_fresh_planner_request_preserves_failure_and_actual_consumer_revives_coder(self):
        command, source = self.failed_planner()
        before = deepcopy(source)
        deadline, used = self.header['deadline_epoch'], self.app['agent_assignments']
        self.assertTrue(restart_planner(self.snapshot, self.app, command, self.decision, self.episode))
        restart = self.episode['planner_restart']
        revival = coder_revival.state(self.group)
        self.assertNotEqual(command.task_id, restart['task_id'])
        self.assertEqual(before, revival['requests'][command.task_id])
        self.assertNotIn('terminal_error', revival)
        self.assertEqual(restart['task_id'], revival['pending'])
        from modport.revival_planning import _validate_request
        _validate_request(restart['payload']['revival_request'])
        self.assertEqual(self.decision['instruction'], restart['payload']['watchdog_recovery']['instruction'])
        self.assertNotIn('sha256', restart['artifact_refs']['coder_revival_request'])
        operations = self.owner._schedule(self.snapshot, self.header, self.app, coder_revival.STAGE,
            task_id=restart['task_id'], dependencies=[], activate=False,
            payload=restart['payload'], artifact_overrides=restart['artifact_refs'])
        added = next(row for row in operations if row['kind'] == 'add_task')
        planner = OperationInput.from_dict(unpack_input(self.root, added['command']['payload']))
        self.assertEqual(deadline, planner.options['deadline_epoch'])
        result = OperationResult('completed', planner.run_id, planner.task_id, planner.stage_id,
            planner.command_id, outputs={'revival_decision': {'reason': 'Provider repaired; retained dependency conflict requires correction.',
                'decisions': [{'task_id': 'a', 'action': 'resume',
                    'instruction': self.decision['instruction'], 'wait_for': [], 'reuse_partial': True}]}})
        self.snapshot['tasks'][planner.task_id] = {'attempts': [{'state': 'succeeded',
            'command': {'execution_id': planner.command_id, 'payload': planner.to_dict()},
            'result': {'value': result.to_dict()}}]}
        operations = self.owner._flowthrough_group_decision(self.snapshot, self.header, self.app)
        coder = next(row for row in operations if row['kind'] == 'new_attempt' and row['task_id'] == 'coder.g1.a')
        fresh = OperationInput.from_dict(unpack_input(self.root, coder['command']['payload']))
        self.assertEqual(self.decision['instruction'], fresh.payload['coder_revival']['instruction'])
        self.assertEqual(planner.command_id, fresh.payload['coder_revival']['planner_execution_id'])
        self.assertEqual(before, self.group['revival']['requests'][command.task_id])
        self.assertEqual(used + 2, self.app['agent_assignments'])

    def test_planner_recovery_replay_and_stale_attempt(self):
        command, source = self.failed_planner()
        self.assertTrue(restart_planner(self.snapshot, self.app, command, self.decision, self.episode))
        before = deepcopy(self.app)
        self.assertTrue(restart_planner(self.snapshot, self.app, command, self.decision, self.episode))
        self.assertEqual(before, self.app)
        self.snapshot['tasks'][command.task_id]['attempts'][-1]['command']['execution_id'] = 'newer-attempt'
        with self.assertRaisesRegex(ValueError, 'active or superseded'):
            restart_planner(self.snapshot, self.app, command, self.decision, self.episode)


if __name__ == '__main__':
    unittest.main()
