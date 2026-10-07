"""Current producer/freeze/SDK repair route; fixtures are not game acceptance."""
from dataclasses import dataclass
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from dispatcher_sdk.orchestrator import Orchestrator, OrchestratorHost
from modport.application_state_storage import hydrate_run_snapshot
from modport.artifact_verification import ArtifactTestReportHandler
from modport.behavior_requirements import BehaviorFreezeHandler
from modport.contracts import OperationResult
from modport.evidence import atomic_json
from modport.kernel_runtime import open_runtime
from modport.models import Budget, MigrationRequest
from modport.operations import MigrationOperations
from modport.target_contract import TargetContractFreezeHandler
from modport.workflow import WORKFLOW_VERSION, compile_migration_workflow, SOURCE_HARNESS_STAGES
from test_target_contract import requirements, target_contract


@dataclass
class Stage:
    missing: bool = False
    __execution_kernel_revision__ = 'source-reading-route-v1'

    def __call__(self, command):
        root = Path(command.run_dir)
        stage = command.stage_id
        outputs = {}
        if stage == 'source':
            atomic_json(root / 'artifacts/source.json', {'source_commit': 'host-source'})
        elif stage == 'behavior_extract':
            atomic_json(root / 'baseline/.modport/behavior-requirements.json', requirements())
        elif stage == 'behavior_freeze':
            return BehaviorFreezeHandler()(command)
        elif stage == 'artifact_test_design':
            candidate = target_contract()
            atomic_json(root / 'worktree/.modport/functional-contract.json', candidate)
            source = root / 'worktree' / candidate['test_evidence']['target.damage']['test_source_files'][0]
            source.parent.mkdir(parents=True, exist_ok=True)
            source.write_text('class HealthGameTest {}')
            atomic_json(root / ('author-input-' + str(command.attempt) + '.json'), command.to_dict())
        elif stage == 'target_contract_freeze':
            return TargetContractFreezeHandler()(command)
        elif stage == 'artifact_test_execute':
            contract = json.loads((root / command.artifact_refs['functional_contract_lock']['path']).read_text())['contract']
            test_id = 'target.damage'
            assertion_id = requirements()['behaviors'][0]['assertions'][0]['assertion_id']
            outputs = {'process_executed': True,
                'case_results': {test_id: {'status': 'passed', 'test_outcome': 'passed'}},
                'assertion_results': {assertion_id: {'status': 'passed', 'test_ids': [test_id]}},
                'evidence_records': {} if self.missing or command.attempt == 1 else {
                    test_id: {'path': contract['test_evidence'][test_id]['path'], 'evidence_kind': 'runtime'}}}
        elif stage == 'artifact_test_report':
            return ArtifactTestReportHandler()(command)
        return OperationResult('completed',command.run_id,command.task_id,stage,command.command_id,outputs=outputs)


