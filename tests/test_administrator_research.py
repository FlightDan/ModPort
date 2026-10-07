from pathlib import Path
from types import SimpleNamespace
import json
import tempfile
import unittest

from modport.contracts import OperationInput, OperationResult
from modport.evidence import atomic_json, digest, read_json
from modport.kernel_runtime import cancel_incomplete_research, reconcile_receipt
from modport.models import Budget, MigrationRequest
from modport.operations import MigrationOperations
from modport.research_policy import initialize_research, record_dispatch
from modport.workflow import STAGE_IDS, WORKFLOW_VERSION
from fixtures_modport import FixtureHandler


class AdministratorFixture:
    __execution_kernel_revision__ = 'administrator-v7-fixture'

    def __call__(self, command):
        if command.stage_id == 'admin_review':
            return OperationResult('completed', command.run_id, command.task_id, command.stage_id, command.command_id,
                outputs={'verdict': 'approved', 'reviewer_id': 'admin-review-agent',
                    'submission_id': command.payload['admin_submission_id'],
                    'approved_gap_resolutions': [{'gap_id': 'platform:0', 'project_status': 'resolved'}]})
        return FixtureHandler()(command)


class AdministratorImportTests(unittest.TestCase):
    def test_real_sdk_import_is_reviewed_before_waking_and_never_refills_budget(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / 'run'
            handler = AdministratorFixture()
            operations = MigrationOperations(handlers={'modport.' + stage: handler for stage in STAGE_IDS}, isolation_mode='thread')
            request = MigrationRequest('admin', 'https://example.invalid/mod.git', '1.20.1', '26.1.2',
                                       source_revision='a' * 40, budget=Budget(max_seconds=None))
            run = operations.submit(request, run_dir=root, run_id='admin')
            source = root / 'artifacts/source.json'
            atomic_json(source, {'source_commit': 'a' * 40})
            with operations.session(root, run.run_id) as (_, header, runtime, sdk):
                state = sdk.get_run(run.run_id)
                app = operations._new_application()
                app['effective']['source'] = OperationResult('completed', 'admin', 'source', 'source', 'seed',
                    outputs={'artifact_refs': {'source_evidence': {'path': 'artifacts/source.json'}}}).to_dict()
                operations._ingest_analysis(app, {'unresolved_relevant_gaps': [{'skill': 'platform', 'index': 0}]})
                initialize_research(app, {})
                record_dispatch(app, 'gap_research', 'spent', ['platform'])
                app['gap_join_stage'] = 'source'
                app['last_gap_plan'] = {'gap_revision': 1, 'gaps': ['platform:0'], 'tasks': []}
                wait = operations._park_administrator(state, header, app)
                state = sdk.apply_operations('admin', command_id='seed-wait', expected_revision=state['revision'],
                                             operations=wait, application_state=app)
                submission = {'schema_version': 1, 'submission_id': 'answer-1', 'run_id': 'admin',
                    'workflow_version': WORKFLOW_VERSION, 'execution_version': header['registry_revision'],
                    'knowledge_revisions': {}, 'base_gap_revision': app['gap_revision'], 'source_commit': 'a' * 40,
                    'gap_resolutions': [{'gap_id': 'platform:0', 'project_status': 'resolved'}],
                    'generic_knowledge_entries': {}, 'sources': []}
            submitted = Path(temporary) / 'answer.json'
            atomic_json(submitted, submission)
            # Edits to a projection have no authority.
            atomic_json(root / 'artifacts/research-gaps.json', {'gaps': []})
            self.assertEqual(operations.status(root, 'admin').status, 'waiting')
            imported = operations.import_research(root, 'admin', submitted)
            self.assertEqual(imported.snapshot['application_state']['project_research_gaps']['platform:0']['project_status'], 'unresolved')
            self.assertIn('admin_review', imported.snapshot['tasks'])
            self.assertEqual(len(operations.import_research(root, 'admin', submitted).snapshot['tasks']['admin_review']['attempts']), 1)
            with operations.session(root, 'admin') as (_, header, runtime, sdk):
                sdk.flush()
                runtime.run_once()
                sdk.sync()
                state = operations.tick(sdk, header)
                app = state['application_state']
                self.assertEqual(app['project_research_gaps']['platform:0']['project_status'], 'resolved')
                self.assertEqual(app['research_budget']['platform']['dispatched'], 1)
                self.assertEqual(app['admin_imports']['answer-1']['status'], 'approved')
                self.assertFalse(any(wait['state'] == 'open' for wait in state['waits'].values()))
                self.assertIn('background' if WORKFLOW_VERSION >= 17 else 'source', state['tasks'])
                self.assertEqual(read_json(root / 'artifacts/research-gaps.json')['gaps'][0]['project_status'], 'resolved')
            stale = {**submission, 'submission_id': 'outdated'}
            atomic_json(submitted, stale)
            with self.assertRaisesRegex(ValueError, 'gap revision'):
                operations.import_research(root, 'admin', submitted)


class ResearchRecoveryTests(unittest.TestCase):
    def test_partial_research_is_explicit_failed_receipt_not_success_or_refund(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            command = OperationInput('r', 'gap_research', 'gap_research', 'r:gap_research:1', str(root))
            expected = {'input_sha256': digest(command.to_dict()), 'run_dir': str(root), 'stage': command.stage_id}
            effect = SimpleNamespace(request=expected)
            response = cancel_incomplete_research(root, {'payload': command.to_dict()}, effect)
            self.assertEqual(response['status'], 'failed')
            self.assertTrue(response['outputs']['research_cancelled'])
            self.assertFalse(response['outputs']['artifacts_complete'])
            self.assertEqual(reconcile_receipt(root, {'payload': command.to_dict()}, effect), response)
            self.assertEqual(cancel_incomplete_research(root, {'payload': command.to_dict()}, effect), response)

    def test_sdk_recovers_partial_research_as_failure_with_spent_allowance(self):
        from fixtures_modport import registry
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / 'run'
            operations = MigrationOperations(handlers=registry(raise_stage='gap_research'), isolation_mode='thread')
            run = operations.submit(MigrationRequest('partial', 'https://example.invalid/mod.git', '1.20.1', '26.1.2',
                source_revision='a' * 40, budget=Budget(max_seconds=None)), run_dir=root, run_id='partial')
            with operations.session(root, run.run_id) as (_, header, runtime, sdk):
                state = sdk.get_run(run.run_id)
                app = operations._new_application()
                initialize_research(app, {})
                operations._ingest_analysis(app, {'unresolved_relevant_gaps': [{'skill': 'platform', 'index': 0}]})
                commands = operations._schedule(state, header, app, 'gap_research', activate=False, dependencies=[])
                app['gap_pending'] = ['gap_research']
                state = sdk.apply_operations(run.run_id, command_id='seed-partial', expected_revision=state['revision'],
                                             operations=commands, application_state=app)
                sdk.flush()
                runtime.run_once()
                sdk.sync()
                self.assertEqual(sdk.get_run(run.run_id)['tasks']['gap_research']['attempts'][0]['state'], 'recovery_required')
            recovered = operations.recover(root, run.run_id, cancel_interrupted_research=True)
            self.assertNotEqual(recovered.snapshot['tasks']['gap_research']['attempts'][0]['state'], 'recovery_required')
            self.assertEqual(recovered.snapshot['application_state']['research_budget']['platform']['dispatched'], 1)
            receipt = read_json(root / 'artifacts/executions/partial:gap_research:1/receipt.json')
            self.assertEqual(receipt['response']['status'], 'failed')
            self.assertFalse(receipt['response']['outputs']['artifacts_complete'])
            self.assertTrue((root / 'partial.txt').exists())
