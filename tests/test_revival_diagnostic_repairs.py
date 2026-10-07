"""Revival diagnosis exports private edits only after a usable decision."""
from dataclasses import replace
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from modport import coder_revival, diagnostic_repairs
from modport.contracts import OperationInput
from modport.handlers import _result
from modport.revival_planning import CoderRevivalPlannerHandler
from modport.workflow import WORKFLOW_VERSION


class RevivalDiagnosticRepairTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source_relative = 'workspaces/development/g1/A-segment-existing'
        self.source = self.root / self.source_relative / 'src' / 'A.java'
        self.source.parent.mkdir(parents=True)
        self.before = 'class A { int count = missing; }\n'
        self.after = 'class A { int count = 1; }\n'
        self.source.write_text(self.before)
        plan = self.root / 'artifacts' / 'plan.json'
        plan.parent.mkdir()
        plan.write_text('{}\n')
        self.plan_ref = {'path': 'artifacts/plan.json', 'media_type': 'application/json'}
        self.task = {'id': 'A', 'objective': 'Complete A', 'dependencies': []}
        self.previous = OperationInput(
            'run', 'coder.g1.A', 'coder', 'previous-coder', str(self.root),
            payload={'development_task': self.task}, options={'workspace': self.source_relative},
            artifact_refs={'development_plan': self.plan_ref})
        self.previous_result = _result(self.previous, 'failed', outputs={'development_task_id': 'A'})
        self.request = {'request_id': 'revival-request', 'generation': 1,
                        'base_commit': 'a' * 40, 'trigger_execution_ids': ['previous-coder'],
                        'requested_tasks': ['A'], 'required_tasks': ['A'], 'tasks': [self.task],
                        'results': {'A': self.previous_result.to_dict()}, 'attempts': {'A': 1},
                        'prior_decisions': [], 'execution_evidence': {}}
        self.header = {'run_id': 'run', 'run_dir': str(self.root),
                       'definition': {'workflow_version': WORKFLOW_VERSION}}
        self.snapshot = {'tasks': {'coder.g1.A': {'attempts': [{
            'state': 'succeeded', 'command': {'payload': self.previous.to_dict()}}]}}}
        self.group = {'generation': 1, 'goal_scope': 'migration',
                      'artifact_refs': {'development_plan': self.plan_ref}}
        self.targets = coder_revival._diagnostic_repair_targets(
            self.snapshot, self.header, self.group, self.request)
        self.command = OperationInput(
            'run', 'revival-request', 'coder_revival_plan', 'planner-execution', str(self.root),
            payload={'revival_request': self.request, 'goal_scope': 'migration',
                     'diagnostic_repair_targets': self.targets},
            options={'workflow_version': WORKFLOW_VERSION,
                     'agent_dialogue_policy': {'version': 1, 'turns': ['plan', 'execute']}},
            artifact_refs={'development_plan': self.plan_ref})

    def agent_reply(self, command, *, valid=True):
        decisions = [{'task_id': 'A', 'action': 'resume', 'instruction': 'The missing name is corrected; finish A.',
                      'wait_for': []}] if valid else []
        return _result(command, 'completed', outputs={
            'raw_report': json.dumps({'decisions': decisions, 'reason': 'A.java uses an undefined name.'}),
            'agent_dialogue': {'transport': 'opencode', 'turns': 2}})

    def test_producer_private_edit_exports_bound_refs_to_matching_resumed_task(self):
        self.assertGreaterEqual(WORKFLOW_VERSION, 40)
        self.assertEqual([{'task_id': 'A', 'plan_ref': self.plan_ref,
                           'source_workspace': self.source_relative,
                           'source_execution_id': 'previous-coder'}], self.targets)

        def agent(handler, command):
            self.assertFalse(handler.read_only)
            self.assertIn('diagnostic_repair_manifest', command.payload)
            workspace = self.root / command.options['workspace']
            self.assertNotEqual(self.source.parent.parent, workspace)
            copies = list(workspace.rglob('A.java'))
            self.assertEqual(1, len(copies))
            self.assertEqual(self.before, copies[0].read_text())
            copies[0].write_text(self.after)
            return self.agent_reply(command)

        with patch('modport.handlers.CodexStageHandler.__call__', agent):
            result = CoderRevivalPlannerHandler()(self.command)
        self.assertEqual('completed', result.status, result.detail)
        self.assertEqual(self.before, self.source.read_text())
        refs = result.outputs['diagnostic_repairs']
        self.assertEqual(1, len(refs))
        document = json.loads((self.root / refs[0]['path']).read_text())
        self.assertEqual([{'path': 'src/A.java', 'before': self.before, 'after': self.after}], document['files'])
        self.assertEqual('A', refs[0]['metadata']['task_id'])
        self.assertEqual(self.plan_ref, refs[0]['metadata']['plan_ref'])
        decision = json.loads((self.root / result.outputs['decision_artifact']['path']).read_text())
        self.assertEqual(refs, decision['diagnostic_repairs'])
        self.group['revival'] = {'holds': {'A': {'status': 'ready', 'request_id': 'revival-request',
            'instruction': 'Finish A', 'previous_result': self.previous_result.to_dict()}},
            'requests': {'revival-request': {'result': result.to_dict(), 'request_ref': self.plan_ref}},
            'pending': None, 'sequence': 1, 'seen': {}}
        payload = coder_revival.coder_payload(self.snapshot, self.header, self.group, 'A')
        self.assertEqual(refs, payload['diagnostic_repair_refs'])
        self.group['artifact_refs']['development_plan'] = {'path': 'artifacts/another-plan.json'}
        self.assertNotIn('diagnostic_repair_refs',
                         coder_revival.coder_payload(self.snapshot, self.header, self.group, 'A'))

    def test_invalid_decision_does_not_export_applicable_repair(self):
        def agent(handler, command):
            copy = next((self.root / command.options['workspace']).rglob('A.java'))
            copy.write_text(self.after)
            return self.agent_reply(command, valid=False)

        with patch('modport.handlers.CodexStageHandler.__call__', agent), \
                patch.object(diagnostic_repairs, 'collect', wraps=diagnostic_repairs.collect) as collect:
            result = CoderRevivalPlannerHandler()(self.command)
        self.assertEqual('revival_decision_invalid', result.error_code)
        collect.assert_not_called()
        self.assertNotIn('diagnostic_repairs', result.outputs)
        self.assertEqual(self.before, self.source.read_text())

    def test_prior_workflow_and_contract_diagnosis_keep_read_only_source(self):
        for workflow, scope in ((39, 'migration'), (WORKFLOW_VERSION, 'contract')):
            with self.subTest(workflow=workflow, scope=scope):
                command = replace(self.command, command_id=f'planner-{workflow}-{scope}',
                    options={**self.command.options, 'workflow_version': workflow},
                    payload={**self.command.payload, 'goal_scope': scope})

                def agent(handler, received):
                    self.assertTrue(handler.read_only)
                    self.assertIs(command, received)
                    return self.agent_reply(received)

                with patch('modport.handlers.CodexStageHandler.__call__', agent), \
                        patch.object(diagnostic_repairs, 'prepare', wraps=diagnostic_repairs.prepare) as prepare:
                    result = CoderRevivalPlannerHandler()(command)
                self.assertEqual('completed', result.status, result.detail)
                prepare.assert_not_called()
                self.assertNotIn('diagnostic_repairs', result.outputs)
        self.assertEqual(self.before, self.source.read_text())

    def test_snapshot_failure_keeps_diagnosis_read_only_and_returns_concrete_feedback(self):
        def agent(handler, received):
            self.assertTrue(handler.read_only)
            self.assertIs(self.command, received)
            return self.agent_reply(received)

        with patch.object(diagnostic_repairs, 'prepare', side_effect=ValueError('source is unsafe')), \
                patch('modport.handlers.CodexStageHandler.__call__', agent):
            result = CoderRevivalPlannerHandler()(self.command)
        self.assertEqual('completed', result.status, result.detail)
        self.assertEqual('source is unsafe', result.outputs['diagnostic_repair_preparation_diagnostic'])
        self.assertNotIn('diagnostic_repairs', result.outputs)


if __name__ == '__main__':
    unittest.main()
