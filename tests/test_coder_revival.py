"""Current-policy routing of failure and dependency-settled planner requests."""
from pathlib import Path
import tempfile
import time
import unittest

from modport import coder_revival
from modport.contracts import OperationInput, OperationResult
from modport.evidence import atomic_json, seal_ref
from modport.memory_admission import MemorySnapshot
from modport.models import Budget, MigrationRequest
from modport.operations import MigrationOperations
from modport.payload_storage import unpack_input
from modport.workflow import WORKFLOW_VERSION, WorkflowDefinition


class CoderRevivalTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        request = MigrationRequest('revival', 'https://example.invalid/synthetic.git', '1', '2',
                                   budget=Budget(max_agent_assignments=100), max_parallel_coders=4)
        self.ops = MigrationOperations(memory_probe=lambda: MemorySnapshot(64 * 1024**3, 64 * 1024**3, 'test'))
        self.header = {'run_id': 'revival', 'request': request.to_dict(),
                       'definition': WorkflowDefinition(request.to_dict()).to_dict(),
                       'deadline_epoch': time.time() + 600, 'run_dir': str(self.root), 'initial_refs': {},
                       'prior_findings': [], 'registry_revision': 'a' * 64, 'rubric_sha256': 'b' * 64}
        self.assertGreaterEqual(WORKFLOW_VERSION, 25)
        self.snapshot = {'run_id': 'revival', 'revision': 1, 'state': 'running', 'tasks': {}, 'waits': {}}
        self.app = self.ops._new_application()
        tasks = [{'id': name, 'objective': 'Implement ' + name, 'owned_paths': [name + '.txt'],
                  'dependencies': [], 'acceptance': ['Deliver ' + name]} for name in ('A', 'B', 'C')]
        self.group = {'kind': 'development', 'generation': 1, 'tasks': tasks, 'base': 'a' * 40,
                      'parallel': True, 'members': [], 'scheduled': [], 'results': {},
                      'goal_scheduled': ['A', 'B', 'C'], 'entry_stage': 'implementation',
                      'goal_scope': 'migration', 'planning_context': {}, 'artifact_refs': {},
                      'execution_payload': {}}
        for task in tasks:
            self.group['results']['goal.g1.' + task['id']] = {
                'outputs': {'artifact_refs': {'coder_goal': self.ref(task['id'] + '-goal')}}}
        self.app['active_group'] = self.group

    def ref(self, name):
        path = self.root / 'artifacts' / (name + '.json')
        atomic_json(path, {'synthetic': name})
        return seal_ref(self.root, {'path': path.relative_to(self.root).as_posix()}, execution_id=name)

    def seed_coder(self, name, *, status='running', business='completed', error=None):
        identifier = coder_revival.coder_id(self.group, name)
        previous = self.snapshot['tasks'].get(identifier, {}).get('attempts', [])
        command = OperationInput('revival', identifier, 'coder', f'revival:{identifier}:{len(previous) + 1}',
            str(self.root), payload={'development_task': next(t for t in self.group['tasks'] if t['id'] == name)})
        attempt = {'state': status, 'command': {'execution_id': command.command_id, 'payload': command.to_dict()}}
        if status == 'succeeded':
            result = OperationResult(business, 'revival', identifier, 'coder', command.command_id,
                outputs={'development_task_id': name}, detail='Raw diagnostic context', error_code=error)
            attempt['result'] = {'value': result.to_dict()}
        self.snapshot['tasks'].setdefault(identifier, {'attempts': []})['attempts'].append(attempt)
        if identifier not in self.group['members']:
            self.group['members'].append(identifier)
        if name not in self.group['scheduled']:
            self.group['scheduled'].append(name)
        return attempt

    def turn(self):
        changes = self.ops._flowthrough_group_decision(self.snapshot, self.header, self.app)
        commands = []
        for change in changes:
            if change['kind'] not in {'add_task', 'new_attempt'}:
                continue
            payload = unpack_input(self.root, change['command']['payload'])
            command = OperationInput.from_dict(payload)
            commands.append(command)
            self.snapshot['tasks'].setdefault(command.task_id, {'attempts': []})['attempts'].append({
                'state': 'running', 'command': {**change['command'], 'payload': payload}})
        self.snapshot['revision'] += 1
        return commands, changes

    def finish(self, command, *, decision=None, business='completed', error=None):
        result = OperationResult(business, command.run_id, command.task_id, command.stage_id,
            command.command_id, outputs={'revival_decision': decision} if decision is not None else {},
            error_code=error, detail='Concrete execution result')
        self.snapshot['tasks'][command.task_id]['attempts'][-1].update(
            state='succeeded', result={'value': result.to_dict()})

    @staticmethod
    def decision(name, action='resume', wait_for=()):
        row = {'task_id': name, 'action': action, 'instruction': 'Read raw error and correct the diagnosed input mismatch',
               'wait_for': list(wait_for)}
        if action == 'resume':
            row['reuse_partial'] = True
        return {'reason': 'Concrete dependency and execution evidence', 'decisions': [row]}

    def test_failed_a_waits_for_b_then_sdk_requests_planner_before_resuming(self):
        self.seed_coder('A', status='succeeded', business='failed', error='agent_failed')
        self.seed_coder('B')
        self.seed_coder('C')
        first, _ = self.turn()
        planner = next(c for c in first if c.stage_id == coder_revival.STAGE)
        self.assertEqual(['A'], planner.payload['revival_request']['required_tasks'])
        self.assertEqual([], self.turn()[0])
        self.finish(planner, decision=self.decision('A', 'wait', ['B']))
        self.assertEqual([], self.turn()[0])
        b = OperationInput.from_dict(self.snapshot['tasks']['coder.g1.B']['attempts'][-1]['command']['payload'])
        self.finish(b)
        second, _ = self.turn()
        replanner = next(c for c in second if c.stage_id == coder_revival.STAGE)
        self.assertNotEqual(planner.task_id, replanner.task_id)
        self.assertIn(b.command_id, replanner.payload['revival_request']['trigger_execution_ids'])
        self.assertFalse(any(c.stage_id == 'coder' for c in second))
        self.finish(replanner, decision=self.decision('A'))
        resumed, _ = self.turn()
        coder = next(c for c in resumed if c.stage_id == 'coder')
        self.assertEqual('revival:coder.g1.A:2', coder.command_id)
        self.assertIn('-segment-', coder.options['workspace'])
        self.assertEqual(replanner.task_id, coder.payload['coder_revival']['request_id'])
        self.assertEqual(2, len(self.snapshot['tasks']['coder.g1.A']['attempts']))
        self.assertEqual([], self.turn()[0])

    def test_third_and_fourth_continuation_are_not_blocked_by_fixed_retry_cap(self):
        self.seed_coder('A', status='succeeded', business='failed', error='agent_failed')
        self.seed_coder('B')
        self.seed_coder('C')
        previous_request = None
        for number in range(2, 6):
            planner = next(c for c in self.turn()[0] if c.stage_id == coder_revival.STAGE)
            if previous_request:
                from modport.evidence import read_json, verified_path
                prior = planner.payload['revival_request']['prior_decisions'][-1]
                archived = read_json(verified_path(self.root, prior['request_ref']))
                self.assertEqual(previous_request, archived['request_id'])
                self.assertTrue(archived['results']['A']['command_id'].endswith(':' + str(number - 2)))
            previous_request = planner.task_id
            self.finish(planner, decision=self.decision('A'))
            coder = next(c for c in self.turn()[0] if c.stage_id == 'coder')
            self.assertTrue(coder.command_id.endswith(':' + str(number)))
            self.finish(coder, business='failed', error='agent_failed')
        self.assertEqual(8, self.app['agent_assignments'])
        self.assertEqual({}, self.app['rounds'])

    def test_log_origin_failure_reaches_actual_bounded_artifact_reader(self):
        import time
        from modport.opencode_shell_mcp import _read_run_artifact
        raw = 'Traceback: parser rejected dependency revision 17 at input.py:42\n'
        log = self.root / 'logs' / 'failed-A.log'
        log.parent.mkdir()
        log.write_text(raw)
        attempt = self.seed_coder('A', status='succeeded', business='failed', error='agent_failed')
        attempt['result']['value']['outputs']['artifact_refs'] = {
            'raw_log': seal_ref(self.root, {'path': 'logs/failed-A.log', 'media_type': 'text/plain'},
                                execution_id=attempt['result']['value']['command_id'])}
        workspace_report = {'path': '.modport/goal-reports/A.json', 'sha256': 'c' * 64}
        attempt['result']['value']['outputs']['native_goal'] = {'acceptance_report': workspace_report}
        self.seed_coder('B')
        self.seed_coder('C')
        planner = next(c for c in self.turn()[0] if c.stage_id == coder_revival.STAGE)
        request = planner.payload['revival_request']
        ref = request['results']['A']['outputs']['artifact_refs']['raw_log']
        self.assertTrue(ref['path'].startswith('artifacts/repair-evidence/'))
        self.assertEqual(ref, planner.artifact_refs['revival:A:raw_log'])
        read = _read_run_artifact({'root': str(self.root), 'deadline_epoch': time.time() + 60}, ref['path'])
        self.assertEqual(raw, read['content_utf8'])
        self.assertEqual(raw, log.read_text())
        self.assertEqual(workspace_report, request['results']['A']['outputs']['native_goal']['acceptance_report'])
        self.assertEqual(ref['path'], request['execution_evidence']['A']['result']['value']
                         ['outputs']['artifact_refs']['raw_log']['path'])

    def test_settled_timeout_receipt_reaches_planner_without_marking_coder_success(self):
        from modport.evidence import read_json, verified_path

        attempt = self.seed_coder('A', status='timed_out')
        self.seed_coder('B')
        self.seed_coder('C')
        receipt = {'execution_id': attempt['command']['execution_id'],
                   'response': {'status': 'failed', 'detail': 'inner stage diagnostic'}}
        path = self.root / 'artifacts' / 'executions' / 'settled-receipt.json'
        atomic_json(path, receipt)
        self.app['settled_timeout_diagnoses'] = {'coder.g1.A': {
            'action': 'diagnose', 'execution_id': attempt['command']['execution_id'],
            'reason': 'settled_coder_handler_timeout',
            'stage_receipt_ref': seal_ref(self.root, {
                'path': path.relative_to(self.root).as_posix(),
                'media_type': 'application/json'}, execution_id=attempt['command']['execution_id']),
        }}

        commands, _ = self.turn()
        self.assertFalse(any(command.stage_id == 'coder' for command in commands))
        planner = next(command for command in commands if command.stage_id == coder_revival.STAGE)
        request = planner.payload['revival_request']
        self.assertEqual('failed', request['results']['A']['status'])
        self.assertEqual('execution_timed_out', request['results']['A']['error_code'])
        evidence = request['execution_evidence']['A']['settled_timeout_diagnosis']
        self.assertEqual('settled_coder_handler_timeout', evidence['proof']['reason'])
        ref = evidence['artifact_refs']['stage_receipt']
        self.assertEqual(ref, planner.artifact_refs['revival:A:stage_receipt'])
        self.assertEqual(receipt, read_json(verified_path(self.root, ref)))

    def test_redoing_b_invalidates_finished_a_and_waits_for_new_b_result(self):
        self.group['tasks'][0]['dependencies'] = ['B']
        self.seed_coder('A', status='succeeded')
        self.seed_coder('B', status='succeeded', business='failed', error='agent_failed')
        self.seed_coder('C')
        planner = next(c for c in self.turn()[0] if c.stage_id == coder_revival.STAGE)
        self.finish(planner, decision=self.decision('B'))
        resumed = self.turn()[0]
        b = next(c for c in resumed if c.stage_id == 'coder')
        self.assertEqual('coder.g1.B', b.task_id)
        self.assertEqual('waiting', self.group['revival']['holds']['A']['status'])
        self.assertNotIn('A', coder_revival.completed(self.group))
        self.finish(b)
        planner2 = next(c for c in self.turn()[0] if c.stage_id == coder_revival.STAGE)
        self.assertEqual(['A'], planner2.payload['revival_request']['required_tasks'])
        self.assertEqual('revival:coder.g1.A:1', planner2.payload['revival_request']['results']['A']['command_id'])

    def test_cycle_is_feedback_to_planner_and_never_dispatches_coder(self):
        self.group['tasks'][1]['dependencies'] = ['A']
        self.seed_coder('A', status='succeeded', business='failed', error='agent_failed')
        self.seed_coder('B')
        self.seed_coder('C')
        planner = next(c for c in self.turn()[0] if c.stage_id == coder_revival.STAGE)
        self.finish(planner, decision=self.decision('A', 'wait', ['B']))
        commands, _ = self.turn()
        self.assertEqual([coder_revival.STAGE], [c.stage_id for c in commands])
        self.assertIn('cyclic', commands[0].payload['revival_request']['prior_decisions'][-1]['feedback'])

    def test_stopping_a_does_not_cancel_independent_b(self):
        self.seed_coder('A', status='succeeded', business='failed', error='agent_failed')
        self.seed_coder('B')
        self.seed_coder('C')
        planner = next(c for c in self.turn()[0] if c.stage_id == coder_revival.STAGE)
        self.finish(planner, decision=self.decision('A', 'stop'))
        commands, operations = self.turn()
        self.assertEqual([], commands)
        self.assertFalse(any(row['kind'] in {'cancel', 'finish'} for row in operations))

    def test_wait_for_unstarted_task_cannot_be_combined_with_stopping_it(self):
        self.seed_coder('A', status='succeeded', business='failed', error='agent_failed')
        self.seed_coder('C')
        planner = next(c for c in self.turn()[0] if c.stage_id == coder_revival.STAGE)
        decision = self.decision('A', 'wait', ['B'])
        decision['decisions'] += self.decision('B', 'stop')['decisions']
        self.finish(planner, decision=decision)
        commands, _ = self.turn()
        replanner = next(c for c in commands if c.stage_id == coder_revival.STAGE)
        self.assertIn('stopped B', replanner.payload['revival_request']['prior_decisions'][-1]['feedback'])
        self.assertEqual('needs_plan', self.group['revival']['holds']['A']['status'])
        self.assertNotIn('B', self.group['revival']['holds'])

    def test_wait_cannot_be_stranded_by_stopped_transitive_prerequisite(self):
        self.group['tasks'][1]['dependencies'] = ['C']
        self.seed_coder('A', status='succeeded', business='failed', error='agent_failed')
        planner = next(c for c in self.turn()[0] if c.stage_id == coder_revival.STAGE)
        decision = self.decision('A', 'wait', ['B'])
        decision['decisions'] += self.decision('C', 'stop')['decisions']
        self.finish(planner, decision=decision)
        commands, _ = self.turn()
        replanner = next(c for c in commands if c.stage_id == coder_revival.STAGE)
        self.assertIn('blocked by a stopped prerequisite',
                      replanner.payload['revival_request']['prior_decisions'][-1]['feedback'])

    def test_changed_dependency_rejects_stale_planner_reply_before_dispatch(self):
        self.group['tasks'][0]['dependencies'] = ['B']
        self.seed_coder('A', status='succeeded', business='failed', error='agent_failed')
        self.seed_coder('B', status='succeeded')
        self.seed_coder('C')
        planner = next(c for c in self.turn()[0] if c.stage_id == coder_revival.STAGE)
        self.seed_coder('B', status='succeeded')
        self.finish(planner, decision=self.decision('A'))
        commands, _ = self.turn()
        self.assertEqual([coder_revival.STAGE], [c.stage_id for c in commands])
        self.assertIn('stale', commands[0].payload['revival_request']['prior_decisions'][-1]['feedback'])

    def test_live_dependency_producer_rejects_resume_using_its_result(self):
        from unittest.mock import patch
        self.group['tasks'][0]['dependencies'] = ['B']
        self.seed_coder('A', status='succeeded', business='failed', error='agent_failed')
        self.seed_coder('B', status='succeeded')
        self.seed_coder('C')
        planner = next(c for c in self.turn()[0] if c.stage_id == coder_revival.STAGE)
        self.finish(planner, decision=self.decision('A'))
        with patch('modport.coder_revival._producer_alive',
                   side_effect=lambda header, result: result.get('command_id') == 'revival:coder.g1.B:1'):
            commands, _ = self.turn()
        self.assertEqual([coder_revival.STAGE], [c.stage_id for c in commands])
        self.assertIn('still alive: B', commands[0].payload['revival_request']['prior_decisions'][-1]['feedback'])

    def test_planner_can_explicitly_use_failed_b_output_for_a(self):
        self.group['tasks'][0]['dependencies'] = ['B']
        self.seed_coder('B', status='succeeded', business='failed', error='agent_failed')
        self.seed_coder('C')
        planner = next(c for c in self.turn()[0] if c.stage_id == coder_revival.STAGE)
        decision = self.decision('B', 'stop')
        decision['decisions'] += self.decision('A')['decisions']
        self.finish(planner, decision=decision)
        commands, _ = self.turn()
        self.assertEqual(['coder.g1.A'], [c.task_id for c in commands if c.stage_id == 'coder'])
        self.assertEqual('failed', self.group['results']['coder.g1.B']['status'])

    def test_run_assignment_budget_is_not_reset_by_revival(self):
        self.header['request']['budget']['max_agent_assignments'] = 0
        self.seed_coder('A', status='succeeded', business='failed', error='agent_failed')
        self.seed_coder('B')
        self.seed_coder('C')
        commands, _ = self.turn()
        self.assertEqual([], commands)
        self.assertEqual(0, self.app['agent_assignments'])
        self.assertEqual('agent_assignment_budget_exhausted', self.app['stop_reason'])

    def test_integrity_failure_stops_without_requesting_revival(self):
        for error in ('coder_isolation_violation', 'locked_artifact_invalid',
                      'opencode_cleanup_unconfirmed'):
            with self.subTest(error=error):
                self.app = self.ops._new_application()
                self.group['results'].pop('coder.g1.A', None)
                self.app['active_group'] = self.group
                self.seed_coder('A', status='succeeded', business='blocked', error=error)
                commands, operations = self.turn()
                self.assertEqual([], commands)
                self.assertTrue(any(row['kind'] == 'finish' for row in operations))
                self.assertEqual(error, self.app['terminal_reason'])
                self.assertIsNone(self.group['revival']['pending'])

    def test_carried_failed_result_requests_planner_without_a_current_attempt(self):
        import time
        self.header['deadline_epoch'] = time.time() + 600
        self.group['results']['coder.g1.A'] = OperationResult(
            'blocked', 'previous-segment', 'coder.g1.A', 'coder', 'previous:coder.g1.A:1',
            outputs={'development_task_id': 'A'},
            error_code='dependency_patch_conflict', detail='Conflicting dependency edits').to_dict()
        self.group['scheduled'].append('A')
        self.seed_coder('B')
        self.seed_coder('C')
        commands, changes = self.turn()
        planner = next(c for c in commands if c.stage_id == coder_revival.STAGE)
        self.assertEqual(['A'], planner.payload['revival_request']['required_tasks'])
        self.assertEqual('previous:coder.g1.A:1',
                         planner.payload['revival_request']['results']['A']['command_id'])
        self.assertFalse(any(c.stage_id == 'coder' for c in commands))
        self.assertFalse(any(row['kind'] == 'finish' for row in changes))
        self.assertEqual([], self.turn()[0])

    def test_carried_integrity_failure_stops_before_planner_dispatch(self):
        import time
        self.header['deadline_epoch'] = time.time() + 600
        self.group['results']['coder.g1.A'] = OperationResult(
            'blocked', 'previous-segment', 'coder.g1.A', 'coder', 'previous:coder.g1.A:1',
            outputs={'development_task_id': 'A'}, error_code='coder_isolation_violation').to_dict()
        commands, changes = self.turn()
        self.assertEqual([], commands)
        self.assertTrue(any(row['kind'] == 'finish' for row in changes))
        self.assertEqual('coder_isolation_violation', self.app['terminal_reason'])

    def test_v24_preserves_diagnostic_dependency_flow_without_new_planner(self):
        self.header['definition'] = WorkflowDefinition(self.header['request'], version=24).to_dict()
        self.seed_coder('A', status='succeeded', business='failed', error='agent_failed')
        self.seed_coder('B')
        self.seed_coder('C')
        self.assertEqual([], self.turn()[0])
        self.assertNotIn('revival', self.group)


if __name__ == '__main__':
    unittest.main()
