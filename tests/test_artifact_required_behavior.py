"""Current artifact policy over real SDK tasks and host-shaped observations.

The fixtures model runtime observations; they do not establish mod acceptance.
"""
from copy import deepcopy
from dataclasses import dataclass
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from dispatcher_sdk.orchestrator import Orchestrator, OrchestratorHost

from modport.artifact_verification import ArtifactTestReportHandler
from modport.artifact_verification_policy import (
    assess_required_cases, assess_source_selection, required_behavior_policy,
)
from modport.contracts import OperationInput, OperationResult
from modport.evidence import atomic_json
from modport.kernel_runtime import open_runtime
from modport.models import Budget, MigrationRequest
from modport.operations import MigrationOperations
from modport.application_state_storage import hydrate_run_snapshot
from modport.test_matrix import select_migration_tests
from modport.workflow import ARTIFACT_VERIFICATION_STAGES, compile_migration_workflow


def contract(include_defect=False):
    ids = ['required'] + (['known_defect'] if include_defect else [])
    return {'schema_version': 1, 'contract_id': 'fixture-contract',
        'source_fingerprint': 'fixture-original-source',
        'behaviors': [{'id': 'behavior', 'test_mapping': ids,
            'assertion_contracts': [{'assertion_id': test_id + '.assertion',
                'test_ids': [test_id], 'text': 'Observe the real action',
                'source_anchor': {'path': 'Source.java', 'start_line': 1, 'end_line': 1}}
                for test_id in ids]}],
        'test_evidence': {test_id: {'path': '.modport/evidence/' + test_id + '.json',
            'evidence_kind': 'runtime', 'executor': 'junit',
            'result_identity': {'kind': 'junit_xml', 'gradle_task': 'test',
                'classname': 'ExampleTest', 'name': test_id}}
            for test_id in ids}}


def observations(document, status='passed', *, source=False, execution_id='fixture-execution'):
    cases, assertions, records = {}, {}, {}
    for test_id, declaration in document['test_evidence'].items():
        outcome = 'failed' if source and test_id == 'known_defect' else status
        cases[test_id] = {'test_id': test_id, 'status': outcome, 'test_outcome': outcome,
            'category': 'mod_behavior' if outcome == 'failed' else 'none',
            'result_identity': deepcopy(declaration['result_identity']),
            'source_commit': document['source_fingerprint'], 'candidate_unchanged': True,
            'execution_nonce': execution_id}
        assertions[test_id + '.assertion'] = {'status': outcome, 'test_ids': [test_id]}
        if (outcome == 'passed' or source and test_id == 'known_defect') and status != 'witness_missing':
            records[test_id] = {'path': declaration['path'], 'evidence_kind': 'runtime',
                                'executor': 'junit', 'runtime_operations': ['invoke real action'],
                                'runtime_witness_count': 1}
        if status == 'witness_missing':
            cases[test_id].update(status='passed', test_outcome='passed')
            assertions[test_id + '.assertion']['status'] = 'passed'
    return {'process_executed': True, 'case_results': cases, 'assertion_results': assertions,
        'evidence_records': records, 'source_commit': document['source_fingerprint'],
        'execution_nonce': execution_id}


@dataclass
class ReportFixture:
    __execution_kernel_revision__ = 'artifact-required-report-fixture-v1'

    def __call__(self, command):
        return ArtifactTestReportHandler()(command)


