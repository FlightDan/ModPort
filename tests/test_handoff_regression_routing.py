"""Independent review of v13 routing boundaries; no models or project execution."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from modport.characterization import BehaviorEntry, CharacterizationContract
from modport.contracts import OperationInput, OperationResult
from modport.evidence import atomic_json, file_digest
from modport.handlers import CodexStageHandler, _agent_log_outputs, _result
from modport.regression import partition_scopes, regression_decision


class RecordingPolicy:
    def __init__(self):
        self.requests = []
        self.failures = []

    def _schedule(self, snapshot, header, app, stage, **options):
        self.requests.append((stage, options))
        return [{'kind': 'dispatch', 'task_id': options['task_id']}]

    def _capture_repair_result(self, header, app, result):
        pass

    def _repair_failure(self, snapshot, header, app, stage, failure, execution_id):
        self.failures.append(failure)
        return [{'kind': 'repair', 'stage': stage, 'execution_id': execution_id}]


class HandoffRegressionRoutingTests(unittest.TestCase):
    def setUp(self):
        self.scopes = [{'scope_id': f'scope-{index:03d}', 'behavior_ids': [name],
                        'runtime_behavior_ids': [name], 'static_behavior_ids': [], 'gap_obligations': []}
                       for index, name in enumerate(('a', 'b'), 1)]
        self.group = {'kind': 'regression', 'generation': 4, 'scopes': self.scopes,
                      'members': [], 'results': {}, 'artifact_refs': {'contract': {'path': 'frozen'}},
                      'review_task_id': 'code_review'}
        self.app = {'active_group': self.group, 'processed': [], 'effective': {}, 'history': [],
                    'stop_reason': None}
        self.header = {'definition': {'workflow_version': 13}, 'request': {'max_parallel_coders': 2}}
        self.snapshot = {'tasks': {}}
        self.policy = RecordingPolicy()

    def decide(self):
        return regression_decision(self.policy, self.snapshot, self.header, self.app)

    def complete(self, stage, index, *, status='completed', verdict='approved'):
        scope = self.scopes[index]
        identifier = f'{stage}.g4.{scope["scope_id"]}'
        command = OperationInput('run', identifier, stage, identifier + ':1', '/tmp/routing-fixture',
                                 payload={'regression_scope': scope})
        alias = {'test_design': 'independent_test_snapshot', 'test_review': 'independent_test_review',
                 'test_execute': 'independent_test_result'}[stage]
        result = OperationResult(status, 'run', identifier, stage, command.command_id,
            outputs={'regression_scope': scope, 'workspace': 'workspaces/tests/' + scope['scope_id'],
                     'verdict': verdict, 'artifact_refs': {alias: {'path': identifier + '.json',
                                                                'sha256': str(index) * 64}}},
            error_code='independent_test_review_rejected' if status != 'completed' else None)
        self.snapshot['tasks'][identifier] = {'attempts': [{'state': 'succeeded',
            'command': {'execution_id': command.command_id, 'payload': command.to_dict()},
            'result': {'value': result.to_dict()}}]}
        return result.to_dict()

    def test_each_execution_waits_for_own_review_and_receives_own_producers(self):
        self.assertEqual(['test_design.g4.scope-001', 'test_design.g4.scope-002'],
                         [row['task_id'] for row in self.decide()])
        self.assertEqual([], self.decide())  # Unpublished SDK tasks still occupy both slots.
        designs = [self.complete('test_design', index) for index in (0, 1)]
        self.assertEqual(['test_review.g4.scope-001', 'test_review.g4.scope-002'],
                         [row['task_id'] for row in self.decide()])
        for index, (_, request) in enumerate(self.policy.requests[-2:]):
            self.assertEqual([designs[index]['task_id']], request['dependencies'])
            self.assertEqual(designs[index], request['upstream_overrides']['test_design'])
            self.assertEqual(designs[index]['outputs']['workspace'], request['extra_options']['workspace'])
            self.assertEqual(designs[index]['outputs']['artifact_refs']['independent_test_snapshot'],
                             request['artifact_overrides']['independent_test_snapshot'])
        first_review = self.complete('test_review', 0)
        self.assertEqual(['test_execute.g4.scope-001'], [row['task_id'] for row in self.decide()])
        request = self.policy.requests[-1][1]
        self.assertEqual([first_review['task_id']], request['dependencies'])
        self.assertEqual({'test_design': designs[0], 'test_review': first_review}, request['upstream_overrides'])
        self.assertEqual(first_review['outputs']['artifact_refs']['independent_test_review'],
                         request['artifact_overrides']['independent_test_review'])
        self.complete('test_execute', 0)
        self.assertEqual([], self.decide())
        second_review = self.complete('test_review', 1)
        self.assertEqual(['test_execute.g4.scope-002'], [row['task_id'] for row in self.decide()])
        self.assertEqual(second_review, self.policy.requests[-1][1]['upstream_overrides']['test_review'])
        self.complete('test_execute', 1)
        self.assertEqual(['test_execute.g4.join'], [row['task_id'] for row in self.decide()])
        join = self.policy.requests[-1][1]
        self.assertEqual([first_review, second_review], join['payload']['regression_reviews'])
        self.assertEqual(designs, join['payload']['regression_designs'])
        self.assertEqual(['test_execute.g4.scope-001', 'test_execute.g4.scope-002'], join['dependencies'])
        self.assertEqual(first_review, self.app['effective']['test_review'])

    def test_rejected_review_stops_new_execution_and_retains_rejection_after_other_scope_settles(self):
        self.decide()
        for index in (0, 1):
            self.complete('test_design', index)
        self.decide()
        rejected = self.complete('test_review', 0, status='failed', verdict='rejected')
        self.assertEqual([], self.decide())
        self.assertFalse(any(stage == 'test_execute' for stage, _ in self.policy.requests))
        self.complete('test_review', 1)
        outcome = self.decide()
        self.assertEqual([{'kind': 'repair', 'stage': 'test_review',
                           'execution_id': rejected['command_id']}], outcome)
        self.assertEqual(rejected, self.policy.failures[0].to_dict())
        self.assertFalse(any(stage == 'test_execute' for stage, _ in self.policy.requests))
        self.assertIsNone(self.app['active_group'])

    def test_completed_but_rejected_review_cannot_open_execution(self):
        self.scopes[:] = self.scopes[:1]
        self.decide()
        self.complete('test_design', 0)
        self.decide()
        self.complete('test_review', 0, verdict='rejected')
        self.assertEqual('repair', self.decide()[0]['kind'])
        self.assertFalse(any(stage == 'test_execute' for stage, _ in self.policy.requests))

class StaticPartitionBoundaryTests(unittest.TestCase):
    def partition(self, entries, declarations, *, limit=8, obligations=(), separate_static=True):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            lock = {'contract': CharacterizationContract(entries=tuple(entries)).to_dict(),
                    'test_evidence': declarations}
            atomic_json(root / 'lock.json', lock)
            refs = {'functional_contract_lock': {'path': 'lock.json', 'sha256': file_digest(root / 'lock.json')}}
            return partition_scopes(root, refs, list(obligations), limit, separate_static=separate_static)

    @staticmethod
    def static_declaration(**changes):
        return {'evidence_kind': 'static_client', 'static_reason': 'Requires direct visual observation',
                'acceptance_gates': ['client_smoke'], **changes}

    def test_mixed_static_coverage_attaches_to_runtime_scopes_without_extra_execution_branches(self):
        entries = [BehaviorEntry('runtime', 'source', test_mapping=('runtime-test',)),
                   BehaviorEntry('visual', 'display', side='client', test_mapping=('visual-test',))]
        obligation = {'gap_id': 'save', 'due_stage': 'test_execute', 'closure_criteria': ['Load old save']}
        scopes = self.partition(entries, {'visual-test': self.static_declaration()}, obligations=[obligation])
        self.assertEqual(1, len(scopes))
        self.assertEqual(['runtime', 'visual'], scopes[0]['behavior_ids'])
        self.assertEqual(['runtime'], scopes[0]['runtime_behavior_ids'])
        self.assertEqual(['visual'], scopes[0]['static_behavior_ids'])
        self.assertEqual([obligation], scopes[0]['gap_obligations'])

    def test_all_static_contract_has_one_explicit_static_scope(self):
        entries = [BehaviorEntry(name, 'display', side='client', test_mapping=(name + '-test',))
                   for name in ('visual-a', 'visual-b')]
        scopes = self.partition(entries, {name + '-test': self.static_declaration()
                                         for name in ('visual-a', 'visual-b')})
        self.assertEqual(1, len(scopes))
        self.assertEqual([], scopes[0]['runtime_behavior_ids'])
        self.assertEqual(['visual-a', 'visual-b'], scopes[0]['static_behavior_ids'])
        self.assertEqual(scopes[0]['behavior_ids'], scopes[0]['static_behavior_ids'])

    def test_missing_or_mixed_frozen_declarations_never_exempt_runtime_behavior(self):
        entry = BehaviorEntry('visual', 'display', side='client', test_mapping=('first', 'second'))
        variants = [{}, {'first': self.static_declaration()},
                    {'first': self.static_declaration(), 'second': {'evidence_kind': 'runtime'}},
                    {'first': self.static_declaration(), 'second': self.static_declaration(static_reason=' ')},
                    {'first': self.static_declaration(), 'second': self.static_declaration(acceptance_gates=[])}]
        for declarations in variants:
            with self.subTest(declarations=declarations):
                scope = self.partition([entry], declarations)[0]
                self.assertEqual(['visual'], scope['runtime_behavior_ids'])
                self.assertEqual([], scope['static_behavior_ids'])

    def test_static_declaration_never_exempts_shared_or_server_behavior(self):
        for side in ('server', 'shared', 'both'):
            with self.subTest(side=side):
                entry = BehaviorEntry('behavior', 'source', side=side, test_mapping=('test',))
                scope = self.partition([entry], {'test': self.static_declaration()})[0]
                self.assertEqual(['behavior'], scope['runtime_behavior_ids'])
                self.assertEqual([], scope['static_behavior_ids'])


class HandlerFailureRetentionReviewTests(unittest.TestCase):
    def test_directory_output_failure_preserves_original_result_and_previous_file(self):
        for prior in (None, b'previous valid output'):
            with self.subTest(prior=prior), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                target = root / 'worktree/.modport/out.json'
                target.mkdir(parents=True)
                (target / 'partial.txt').write_text('model created wrong output shape')
                previous = root / 'artifacts/executions/command/previous-outputs'
                previous.mkdir(parents=True)
                (previous / '.replacement-started').write_text('staged')
                if prior is not None:
                    backup = previous / '.modport/out.json'
                    backup.parent.mkdir(parents=True)
                    backup.write_bytes(prior)
                command = OperationInput('run', 'stage', 'stage', 'command', directory)
                failure = _result(command, 'failed', outputs={'last_message': 'logs/last.txt'},
                                  error_code='agent_output_missing')
                with patch.object(CodexStageHandler, '_execute', return_value=failure):
                    result = CodexStageHandler('author', required_paths=('.modport/out.json',))(command)
                self.assertEqual('agent_output_missing', result.error_code)
                self.assertEqual('logs/last.txt', result.outputs['last_message'])
                ref = result.outputs['artifact_refs']['rejected_output:.modport/out.json']
                description = json.loads((root / ref['path']).read_text())
                self.assertEqual('directory', description['actual_type'])
                self.assertEqual('model created wrong output shape',
                                 (root / description['retained_path'] / 'partial.txt').read_text())
                if prior is None:
                    self.assertFalse(target.exists())
                else:
                    self.assertEqual(prior, target.read_bytes())

    def test_partial_agent_log_and_prompt_both_remain_addressable(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'logs').mkdir()
            (root / 'logs/coder.log').write_text('partial model output before timeout')
            (root / 'logs/coder.log.stdin.txt').write_text('original assignment')
            output = _agent_log_outputs(root, 'logs/coder.log')
            refs = output['artifact_refs']
            self.assertEqual({'execution_log', 'agent_prompt'}, set(refs))
            for ref in refs.values():
                self.assertEqual(file_digest(root / ref['path']), ref['sha256'])


if __name__ == '__main__':
    unittest.main()
