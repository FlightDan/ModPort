"""Real Git conflicts through current SDK dispatch, planner and coder handoff."""
import json
import copy
from contextlib import ExitStack
from dataclasses import replace
from pathlib import Path
import subprocess
import tempfile
import time
import unittest
from unittest.mock import patch
from dispatcher_sdk.orchestrator import Operations

from fixtures_modport import registry
from modport.application_state_storage import pack_application_state, hydrate_run_snapshot
from modport.contracts import OperationInput
from modport.continuation import continue_from_planner, prepare_application
from modport.development import CoderHandler, DevelopmentIntegrateHandler, _artifact, validate_plan
from modport.execution_budget import current_deadline_budget
from modport.handlers import _result
from modport.memory_admission import MemorySnapshot
from modport.models import Budget, MigrationRequest
from modport.operations import MigrationOperations
from modport.revival_planning import CoderRevivalPlannerHandler
from modport.report_dialogue import prepare_dialogue
from modport.workflow import WORKFLOW_VERSION
from modport import workflow_upgrade


TEST_PREDECESSOR_RUN_ID = 'selected-predecessor'


class CarriedRequest(MigrationRequest):
    """Represent the actual retained Run's pre-Wiki frozen request shape."""

    def to_dict(self):
        value = super().to_dict()
        value.pop('wiki_enabled')
        return value


def git(root, *args):
    return subprocess.check_output(['git', '-c', 'core.hooksPath=/dev/null',
        '-c', 'user.name=Test', '-c', 'user.email=test@example.invalid', *args],
        cwd=root, stderr=subprocess.DEVNULL).decode().strip()


class SDKCoder(CoderHandler):
    __execution_kernel_revision__ = 'current-dependency-conflict-coder'


class SDKPlanner(CoderRevivalPlannerHandler):
    __execution_kernel_revision__ = 'current-dependency-conflict-planner'


class SDKIntegration(DevelopmentIntegrateHandler):
    __execution_kernel_revision__ = 'current-dependency-conflict-integration'