@dataclass
class ArtifactStageFixture:
    source_outcomes: tuple = ('passed',)
    target_outcomes: tuple = ('passed',)
    include_defect: bool = False
    product_violation: bool = False
    __execution_kernel_revision__ = 'artifact-required-behavior-fixture-v1'

    def __call__(self, command):
        root = Path(command.run_dir)
        document = contract(self.include_defect)
        stage = command.stage_id
        outputs = {}
        status = 'completed'
        error_code = None
        if stage == 'contract_draft':
            target = root / 'baseline/.modport/functional-contract.json'
            atomic_json(target, document)
            if command.payload.get('required_behavior_repair'):
                adapter = root / 'baseline/.modport/harness/source-adapter.txt'
                adapter.parent.mkdir(parents=True, exist_ok=True)
                adapter.write_text('repaired source adapter\n')
        elif stage in {'contract_verify', 'artifact_test_execute'}:
            source = stage == 'contract_verify'
            outcomes = self.source_outcomes if source else self.target_outcomes
            outcome = outcomes[min(command.attempt - 1, len(outcomes) - 1)]
            if not source:
                lock_ref = command.artifact_refs['functional_contract_lock']
                document = json.loads((root / lock_ref['path']).read_text())['contract']
            outputs = observations(document, outcome, source=source, execution_id=command.command_id)
            # Deliberately model the old zero-exit/skip operation status bug.
            # Host completion must use the actual observations, not this status.
            status = 'failed' if outcome == 'failed' or source and self.include_defect else 'completed'
        elif stage == 'contract_review':
            matrix = {'schema_version': 1, 'cases': [
                {'test_id': test_id, 'behavior_id': 'behavior', 'entry_point': 'ExampleTest.' + test_id,
                 'action': 'Invoke real action', 'conditions': ['live runtime'],
                 'assertion_ids': [test_id + '.assertion']}
                for test_id in document['test_evidence']]}
            decisions = [{'test_id': test_id,
                'decision': 'source_defect' if test_id == 'known_defect' else 'keep',
                'reason': 'Observed original-source result',
                **({'defect_assertion_ids': ['known_defect.assertion'],
                    'evidence': ['original-source runtime observation']} if test_id == 'known_defect' else {})}
                for test_id in document['test_evidence']]
            selection = select_migration_tests(document, matrix,
                {'schema_version': 1, 'decisions': decisions},
                command.upstream_results['contract_verify']['outputs'])
            target = root / 'artifacts/executions' / command.command_id / 'baseline-test-selection.json'
            atomic_json(target, {'selection': selection})
            outputs = {'artifact_refs': {'baseline_test_selection': {
                'path': target.relative_to(root).as_posix(), 'media_type': 'application/json'}}}
        elif stage == 'contract_freeze':
            selected = command.upstream_results['contract_review']['outputs']['artifact_refs']['baseline_test_selection']
            selection = json.loads((root / selected['path']).read_text())['selection']
            target = root / 'artifacts/executions' / command.command_id / 'functional-contract-observation.json'
            atomic_json(target, {'contract': selection['migration_contract'],
                'source_contract': document, 'source_defects': selection['source_defects'],
                'uncovered_assertion_ids': selection['uncovered_assertion_ids']})
            outputs = {'artifact_refs': {'functional_contract_lock': {
                'path': target.relative_to(root).as_posix(), 'media_type': 'application/json'}}}
        elif stage == 'artifact_test_design':
            if self.product_violation:
                (root / 'worktree/delivered.jar').write_bytes(b'changed-by-fixture-author')
                status, error_code = 'failed', 'artifact_candidate_changed'
            elif command.payload.get('required_behavior_repair'):
                target = root / 'worktree/.modport/harness/target-adapter.txt'
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text('repaired target adapter\n')
        return OperationResult(status, command.run_id, command.task_id, stage, command.command_id,
                               outputs=outputs, error_code=error_code)


