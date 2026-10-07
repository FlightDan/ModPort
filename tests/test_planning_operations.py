"""Policy fixture and current v22 planning checks."""
import unittest
import tempfile
from unittest.mock import patch

from modport.contracts import OperationInput, OperationResult, json_copy
from modport.payload_storage import unpack_input
from modport.execution_plan import normalize_execution_plan
from modport.models import Budget, MigrationRequest
from modport.memory_admission import MemorySnapshot
from modport.operations import MigrationOperations
from modport.workflow import REPAIR_POLICY, REPAIR_ROUTE


class PlanningPolicyTests(unittest.TestCase):
    def setUp(self):
        self.run_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.run_directory.cleanup)
        self.evidence_mock = patch('modport.operations.snapshot_repair_evidence',
                                   side_effect=lambda root, value: json_copy(value))
        self.evidence_mock.start()
        self.addCleanup(self.evidence_mock.stop)
        request = MigrationRequest(
            'policy', 'https://example.invalid/mod.git', '1.20.1', '26.1.2',
            budget=Budget(max_agent_assignments=100), max_parallel_coders=2,
        )
        from modport.workflow import WorkflowDefinition
        self.header = {
            'request': request.to_dict(), 'definition': WorkflowDefinition(request.to_dict(), version=18).to_dict(),
            'deadline_epoch': None, 'run_dir': self.run_directory.name,
            'initial_refs': {'source': self.ref('source')}, 'prior_findings': [],
            'registry_revision': 'a' * 64, 'rubric_sha256': 'b' * 64,
        }
        self.header['definition']['workflow_version'] = 11  # shared fixture for historical inputs
        self.header['definition']['repair_policy'] = dict(REPAIR_POLICY)
        self.header['definition']['repair_route'] = dict(REPAIR_ROUTE)
        self.operations = MigrationOperations(memory_probe=lambda: MemorySnapshot(64 * 1024**3, 64 * 1024**3, "fixture"))
        self.snapshot = {'run_id': 'policy', 'state': 'running', 'tasks': {}, 'waits': {},
                         'application_state': self.operations._new_application()}

    @staticmethod
    def ref(name):
        return {'path': 'artifacts/' + name + '.json', 'sha256': 'c' * 64,
                'media_type': 'application/json'}

    @property
    def app(self):
        return self.snapshot['application_state']

    def settle(self, stage=None, *, status='completed', outputs=None, error_code=None):
        """Settle one business result, then apply only the returned policy state."""
        stage = stage or self.app['active_stage']
        if stage not in self.snapshot['tasks']:
            operation = OperationInput('policy', stage, stage, 'policy:' + stage + ':1', self.header['run_dir'])
            self.snapshot['tasks'][stage] = {'attempts': [{
                'state': 'running', 'command': {'execution_id': operation.command_id,
                                              'payload': operation.to_dict()}}]}
        attempt = self.snapshot['tasks'][stage]['attempts'][-1]
        operation = OperationInput.from_dict(attempt['command']['payload'])
        result = OperationResult(status, operation.run_id, operation.task_id, operation.stage_id,
                                 operation.command_id, outputs or {}, error_code=error_code)
        attempt.update(state='succeeded', result={'value': result.to_dict()})
        self.app['active_stage'] = stage
        changes, app = self.operations._decision(self.snapshot, self.header)
        self.snapshot['application_state'] = app
        for change in changes:
            if change['kind'] in {'add_task', 'new_attempt'}:
                logical_payload = unpack_input(self.header['run_dir'], change['command']['payload'])
                command = OperationInput.from_dict(logical_payload)
                attempt = {'state': 'running', 'command': {**change['command'], 'payload': logical_payload}}
                if command.stage_id == 'supervisor':
                    decision = {'schema_version': 1, 'decision': 'continue',
                        'reason': 'fixture supervision permits policy testing to continue',
                        'evidence_execution_ids': command.payload['supervision_packet']['evidence_execution_ids'],
                        'process_improvements': []}
                    result = OperationResult('completed', command.run_id, command.task_id,
                        command.stage_id, command.command_id,
                        outputs={'supervisor_decision': decision})
                    attempt.update(state='succeeded', result={'value': result.to_dict()})
                self.snapshot['tasks'].setdefault(change['task_id'], {'attempts': []})['attempts'].append(attempt)
        return changes

    @staticmethod
    def scheduled(changes):
        return [OperationInput.from_dict(unpack_input(change['command']['payload']['run_dir'],
                                                     change['command']['payload'])) for change in changes
                if change['kind'] in {'add_task', 'new_attempt'}
                and change['command']['payload']['stage_id'] != 'supervisor']

    def seed_effective(self, stage, outputs=None):
        result = OperationResult('completed', 'policy', stage, stage, 'old:' + stage,
                                 outputs or {'artifact_refs': {stage: self.ref('old-' + stage)}})
        self.app['effective'][stage] = result.to_dict()

    def test_current_markdown_plan_dependencies_reach_sdk_coder_tasks(self):
        import json
        from modport.workflow import WORKFLOW_VERSION, WorkflowDefinition
        self.header['definition'] = WorkflowDefinition(self.header['request']).to_dict()
        report = '# Implementation plan\n```json\n' + json.dumps({'tasks': [
            {'id': 'MP-01', 'objective': 'Create shared API', 'dependencies': [],
             'owned_paths': ['src/common/']},
            {'id': 'MP-02', 'objective': 'Implement client adapter',
             'dependencies': ['MP-01'], 'owned_paths': ['src/client/']},
        ]}) + '\n```\n## Evidence\nTarget API sources.\n'
        plan = normalize_execution_plan(report, workflow_version=WORKFLOW_VERSION)
        tasks = plan['tasks']
        self.operations._start_development_group(self.header, self.app, 'implementation', {
            'development_tasks': tasks, 'development_base': 'f' * 40,
        })
        group = self.app['active_group']
        generation = group['generation']
        for task in tasks:
            task_id = task['id']
            group['goal_scheduled'].append(task_id)
            group['results'][f'goal.g{generation}.{task_id}'] = {
                'outputs': {'artifact_refs': {'coder_goal': self.ref('goal-' + task_id)}}}

        def coder_command(changes):
            operation = next(item for item in changes if item['kind'] == 'add_task'
                             and item['command']['handler_id'] == 'modport.coder')
            payload = unpack_input(self.header['run_dir'], operation['command']['payload'])
            return OperationInput.from_dict(payload)

        first_operations = self.operations._group_decision(self.snapshot, self.header, self.app)
        first = coder_command(first_operations)
        self.assertEqual('coder.g1.MP-01', first.task_id)
        self.assertEqual('MP-01', first.payload['development_task']['id'])
        self.assertEqual([], first.payload['development_task']['dependencies'])

        group['results'][first.task_id] = {'outputs': {'artifact_refs': {
            'coder_patch': self.ref('patch-MP-01')}}}
        second_operations = self.operations._group_decision(self.snapshot, self.header, self.app)
        second = coder_command(second_operations)
        self.assertEqual('coder.g1.MP-02', second.task_id)
        self.assertEqual('MP-02', second.payload['development_task']['id'])
        self.assertEqual(['MP-01'], second.payload['development_task']['dependencies'])
        self.assertEqual([self.ref('patch-MP-01')], second.payload['dependency_patches'])