class DependencyConflictRecoveryTests(unittest.TestCase):
    def test_conflict_is_planned_then_resolved_under_the_same_sdk_budget(self):
        self.exercise_recovery()

    def test_explicit_continuation_reconsiders_stop_without_resetting_budget(self):
        self.exercise_recovery(continue_stopped=True)

    def exercise_recovery(self, *, continue_stopped=False):
        with patch.object(workflow_upgrade, 'UPGRADE_SOURCE_RUN_ID', TEST_PREDECESSOR_RUN_ID), \
                tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / 'run'
            handlers = registry()
            handlers.update({'modport.coder': SDKCoder(),
                             'modport.coder_revival_plan': SDKPlanner(),
                             'modport.development_integrate': SDKIntegration()})
            operations = MigrationOperations(handlers=handlers, isolation_mode='thread',
                memory_probe=lambda: MemorySnapshot(64 * 1024**3, 64 * 1024**3, 'test'))
            request_type = CarriedRequest if continue_stopped else MigrationRequest
            request = request_type('conflict-recovery', 'https://example.invalid/mod.git',
                '1.21', '26.1', source_loader='neoforge', source_revision='a' * 40,
                budget=Budget(max_seconds=600, max_agent_assignments=12,
                              execution_max_attempts=1), max_parallel_coders=3)
            run = operations.submit(request, run_dir=root,
                run_id=TEST_PREDECESSOR_RUN_ID if continue_stopped else 'conflict-recovery')
            work = root / 'worktree'
            work.mkdir()
            git(work, 'init')
            contract = work / '.modport' / 'functional-contract.json'
            contract.parent.mkdir()
            frozen_requirements = [{'id': 'behavior-A', 'assertions': [
                {'id': 'assertion-A', 'expected': 'preserve observable behavior'}]}]
            initial_contract = {'schema_version': 1, 'implementation_note': 'base',
                                'requirements': frozen_requirements, 'target_cases': ['case-base'],
                                'execution_bindings': {}}
            contract.write_text(json.dumps(initial_contract, indent=2) + '\n')
            git(work, 'add', '.'); git(work, 'commit', '-m', 'base')
            base = git(work, 'rev-parse', 'HEAD')
            tasks = [{'id': name, 'objective': 'Integrate ' + name,
                      'owned_paths': ['.modport/functional-contract.json'],
                      'dependencies': ['left', 'right', 'harness'] if name == 'join' else [],
                      'acceptance': [], 'complexity': 'simple'}
                     for name in ('left', 'right', 'harness', 'join')]
            plan = validate_plan({'base_commit': base, 'tasks': tasks}, workflow_version=WORKFLOW_VERSION)
            freeze = OperationInput(run.run_id, 'implementation', 'implementation', 'freeze', str(root))
            plan_ref = _artifact(freeze, 'development-plan.json', json.dumps(plan).encode(),
                                 {'development_base': base})
            entered = []
            planner_requests = []

            def model_boundary(handler, command):
                budget = current_deadline_budget(command)
                self.assertIsNotNone(budget, 'real SDK assignment must own the execution budget')
                if command.stage_id == 'coder_revival_plan':
                    from modport.opencode_shell_mcp import _read_run_artifact
                    evidence = command.payload['revival_request']
                    planner_requests.append(evidence)
                    failure = evidence['results']['join']
                    self.assertEqual('dependency_patch_conflict', failure['error_code'])
                    self.assertFalse(failure['outputs']['agent_started'])
                    ref = failure['outputs']['artifact_refs']['dependency_conflict']
                    raw = _read_run_artifact({'root': str(root), 'deadline_epoch': time.time() + 60}, ref['path'])
                    record = json.loads(raw['content_utf8'])
                    self.assertEqual(['.modport/functional-contract.json'], record['paths'])
                    self.assertIn('with conflicts', record['detail'])
                    self.assertIn('dependency_conflict', handler.prompt)
                    dialogue = prepare_dialogue(command, root, handler.prompt)
                    for prompt in (dialogue['planning_task'], dialogue['execution_task']):
                        self.assertIn('host already supports dependency-conflict recovery', prompt)
                        self.assertIn('fresh isolated coder workspace', prompt)
                        self.assertIn('no generic request_rework tool', prompt)
                        self.assertIn('Stop is permitted only in two cases', prompt)
                        self.assertIn('Uncertainty is not proof of impossibility', prompt)
                    self.assertEqual(initial_contract, json.loads(contract.read_text()))
                    if continue_stopped and len(planner_requests) == 1:
                        return _result(command, 'completed', outputs={
                            'raw_report': json.dumps({'reason': 'Prior planner did not know the repair handoff.',
                                'decisions': [{'task_id': 'join', 'action': 'stop',
                                    'instruction': 'Cannot edit the diagnostic contract copy.', 'wait_for': []}]}),
                            'agent_dialogue': {'transport': 'opencode', 'turns': 2}})
                    if continue_stopped:
                        self.assertEqual('stop', evidence['prior_decisions'][0]['decision']['decisions'][0]['action'])
                    return _result(command, 'completed', outputs={
                        'raw_report': json.dumps({'reason': 'Overlapping prose edits in the preserved contract; '
                            'resume the integrator to reconcile both dependency versions.',
                            'decisions': [{'task_id': 'join', 'action': 'resume',
                                'instruction': 'Resolve the preserved contract conflict using both prerequisite patches.',
                                'wait_for': [], 'reuse_partial': False}]}),
                        'agent_dialogue': {'transport': 'opencode', 'turns': 2}})
                name = command.payload['development_task']['id']
                entered.append(name)
                target = root / command.options['workspace'] / '.modport' / 'functional-contract.json'
                if name == 'join':
                    self.assertIn('<<<<<<<', target.read_text())
                    self.assertIn('Conflict evidence:', handler.prompt)
                    self.assertIn('planner', handler.prompt)
                    self.assertIn('Resolve the preserved contract conflict', handler.prompt)
                    note = 'left and right and harness'
                    document = {**initial_contract, 'implementation_note': note,
                        'target_cases': ['case-base', 'case-harness'],
                        'execution_bindings': {key: 'real-' + key for key in ('left', 'right', 'harness')}}
                else:
                    note = name
                    document = {**initial_contract, 'implementation_note': note,
                        'execution_bindings': {name: 'real-' + name}}
                    if name == 'harness':
                        document['target_cases'] = ['case-base', 'case-harness']
                target.write_text(json.dumps(document, indent=2) + '\n')
                return _result(command, 'completed')

            with patch('modport.handlers.CodexStageHandler.__call__', model_boundary), ExitStack() as sessions:
                _, header, runtime, sdk = sessions.enter_context(operations.session(root, run.run_id))
                self.assertEqual(WORKFLOW_VERSION, header['definition']['workflow_version'])
                deadline = header['deadline_epoch']
                app = operations._new_application()
                operations._start_development_group(header, app, 'implementation', {
                    'development_tasks': plan['tasks'], 'development_base': base,
                    'goal_scope': 'migration', 'artifact_refs': {'development_plan': plan_ref}})
                app['effective']['implementation'] = _result(freeze, 'completed', outputs={
                    'development_tasks': plan['tasks'], 'development_base': base,
                    'goal_scope': 'migration', 'artifact_refs': {'development_plan': plan_ref}}).to_dict()
                group = app['active_group']
                group['goal_scheduled'] = [item['id'] for item in plan['tasks']]
                for item in plan['tasks']:
                    group['results']['goal.g1.' + item['id']] = {'outputs': {}}

                def tick(label):
                    state = hydrate_run_snapshot(root, sdk.get_run(run.run_id))
                    changes = operations._flowthrough_group_decision(state, header, app)
                    if changes:
                        sdk.apply_operations(run.run_id, command_id=label,
                            expected_revision=state['revision'], expected_generation=state['generation'],
                            operations=changes, application_state=pack_application_state(root, app))
                    return changes

                def execute_one():
                    sdk.flush()
                    self.assertIsNotNone(runtime.run_once())
                    sdk.sync()

                tick('seed-coders')
                execute_one(); execute_one(); execute_one()
                state = hydrate_run_snapshot(root, sdk.get_run(run.run_id))
                left = state['tasks']['coder.g1.left']['attempts'][-1]
                premature = replace(OperationInput.from_dict(left['command']['payload']),
                    stage_id='development_integrate', task_id='premature-integration',
                    command_id='premature-integration',
                    options={**left['command']['payload']['options'], 'workspace': 'worktree'},
                    payload={**left['command']['payload']['payload'],
                        'development_results': [state['tasks']['coder.g1.' + name]['attempts'][-1]['result']['value']
                                                for name in ('left', 'right')]})
                rejected = DevelopmentIntegrateHandler()(premature)
                self.assertEqual('integration_conflict', rejected.error_code)
                self.assertEqual(base, git(work, 'rev-parse', 'HEAD'))
                self.assertEqual('', git(work, 'status', '--porcelain'))
                tick('dispatch-join')
                execute_one()
                self.assertCountEqual(['left', 'right', 'harness'], entered)
                changes = tick('dispatch-recovery-planner')
                self.assertFalse(any(op.get('kind') == 'finish' for op in changes))
                state = hydrate_run_snapshot(root, sdk.get_run(run.run_id))
                failed = state['tasks']['coder.g1.join']['attempts'][0]['result']['value']
                self.assertEqual('dependency_patch_conflict', failed['error_code'])
                self.assertNotIn('coder_patch', failed['outputs']['artifact_refs'])
                self.assertTrue(any(name.startswith('revival.') for name in state['tasks']))
                # A formerly misclassified pre-model setup failure has no
                # native session or partial candidate to recover on continue.
                from modport.continuation import _budget_exhausted_development_group
                previous = copy.deepcopy(state)
                previous['tasks']['coder.g1.join']['attempts'][0]['result']['value']['error_code'] = 'coder_isolation_violation'
                previous_app = copy.deepcopy(app)
                previous_app['terminal_reason'] = 'coder_isolation_violation'
                previous_app['effective']['implementation'] = _result(freeze, 'completed', outputs={
                    'development_tasks': plan['tasks'], 'development_base': base,
                    'artifact_refs': {'development_plan': plan_ref}}).to_dict()
                continued = _budget_exhausted_development_group(previous, previous_app)
                self.assertCountEqual(['left', 'right', 'harness'], continued['scheduled'])
                self.assertEqual([], continued['execution_payload']['interrupted_development_work'])
                self.assertEqual('join', continued['execution_payload']['interrupted_development_setup'][0]['task_id'])
                execute_one()
                after_planner = hydrate_run_snapshot(root, sdk.get_run(run.run_id))
                self.assertEqual(1, len(planner_requests), json.dumps({name: task['attempts'][-1]
                    for name, task in after_planner['tasks'].items() if name.startswith('revival.')}))
                if continue_stopped:
                    tick('settle-planner-stop')
                    predecessor = sdk.get_run(run.run_id)
                    self.assertEqual('failed', predecessor['state'])
                    hydrated = hydrate_run_snapshot(root, predecessor)
                    self.assertEqual('coder_revival_stopped', hydrated['application_state']['terminal_reason'])
                    previous_id = run.run_id
                    # Changed inputs are not an excuse to reopen a supervisor-terminated author.
                    terminated = copy.deepcopy(hydrated)
                    terminated['application_state']['progress_supervision'] = {
                        'terminated_executions': {'historical-right': {'task_id': 'coder.g1.right'}}}
                    excluded = prepare_application(root, terminated, target_workflow_version=WORKFLOW_VERSION)
                    self.assertEqual('stopped', excluded['active_group']['revival']['holds']['join']['status'])
                    self.assertNotIn('dependency_stop_reconsideration', excluded['continuation_feedback'])
                    unrelated = copy.deepcopy(hydrated)
                    unrelated['application_state']['terminal_reason'] = 'external_dependency_unavailable'
                    unchanged = prepare_application(root, unrelated, target_workflow_version=WORKFLOW_VERSION)
                    self.assertEqual('stopped', unchanged['active_group']['revival']['holds']['join']['status'])
                    sessions.close()
                    run = continue_from_planner(operations, root, previous_id,
                        next_run_id='conflict-recovery-successor', reason='Corrected recovery capability instructions',
                        upgrade_workflow=True)
                    _, header, runtime, sdk = sessions.enter_context(operations.session(root, run.run_id))
                    app = hydrate_run_snapshot(root, sdk.get_run(run.run_id))['application_state']
                    self.assertEqual('needs_plan', app['active_group']['revival']['holds']['join']['status'])
                    self.assertEqual('stopped', app['continuation_feedback']['dependency_stop_reconsideration']
                                     ['previous_holds']['join']['status'])
                    self.assertEqual(5, header['continuation']['agent_assignments_carried'])
                    self.assertEqual(deadline, header['deadline_epoch'])
                    self.assertEqual(12, header['request']['budget']['max_agent_assignments'])
                    self.assertEqual(WORKFLOW_VERSION, header['definition']['workflow_version'])
                    self.assertEqual(WORKFLOW_VERSION, header['workflow_upgrade']['from_version'])
                    rules = root / header['initial_refs']['agent_rules']['path']
                    self.assertIn('only when the authoritative host context confirms exhaustion', rules.read_text())
                    tick('reconsider-prior-stop')
                    current = sdk.get_run(run.run_id)
                    self.assertFalse(any(name.startswith('coder.') for name in current['tasks']))
                    self.assertTrue(any(name.startswith('revival.') for name in current['tasks']))
                    # A host bootstrap failure can leave a reserved planner in
                    # the SDK outbox. Cancel it publicly without entering its handler,
                    # then replace this unstarted deployment without reclaiming usage.
                    replacement = 'conflict-recovery-replacement'
                    sdk.apply_operations(run.run_id, command_id='cancel-unstarted-planner',
                        expected_revision=current['revision'], operations=[
                            Operations.cancel(name, reason='Host bootstrap repair')
                            for name in current['tasks']])
                    sdk.flush(); sdk.sync()
                    cancelled = sdk.get_run(run.run_id)
                    self.assertTrue(all(t['attempts'][-1]['state'] == 'cancelled'
                                        for t in cancelled['tasks'].values()))
                    self.assertEqual(1, len(planner_requests))
                    sdk.apply_operations(run.run_id,
                        command_id=replacement + ':replace-planned-predecessor',
                        expected_revision=cancelled['revision'], operations=[Operations.finish('cancelled')])
                    replaced_id = run.run_id
                    sessions.close()
                    run = continue_from_planner(operations, root, previous_id,
                        next_run_id=replacement, reason='Replace unstarted host deployment',
                        replace_unstarted_successor=replaced_id, upgrade_workflow=True)
                    _, header, runtime, sdk = sessions.enter_context(operations.session(root, run.run_id))
                    app = hydrate_run_snapshot(root, sdk.get_run(run.run_id))['application_state']
                    self.assertEqual(6, header['continuation']['agent_assignments_carried'])
                    self.assertEqual(6, app['continuation_feedback']['replaced_successor_usage']['agent_assignments'])
                    self.assertEqual(deadline, header['deadline_epoch'])
                    self.assertEqual(12, header['request']['budget']['max_agent_assignments'])
                    tick('dispatch-replacement-planner')
                    execute_one()
                    retained = sdk.get_run(previous_id)
                    # Public SDK continuation records its successor link and
                    # advances the revision; frozen inputs and failure evidence stay intact.
                    for key in ('state', 'input', 'definition', 'tasks', 'application_state'):
                        self.assertEqual(predecessor[key], retained[key], key)
                    self.assertEqual(2, len(planner_requests))
                tick('dispatch-resumed-integrator')
                execute_one()
                self.assertCountEqual(['left', 'right', 'harness', 'join'], entered)
                tick('dispatch-final-integration')
                execute_one()
                state = hydrate_run_snapshot(root, sdk.get_run(run.run_id))
                result = state['tasks']['development_integrate']['attempts'][-1]['result']['value']
                self.assertEqual('completed', result['status'], result['detail'])
                merged = json.loads(contract.read_text())
                self.assertEqual('left and right and harness', merged['implementation_note'])
                self.assertEqual(frozen_requirements, merged['requirements'])
                self.assertEqual(['case-base', 'case-harness'], merged['target_cases'])
                self.assertEqual({key: 'real-' + key for key in ('left', 'right', 'harness')},
                                 merged['execution_bindings'])
                self.assertEqual('', git(work, 'status', '--porcelain'))
                self.assertEqual(1 if continue_stopped else 2, len(state['tasks']['coder.g1.join']['attempts']))
                self.assertEqual(deadline, header['deadline_epoch'])
                self.assertEqual(8 if continue_stopped else 6, app['agent_assignments'])
                resumed = state['tasks']['coder.g1.join']['attempts'][-1]['result']['value']
                self.assertEqual('unverified', resumed['outputs']['acceptance_status'])
                self.assertEqual('dependency_patch_conflict', failed['error_code'])


if __name__ == '__main__':
    unittest.main()