class SourceReadingWorkflowTests(unittest.TestCase):
    def execute(self, missing=False):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        (root / 'worktree').mkdir()
        jar = root / 'worktree/delivered.jar'
        jar.write_text('fixture')
        descriptor = {'jar_ref': {'path': 'worktree/delivered.jar'}, 'target_commit': 'host-target'}
        atomic_json(root / 'artifacts/artifact-input.json', descriptor)
        request = MigrationRequest('example','https://example.invalid/source.git','1.20.1','1.21.1',
            workflow_mode='artifact_verification', budget=Budget(max_seconds=120,max_agent_assignments=4))
        definition = compile_migration_workflow(request).to_dict()
        self.assertTrue(SOURCE_HARNESS_STAGES.isdisjoint(definition['main_stages']))
        handlers = {'modport.' + stage: Stage(missing) for stage in definition['main_stages']}
        operations = MigrationOperations(handlers=handlers,isolation_mode='thread')
        with open_runtime(root,handlers=handlers,isolation_mode='thread') as runtime:
            sdk = Orchestrator(root / 'orchestrator.sqlite3',runtime.kernel,runtime=runtime)
            try:
                header = {'format_version':2,'run_id':'target-only','run_dir':str(root),
                    'request':request.to_dict(),'definition':definition,'registry_revision':runtime.registry_revision,
                    'prior_findings':[],'initial_refs':{'artifact_input':{'path':'artifacts/artifact-input.json'}},
                    'rubric_sha256':'host-supplied','started_at':time.time(),'deadline_epoch':time.time()+120}
                atomic_json(root / 'run.json',header)
                sdk.create_run(header['run_id'],command_id='create',input=header,definition=definition)
                with patch('modport.artifact_verification.artifact_input',return_value=(descriptor,jar)), OrchestratorHost(sdk,worker_count=2) as host:
                    until = time.monotonic()+45
                    while time.monotonic()<until:
                        state = operations.tick(sdk,header)
                        if state['state'] in {'succeeded','failed','cancelled'}:
                            return root,hydrate_run_snapshot(root,state)
                        host.wake(header['run_id'])
                        time.sleep(.02)
                    self.fail('current workflow failed to settle within bounded SDK test')
            finally:
                sdk.close()

    def test_missing_witness_reaches_author_and_fresh_target_verification(self):
        root,state = self.execute()
        self.assertEqual('succeeded',state['state'],state['application_state'].get('terminal_reason'))
        self.assertTrue(SOURCE_HARNESS_STAGES.isdisjoint(state['tasks']))
        self.assertEqual(2,len(state['tasks']['artifact_test_execute']['attempts']))
        author = json.loads((root / 'author-input-2.json').read_text())
        self.assertEqual('target',author['payload']['required_behavior_repair']['scope'])
        self.assertIn('runtime witness',json.dumps(author['payload']['required_behavior_repair']))
        self.assertIn('behavior_requirements',author['artifact_refs'])
        self.assertEqual(4,state['application_state']['agent_assignments'])

    def test_unresolved_target_archives_report_and_fails_at_original_assignment_cap(self):
        root,state = self.execute(missing=True)
        self.assertEqual('failed',state['state'])
        self.assertIn('artifact_test_report',state['application_state']['effective'])
        self.assertEqual('agent_assignment_budget_exhausted',state['application_state']['terminal_reason'])
        self.assertEqual('unverified',state['application_state']['acceptance_status'])
        self.assertTrue(SOURCE_HARNESS_STAGES.isdisjoint(state['tasks']))

    def test_native_gametest_protocol_reaches_actual_host_selection(self):
        from modport.handlers import _test_evidence_declarations
        from modport.rubric import acceptance_rubric
        from modport.target_contract import validate_target_contract
        from modport.test_selection_execution import build_selected_test_execution
        candidate = target_contract()
        declaration = candidate['test_evidence']['target.damage']
        declaration.update(executor='gametest',result_identity={'kind':'junit_xml',
            'gradle_task':'runGameTestServer','classname':'example:health','name':'example:damage'})
        candidate['baseline_gradle_tasks'] = ['runGameTestServer']
        candidate = validate_target_contract(requirements(),candidate)
        rubric = acceptance_rubric(workflow_version=WORKFLOW_VERSION)
        candidate.update(rubric_id=rubric['rubric_id'],rubric_version=rubric['rubric_version'])
        declarations = _test_evidence_declarations(candidate,rubric,workflow_version=WORKFLOW_VERSION,
                                                  gradle_tasks=['runGameTestServer'])
        selected = build_selected_test_execution(candidate,list(declarations),workflow_version=WORKFLOW_VERSION)
        self.assertEqual((':runGameTestServer',),selected.gradle_tasks)
        self.assertIn('outputs.upToDateWhen { false }',selected.gradle_init_script)
        self.assertNotIn('--rerun-tasks',selected.gradle_init_script)
        self.assertIn('modportNativeTaskPaths',selected.gradle_init_script)

    def test_current_machine_input_failure_stops_generic_migration(self):
        request=MigrationRequest('example','https://example.invalid/source.git','1.20.1','1.21.1')
        header={'definition':compile_migration_workflow(request).to_dict(),'request':request.to_dict()}
        operations=MigrationOperations()
        for stage,error in [('behavior_freeze','behavior_requirements_missing'),
                            ('target_contract_freeze','target_contract_invalid')]:
            app=operations._new_application()
            app['effective'][stage]={'status':'failed','error_code':error}
            operations._flowthrough_schedule_successor({'tasks':{}},header,app,stage,'failed-input')
            self.assertEqual(error,app['terminal_reason'])
