"""Current supervision dispatch and actual goal consumers, without a model call."""
from dataclasses import replace
import json
from pathlib import Path
import subprocess
import unittest
from unittest.mock import patch

from modport.contracts import OperationInput
from modport.evidence import atomic_json, file_digest, verified_path
from modport.handlers import SupervisorHandler, _result
from modport.payload_storage import unpack_input
from modport.workflow import WorkflowDefinition
import test_planning_operations
import test_development
import test_handlers


class DeepSupervisionTests(unittest.TestCase):
    def setUp(self):
        self.f = test_planning_operations.PlanningPolicyTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.f.header['definition'] = WorkflowDefinition(self.f.header['request']).to_dict()
        self.root = Path(self.f.header['run_dir'])

    def seed(self):
        f = self.f
        for index in range(1, 6):
            command = OperationInput('policy', f'b{index}', 'background', f'policy:b{index}:1', str(self.root),
                options={'workflow_version': 26, 'agent_assignment': index})
            result = _result(command, 'completed')
            f.snapshot['tasks'][command.task_id] = {'attempts': [{'state': 'succeeded',
                'command': {'execution_id': command.command_id, 'payload': command.to_dict()},
                'result': {'value': result.to_dict()}}]}
        f.app['agent_assignments'] = 5

    def commands(self, changes):
        return [OperationInput.from_dict(unpack_input(str(self.root), item['command']['payload']))
                for item in changes if item['kind'] in {'add_task', 'new_attempt'}]

    def test_current_public_decision_dispatches_supervision_and_business_without_gate(self):
        self.seed()
        changes = self.f.settle('target_build', status='failed', error_code='target_contract_failed')
        commands = self.commands(changes)
        self.assertEqual({'supervisor', 'code_review'}, {cmd.stage_id for cmd in commands})
        self.assertEqual(7, self.f.app['agent_assignments'])
        self.assertEqual([5], self.f.app['supervision']['scheduled_windows'])
        self.assertFalse(self.f.app['stop_reason'])
        again, _ = self.f.operations._decision(self.f.snapshot, self.f.header)
        self.assertFalse(any(cmd.stage_id == 'supervisor' for cmd in self.commands(again)))

    def test_queued_task_input_and_failed_supervisor_evidence_remain_readable(self):
        self.seed()
        last = self.f.snapshot['tasks']['b5']['attempts'][0]
        last['state'] = 'planned'
        last.pop('result')
        last['command']['payload']['payload'] = {'development_task': {'id': 'queued', 'objective': 'Preserve queued context'}}
        command, = self.commands(self.f.operations._supervision_decision(self.f.snapshot, self.f.header, self.f.app))
        record = command.payload['supervision_packet']['attempts'][-1]
        self.assertEqual('planned', record['state'])
        queued_input = json.loads(verified_path(self.root, record['artifact_refs']['dispatched_input']).read_text())
        self.assertEqual('Preserve queued context', queued_input['payload']['development_task']['objective'])
        output = self.root / 'artifacts/failed-supervisor.log'
        output.write_text('Cannot connect to API')
        output_ref = {'path': output.relative_to(self.root).as_posix(), 'sha256': file_digest(output)}
        result = _result(command, 'failed', detail='Cannot connect to API', error_code='agent_runtime_failed',
                         outputs={'artifact_refs': {'execution_log': output_ref}})
        self.f.snapshot['tasks'][command.task_id] = {'attempts': [{'state': 'succeeded',
            'command': {'execution_id': command.command_id, 'payload': command.to_dict()},
            'result': {'value': result.to_dict()}}]}
        self.f.operations._supervision_decision(self.f.snapshot, self.f.header, self.f.app)
        prior = self.f.app['supervision']['results'][-1]
        self.assertEqual(command.command_id, prior['execution_id'])
        self.assertEqual('Cannot connect to API', prior['detail'])
        self.assertEqual(output_ref, prior['artifact_refs']['execution_log'])
        self.assertIn('dispatched_input', prior['artifact_refs'])

    def test_cancel_and_exhausted_budget_cannot_launch_supervision(self):
        self.seed()
        changes, _ = self.f.operations._decision(self.f.snapshot, self.f.header, stop_reason='user_cancelled')
        self.assertEqual([], self.commands(changes))
        self.f.header['request']['budget']['max_agent_assignments'] = 5
        changes, app = self.f.operations._decision(self.f.snapshot, self.f.header)
        self.assertEqual([], self.commands(changes))
        self.assertEqual([], app['supervision']['scheduled_windows'])
        self.assertEqual('agent_assignment_budget_exhausted', app['terminal_reason'])

    def test_v25_keeps_supervision_disabled(self):
        self.seed()
        self.f.header['definition'] = WorkflowDefinition(self.f.header['request'], version=25).to_dict()
        changes = self.f.settle('target_build', status='failed', error_code='target_contract_failed')
        self.assertEqual(['code_review'], [cmd.stage_id for cmd in self.commands(changes)])

    def test_upgrade_preserves_frozen_v25_and_selects_current_supervision(self):
        from modport.workflow_upgrade import validate_upgrade_definition
        old = WorkflowDefinition(self.f.header['request'], version=25).to_dict()
        current = validate_upgrade_definition({**self.f.header, 'definition': old})
        self.assertNotIn('supervision_policy', old)
        self.assertEqual(25, old['workflow_version'])
        self.assertEqual(28, current['workflow_version'])
        self.assertTrue(next(s['agent'] for s in current['stages'] if s['stage_id'] == 'supervisor'))

    def test_upgrade_accepts_exact_v26_and_adds_current_validation_policy(self):
        from modport.workflow_upgrade import validate_upgrade_definition
        source = WorkflowDefinition(self.f.header['request'], version=26).to_dict()
        current = validate_upgrade_definition({**self.f.header, 'definition': source})
        self.assertEqual(26, source['workflow_version'])
        self.assertNotIn('validation_policy', source)
        self.assertEqual(28, current['workflow_version'])
        self.assertEqual('full', current['validation_policy']['scope'])

        altered = dict(source)
        altered['supervision_policy'] = {**source['supervision_policy'],
                                         'business_assignment_interval': 6}
        with self.assertRaisesRegex(ValueError, 'exact supported upgrade source'):
            validate_upgrade_definition({**self.f.header, 'definition': altered})

    def test_late_supervisor_cannot_overwrite_a_revision_it_did_not_read(self):
        from modport.supervised_goals import prepare, collect, target_key
        self.seed()
        plan = self.root / 'artifacts/plan.json'
        atomic_json(plan, {'tasks': []})
        ref = {'path': 'artifacts/plan.json', 'sha256': file_digest(plan)}
        task = {'id': 'fix', 'objective': 'Original objective'}
        key = target_key(task, ref)
        target = {'key': key, 'task': task, 'plan_ref': ref, 'source_execution_id': None}
        self.f.app['supervision']['scheduled_windows'] = [5, 10]
        accepted = None
        for window in (5, 10):
            # Both frozen commands refer to the same original document.
            command = OperationInput('policy', f'supervisor.window.{window}', 'supervisor',
                f'policy:supervisor.window.{window}:1', str(self.root),
                options={'workflow_version': 26}, payload={
                    'supervision_window': window, 'supervised_goal_targets': [target]})
            prepared = prepare(command)
            (self.root / prepared.options['workspace'] / 'goals' / (key + '.md')).write_text(f'Revision {window}')
            result = collect(prepared, _result(prepared, 'completed'))
            self.f.snapshot['tasks'][command.task_id] = {'attempts': [{'state': 'succeeded',
                'command': {'execution_id': command.command_id, 'payload': command.to_dict()},
                'result': {'value': result.to_dict()}}]}
            self.f.operations._supervision_decision(self.f.snapshot, self.f.header, self.f.app)
            current = self.f.app['supervision']['goal_revisions'][key]['revision_ref']
            if window == 5:
                accepted = current
            else:
                self.assertEqual(accepted, current)
                self.assertIn('superseded', self.f.app['supervision']['results'][-1]['revision_diagnostics'][0])

    def test_supervisor_real_handler_prompt_edits_goal_and_next_dispatch_binds_revision(self):
        from modport.supervised_goals import target_key
        from modport.prompt_compressor import PromptCompressor
        self.seed()
        plan = self.root / 'artifacts/plan.json'
        atomic_json(plan, {'tasks': []})
        ref = {'path': 'artifacts/plan.json', 'sha256': file_digest(plan), 'media_type': 'application/json'}
        task = {'id': 'fix', 'objective': 'Fix the API error', 'owned_paths': ['src'],
                'dependencies': [], 'acceptance': ['Preserve behavior']}
        key = target_key(task, ref)
        fixture = test_handlers.HandlerTests._command(self.root, 'supervisor')
        commands = self.commands(self.f.operations._supervision_decision(self.f.snapshot, self.f.header, self.f.app))
        command = replace(commands[0], artifact_refs=fixture.artifact_refs,
            payload={**commands[0].payload, 'supervised_goal_targets': [
                {'key': key, 'task': task, 'plan_ref': ref, 'source_execution_id': None}]})
        (self.root / 'worktree').mkdir()
        (self.root / 'worktree' / 'Example.java').write_text('class Example {}')
        def execute(**kwargs):
            self.assertFalse(kwargs['read_only'])
            self.assertIsNotNone(kwargs['planning_prompt'])
            instructions = self.root / 'artifacts/executions' / command.command_id / 'task-instructions.execute.json'
            body = json.loads(instructions.read_text())
            self.assertIn('raw', body['task'])
            self.assertIn('goals/*.md', body['task'])
            self.assertNotIn('do not use tools or read external files', kwargs['prompt'])
            documents = list((kwargs['cwd'] / 'goals').glob('*.md'))
            self.assertEqual(1, len(documents))
            documents[0].write_text('Fix the API error using the exact target signatures; verify the original failing call.\n')
            result = subprocess.CompletedProcess(['opencode'], 0,
                json.dumps({'type':'item.completed','item':{'type':'agent_message','text':'Cause: wrong target API.'}}))
            result.dialogue_metadata = {}
            return result
        compressor = PromptCompressor(catalog={'models': [{'slug': 'gpt-6-luna', 'context_window': 272000,
                                                            'variants': {'max': {}}}]})
        with patch('modport.handlers.PromptCompressor.from_environment', return_value=compressor), \
             patch('modport.opencode_agent.run_agent', side_effect=execute):
            outcome = SupervisorHandler()(command)
        self.assertEqual('completed', outcome.status, outcome.detail)
        self.assertEqual(1, len(outcome.outputs['supervised_goal_revisions']))
        self.f.snapshot['tasks'][command.task_id] = {'attempts': [{'state': 'succeeded',
            'command': {'execution_id': command.command_id, 'payload': command.to_dict()},
            'result': {'value': outcome.to_dict()}}]}
        self.f.operations._supervision_decision(self.f.snapshot, self.f.header, self.f.app)
        changes = self.f.operations._schedule(self.f.snapshot, self.f.header, self.f.app, 'coder',
            task_id='coder.fix', dependencies=[], payload={'development_task': task},
            artifact_overrides={'development_plan': ref})
        dispatched, = self.commands(changes)
        revision = dispatched.artifact_refs['supervised_goal_revision']
        published = json.loads(verified_path(self.root, revision).read_text())
        self.assertEqual(command.command_id, published['supervisor_execution_id'])
        self.assertEqual('Fix the API error', published['original_objective'])