class ArtifactRequiredBehaviorTests(unittest.TestCase):
    def source_assessment(self, document, output, assessment):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        matrix = {'schema_version': 1, 'cases': [
            {'test_id': test_id, 'behavior_id': 'behavior',
             'entry_point': 'ExampleTest.' + test_id, 'action': 'Invoke real action',
             'conditions': ['live runtime'], 'assertion_ids': [
                 row['assertion_id'] for row in document['behaviors'][0]['assertion_contracts']
                 if test_id in row['test_ids']]}
            for test_id in document['test_evidence']]}
        selection = select_migration_tests(document, matrix, assessment, output)
        path = root / 'artifacts/selection.json'
        atomic_json(path, {'selection': selection})
        effective = {'contract_verify': {'outputs': output}, 'contract_review': {'outputs': {
            'artifact_refs': {'baseline_test_selection': {'path': 'artifacts/selection.json'}}}}}
        return assess_source_selection(root, effective)

    def run_workflow(self, *, source=('passed',), target=('passed',), defect=False,
                     rounds=2, assignments=20, reroute=None, deadline=None, product_violation=False):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        (root / 'baseline').mkdir()
        (root / 'worktree').mkdir()
        jar = root / 'worktree/delivered.jar'
        jar.write_bytes(b'fixture-delivered-binary')
        descriptor = {'jar_ref': {'path': 'worktree/delivered.jar'}, 'target_commit': 'fixture-target'}
        atomic_json(root / 'artifacts/artifact-input.json', descriptor)
        request = MigrationRequest('example', 'https://example.invalid/source.git', '1.20.1', '1.21.1',
            workflow_mode='artifact_verification',
            budget=Budget(max_seconds=120, max_agent_assignments=assignments, max_rework_rounds=rounds))
        definition = compile_migration_workflow(request).to_dict()
        fixture = ArtifactStageFixture(source, target, defect, product_violation)
        handlers = {'modport.' + stage: fixture for stage in ARTIFACT_VERIFICATION_STAGES}
        handlers['modport.artifact_test_report'] = ReportFixture()
        operations = MigrationOperations(handlers=handlers, isolation_mode='thread')
        with open_runtime(root, handlers=handlers, isolation_mode='thread') as runtime:
            sdk = Orchestrator(root / 'orchestrator.sqlite3', runtime.kernel, runtime=runtime)
            try:
                header = {'format_version': 2, 'run_id': 'required-artifact', 'run_dir': str(root),
                    'request': request.to_dict(), 'definition': definition,
                    'registry_revision': runtime.registry_revision, 'prior_findings': [],
                    'initial_refs': {'artifact_input': {'path': 'artifacts/artifact-input.json'}},
                    'rubric_sha256': 'host-supplied-provenance', 'started_at': time.time(),
                    'deadline_epoch': time.time() + 120 if deadline is None else deadline}
                atomic_json(root / 'run.json', header)
                state = sdk.create_run(header['run_id'], command_id='create', input=header,
                                       definition=definition)
                with patch('modport.artifact_verification.artifact_input', return_value=(descriptor, jar)), \
                        OrchestratorHost(sdk, worker_count=2) as host:
                    until = time.monotonic() + 60
                    while time.monotonic() < until:
                        if reroute is not None:
                            sdk.sync()
                            observed = hydrate_run_snapshot(root, sdk.get_run(header['run_id']))
                            stage = 'contract_review' if reroute == 'resume' else 'artifact_test_report'
                            task = observed['tasks'].get(stage)
                            app = observed.get('application_state') or {}
                            if (task and task['attempts'][-1]['state'] == 'succeeded'
                                    and stage not in app.get('effective', {})):
                                attempt = task['attempts'][-1]
                                result = attempt['result']['value']
                                app['effective'][stage] = result
                                app['processed'].append(result['command_id'])
                                app['active_stage'] = None
                                if reroute == 'resume':
                                    app['flowthrough_resume'] = {'stage': stage,
                                        'task_id': stage, 'location': 'main',
                                        'next_stage': 'contract_freeze', 'command_id': result['command_id']}
                                else:
                                    app['flowthrough_finish_pending'] = True
                                sdk.apply_operations(header['run_id'], command_id='restore-routing-boundary',
                                    expected_revision=observed['revision'], expected_generation=observed['generation'],
                                    operations=[], application_state=app)
                                reroute = None
                        state = operations.tick(sdk, header)
                        if state['state'] in {'succeeded', 'failed', 'cancelled'}:
                            return root, header, state
                        host.wake(header['run_id'])
                        time.sleep(0.01)
                    self.fail('bounded artifact SDK workflow did not settle: ' + json.dumps({
                        'health': str(host.health()),
                        'tasks': {stage: [(attempt['state'], attempt.get('result', {}).get('error'))
                                         for attempt in task['attempts']]
                                  for stage, task in state['tasks'].items()},
                        'active_stage': state['application_state'].get('active_stage')}))
            finally:
                sdk.close()

    def test_scoped_policy_does_not_change_migration_diagnostics(self):
        request = MigrationRequest('example', 'https://example.invalid/source.git', '1.20.1', '1.21.1')
        self.assertFalse(required_behavior_policy(compile_migration_workflow(request).to_dict()))

    def test_missing_witness_cannot_pass_even_with_passing_junit_and_assertions(self):
        assessment = assess_required_cases(contract(), observations(contract(), 'witness_missing'))
        self.assertEqual('failed', assessment['status'])
        self.assertIn('required: host-validated runtime witness is missing', assessment['gaps'])

    def test_xml_only_placeholder_cannot_be_excluded_as_a_source_defect(self):
        document = contract(True)
        output = observations(document, source=True)
        output['case_results']['known_defect']['category'] = 'unknown'
        del output['evidence_records']['known_defect']
        result = self.source_assessment(document, output, {'schema_version': 1, 'decisions': [
            {'test_id': 'required', 'decision': 'keep', 'reason': 'Keep live coverage'},
            {'test_id': 'known_defect', 'decision': 'source_defect', 'reason': 'Failed JUnit placeholder',
             'defect_assertion_ids': ['known_defect.assertion'], 'evidence': ['assertion failure']}]})
        self.assertEqual('failed', result['status'])
        self.assertTrue(any('known_defect: source-defect exclusion lacks' in gap for gap in result['gaps']))

    def test_failed_original_assertion_with_live_runtime_witness_can_be_excluded(self):
        document = contract(True)
        result = self.source_assessment(document, observations(document, source=True), {
            'schema_version': 1, 'decisions': [
                {'test_id': 'required', 'decision': 'keep', 'reason': 'Keep live coverage'},
                {'test_id': 'known_defect', 'decision': 'source_defect', 'reason': 'Observed original defect',
                 'defect_assertion_ids': ['known_defect.assertion'], 'evidence': ['Live action witness']}]})
        self.assertEqual('passed', result['status'])

    def test_redundant_skipped_case_does_not_keep_retained_source_assertion_unverified(self):
        document = contract()
        document['test_evidence']['duplicate'] = deepcopy(document['test_evidence']['required'])
        document['test_evidence']['duplicate']['result_identity']['name'] = 'duplicate'
        document['behaviors'][0]['test_mapping'].append('duplicate')
        document['behaviors'][0]['assertion_contracts'][0]['test_ids'].append('duplicate')
        output = observations(document)
        output['case_results']['duplicate'].update(status='skipped', test_outcome='skipped')
        del output['evidence_records']['duplicate']
        output['assertion_results']['required.assertion'] = {
            'status': 'unverified', 'test_ids': ['required', 'duplicate']}
        result = self.source_assessment(document, output, {'schema_version': 1, 'decisions': [
            {'test_id': 'required', 'decision': 'keep', 'reason': 'Retain live coverage'},
            {'test_id': 'duplicate', 'decision': 'merge', 'reason': 'Same assertion is already covered',
             'replacement_test_ids': ['required']}]})
        self.assertEqual('passed', result['status'])
        self.assertEqual(['required.assertion'], result['required_assertion_ids'])

    def test_passing_cases_cannot_replace_missing_or_failed_target_assertion_results(self):
        document = contract()
        for rows in ({}, {'required.assertion': {'status': 'failed', 'test_ids': ['required']}}):
            with self.subTest(rows=rows):
                output = observations(document)
                output['assertion_results'] = rows
                self.assertEqual('failed', assess_required_cases(document, output)['status'])

    def test_required_source_adapter_is_repaired_verified_and_reassessed_before_freeze(self):
        root, header, state = self.run_workflow(source=('skipped', 'passed'), rounds=0)
        self.assertEqual('succeeded', state['state'])
        self.assertTrue((root / 'baseline/.modport/harness/source-adapter.txt').is_file())
        for stage in ('contract_draft', 'contract_verify', 'contract_review'):
            self.assertEqual(2, len(state['tasks'][stage]['attempts']))
        self.assertEqual(1, len(state['tasks']['contract_freeze']['attempts']))
        repaired = OperationInput.from_dict(state['tasks']['contract_draft']['attempts'][1]['command']['payload'])
        self.assertEqual(header['deadline_epoch'], repaired.options['deadline_epoch'])
        self.assertEqual('skipped', repaired.upstream_results['contract_verify']['outputs']['case_results']['required']['status'])
        self.assertEqual('source', repaired.payload['required_behavior_repair']['scope'])
        report = json.loads((root / 'artifacts/artifact-verification-report.json').read_text())
        self.assertEqual('passed', report['required_behavior_status'])

    def test_target_failure_gets_real_harness_repair_and_fresh_execution(self):
        root, _, state = self.run_workflow(target=('failed', 'passed'), rounds=0)
        self.assertEqual('succeeded', state['state'])
        self.assertTrue((root / 'worktree/.modport/harness/target-adapter.txt').is_file())
        self.assertEqual(b'fixture-delivered-binary', (root / 'worktree/delivered.jar').read_bytes())
        self.assertEqual(2, len(state['tasks']['artifact_test_design']['attempts']))
        self.assertEqual(2, len(state['tasks']['artifact_test_execute']['attempts']))

    def test_skipped_target_exhausts_shared_assignment_budget_and_archives_failed_report(self):
        root, _, state = self.run_workflow(target=('skipped',), rounds=0, assignments=4)
        self.assertEqual('failed', state['state'])
        self.assertEqual(2, len(state['tasks']['artifact_test_execute']['attempts']))
        self.assertEqual('agent_assignment_budget_exhausted', state['application_state']['terminal_reason'])
        self.assertIn('artifact_test_report', state['tasks'])
        report = json.loads((root / 'artifacts/artifact-verification-report.json').read_text())
        self.assertEqual('failed', report['required_behavior_status'])
        self.assertEqual('skipped', report['target_verification']['outputs']['case_results']['required']['test_outcome'])

    def test_missing_source_evidence_exhausts_assignment_budget_without_target_execution(self):
        root, _, state = self.run_workflow(source=('witness_missing',), assignments=2)
        self.assertEqual('failed', state['state'])
        self.assertNotIn('artifact_test_execute', state['tasks'])
        self.assertEqual('agent_assignment_budget_exhausted', state['application_state']['terminal_reason'])
        self.assertTrue((root / 'artifacts/artifact-verification-report.json').is_file())

    def test_legitimate_original_defect_is_excluded_without_repairing_product(self):
        root, _, state = self.run_workflow(defect=True)
        self.assertEqual('succeeded', state['state'])
        self.assertEqual(1, len(state['tasks']['contract_draft']['attempts']))
        source = state['application_state']['effective']['contract_verify']
        self.assertEqual('failed', source['status'])
        target = state['application_state']['effective']['artifact_test_execute']
        self.assertEqual(['required'], list(target['outputs']['case_results']))
        report = json.loads((root / 'artifacts/artifact-verification-report.json').read_text())
        self.assertEqual('passed', report['required_behavior_status'])

    def test_expired_original_deadline_archives_failure_without_dispatch(self):
        root, _, state = self.run_workflow(deadline=time.time() - 1)
        self.assertEqual('failed', state['state'])
        self.assertEqual({}, state['tasks'])
        report = json.loads((root / 'artifacts/artifact-verification-report.json').read_text())
        self.assertEqual('wall_clock_budget_exhausted', report['failure_reason'])

    def test_restored_next_stage_cannot_skip_required_source_repair(self):
        root, _, state = self.run_workflow(source=('skipped', 'passed'), reroute='resume')
        self.assertEqual('succeeded', state['state'])
        self.assertEqual(2, len(state['tasks']['contract_draft']['attempts']))
        self.assertEqual(2, len(state['tasks']['contract_verify']['attempts']))
        self.assertEqual(1, len(state['tasks']['contract_freeze']['attempts']))
        self.assertTrue((root / 'baseline/.modport/harness/source-adapter.txt').is_file())

    def test_pending_finish_reassesses_actual_failed_report_and_required_cases(self):
        root, _, state = self.run_workflow(target=('skipped',), rounds=0, assignments=3, reroute='pending')
        self.assertEqual('failed', state['state'])
        self.assertEqual(1, len(state['tasks']['artifact_test_execute']['attempts']))
        self.assertFalse(state['application_state'].get('flowthrough_finish_pending', False))
        report = json.loads((root / 'artifacts/artifact-verification-report.json').read_text())
        self.assertEqual('failed', report['required_behavior_status'])

    def test_product_violation_is_archived_without_snapshotting_it_as_another_repair(self):
        root, _, state = self.run_workflow(product_violation=True)
        self.assertEqual('failed', state['state'])
        self.assertNotIn('artifact_test_execute', state['tasks'])
        self.assertEqual(1, len(state['tasks']['artifact_test_design']['attempts']))
        self.assertEqual('artifact_candidate_changed', state['application_state']['terminal_reason'])
        self.assertTrue((root / 'artifacts/artifact-verification-report.json').is_file())


if __name__ == '__main__':
    unittest.main()