class GoalConsumerTests(unittest.TestCase):
    def test_revision_survives_goal_preparation_and_reaches_real_coder_handler(self):
        from modport.goal_planning import GoalPreparationHandler
        from modport.supervised_goals import prepare, collect, target_key
        fixture = test_development.IsolatedDevelopmentTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        coder = fixture.command('coder', 'a')
        task = coder.payload['development_task']
        plan = coder.artifact_refs['development_plan']
        supervisor = OperationInput('run', 'supervisor', 'supervisor', 'run:supervisor:1', str(fixture.root),
            payload={'supervised_goal_targets': [{'key': target_key(task, plan), 'task': task,
                'plan_ref': plan, 'source_execution_id': None}]}, options={'workflow_version': 26})
        supervisor = prepare(supervisor)
        document, = (fixture.root / supervisor.options['workspace'] / 'goals').glob('*.md')
        revised = 'Update a using the observed target signatures; retain the behavior witness.'
        document.write_text(revised)
        publication = collect(supervisor, _result(supervisor, 'completed'))
        revision = publication.outputs['supervised_goal_revisions'][0]['revision_ref']
        prep = replace(coder, stage_id='goal_prepare', command_id='run:goal-a:1', task_id='goal-a',
            options={'workflow_version': 26},
            payload={'development_task': task, 'planning_context': {}, 'goal_scope': 'migration', 'goal_generation': 1},
            artifact_refs={**coder.artifact_refs, 'supervised_goal_revision': revision})
        report = fixture.root / 'artifacts/goal-context.md'
        report.write_text('Verify the exact failing source location in a.txt.')
        def author(handler, command):
            self.assertIn(revised, handler.prompt)
            return _result(command, 'completed', outputs={'last_message': report.relative_to(fixture.root).as_posix()})
        with patch('modport.handlers.CodexStageHandler.__call__', author):
            prepared = GoalPreparationHandler()(prep)
        self.assertEqual('completed', prepared.status, prepared.detail)
        goal_ref = prepared.outputs['artifact_refs']['coder_goal']
        coder = replace(coder, options={**coder.options, 'workflow_version': 26},
            payload={**coder.payload, 'planning_context': {}},
            artifact_refs={**coder.artifact_refs, 'coder_goal': goal_ref, 'supervised_goal_revision': revision})
        def execute(handler, command):
            self.assertIn(revised, handler.native_goal['objective'])
            self.assertIn(report.read_text(), handler.native_goal['objective'])
            self.assertEqual(task['acceptance'], handler.native_goal['acceptance'])
            (fixture.root / command.options['workspace'] / 'a.txt').write_text('corrected\n')
            return _result(command, 'completed')
        with patch('modport.handlers.CodexStageHandler.__call__', execute):
            result = fixture.registry['coder'](coder)
        self.assertEqual('completed', result.status, result.detail)
        receipt = json.loads((fixture.root / 'artifacts/executions' / coder.command_id /
                              'supervised-goal-application.json').read_text())
        self.assertEqual(coder.command_id, receipt['consumer_execution_id'])
        self.assertEqual(revision, receipt['revision_ref'])

    def test_current_coder_preserves_context_from_prepared_goal(self):
        fixture = test_development.IsolatedDevelopmentTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        command = fixture.command('coder', 'a')
        objective = 'Update a\n\nCoder context:\nUse the exact target API, preserving the known witness.'
        goal_path = fixture.root / 'artifacts/prepared-goal.json'
        atomic_json(goal_path, {'objective': objective})
        command = replace(command, options={**command.options, 'workflow_version': 26},
            payload={**command.payload, 'planning_context': {}},
            artifact_refs={**command.artifact_refs, 'coder_goal': {
                'path': goal_path.relative_to(fixture.root).as_posix(), 'sha256': file_digest(goal_path)}})
        def execute(handler, cmd):
            self.assertTrue(handler.native_goal['objective'].endswith(objective))
            self.assertIn('exact target API', handler.prompt)
            (fixture.root / cmd.options['workspace'] / 'a.txt').write_text('updated\n')
            return _result(cmd, 'completed')
        with patch('modport.handlers.CodexStageHandler.__call__', execute):
            outcome = fixture.registry['coder'](command)
        self.assertEqual('completed', outcome.status, outcome.detail)
