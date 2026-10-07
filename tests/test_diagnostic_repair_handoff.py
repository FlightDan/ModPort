"""Real SDK revival repair publication, coder consumption and Git integration."""
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from fixtures_modport import registry
from modport.application_state_storage import hydrate_run_snapshot, pack_application_state
from modport.contracts import OperationInput
from modport.development import CoderHandler, DevelopmentIntegrateHandler, _artifact, validate_plan
from modport.execution_budget import current_deadline_budget
from modport.handlers import _result
from modport.memory_admission import MemorySnapshot
from modport.models import Budget, MigrationRequest
from modport.operations import MigrationOperations
from modport.revival_planning import CoderRevivalPlannerHandler
from modport.workflow import WORKFLOW_VERSION


def git(root, *args):
    return subprocess.check_output([
        'git', '-c', 'core.hooksPath=/dev/null', '-c', 'user.name=Test',
        '-c', 'user.email=test@example.invalid', *args], cwd=root,
        stderr=subprocess.DEVNULL).decode().strip()


class SDKCoder(CoderHandler):
    __execution_kernel_revision__ = 'current-diagnostic-repair-coder'


class SDKPlanner(CoderRevivalPlannerHandler):
    __execution_kernel_revision__ = 'current-diagnostic-repair-planner'


class SDKIntegration(DevelopmentIntegrateHandler):
    __execution_kernel_revision__ = 'current-diagnostic-repair-integration'


class DiagnosticRepairHandoffTests(unittest.TestCase):
    def run_handoff(self, *, conflicting_partial=False):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / 'run'
            handlers = registry()
            handlers.update({'modport.coder': SDKCoder(),
                             'modport.coder_revival_plan': SDKPlanner(),
                             'modport.development_integrate': SDKIntegration()})
            operations = MigrationOperations(handlers=handlers, isolation_mode='thread',
                memory_probe=lambda: MemorySnapshot(64 * 1024**3, 64 * 1024**3, 'test'))
            run = operations.submit(MigrationRequest(
                'diagnostic-repair', 'https://example.invalid/mod.git', '1.21', '26.1',
                source_loader='neoforge', source_revision='a' * 40,
                budget=Budget(max_seconds=600, max_agent_assignments=8,
                              execution_max_attempts=1), max_parallel_coders=1),
                run_dir=root, run_id='diagnostic-repair-handoff')
            work = root / 'worktree'
            source = work / 'src' / 'A.java'
            source.parent.mkdir(parents=True)
            before = 'class A { int count = missing; }\n'
            stopped = 'class A { int count = other; }\n' if conflicting_partial else before
            after = 'class A { int count = 1; }\n'
            source.write_text(before)
            (work / 'remaining.txt').write_text('pending\n')
            git(work, 'init')
            git(work, 'add', '.')
            git(work, 'commit', '-m', 'base')
            base = git(work, 'rev-parse', 'HEAD')
            task = {'id': 'A', 'objective': 'Finish the count change and remaining work',
                    'owned_paths': ['src/A.java', 'remaining.txt'], 'dependencies': [],
                    'acceptance': [], 'complexity': 'simple'}
            plan = validate_plan({'base_commit': base, 'tasks': [task]},
                                 workflow_version=WORKFLOW_VERSION)
            freeze = OperationInput(run.run_id, 'implementation', 'implementation', 'freeze', str(root))
            plan_ref = _artifact(freeze, 'development-plan.json', json.dumps(plan).encode(),
                                 {'development_base': base})
            coder_commands, planner_commands, consumed_refs = [], [], []

            def model_boundary(handler, command):
                budget = current_deadline_budget(command)
                self.assertIsNotNone(budget, 'the real SDK execution must own this assignment budget')
                self.assertEqual(WORKFLOW_VERSION, command.options['workflow_version'])
                workspace = root / command.options['workspace']
                if command.stage_id == 'coder_revival_plan':
                    planner_commands.append(command)
                    self.assertFalse(handler.read_only)
                    self.assertEqual('agent_failed', command.payload['revival_request']['results']['A']['error_code'])
                    self.assertIn('obvious, confirmed, small code errors', handler.prompt)
                    copies = list(workspace.rglob('A.java'))
                    self.assertEqual(1, len(copies))
                    self.assertEqual(stopped, copies[0].read_text())
                    copies[0].write_text(after)
                    self.assertEqual(stopped, (root / coder_commands[0].options['workspace'] / 'src/A.java').read_text())
                    return _result(command, 'completed', outputs={
                        'raw_report': json.dumps({'reason': 'The current A.java uses an undefined name; '
                            'the isolated copy replaces it with the intended literal.',
                            'decisions': [{'task_id': 'A', 'action': 'resume',
                                'instruction': 'The isolated correction supplies count=1; finish remaining.txt.',
                                'wait_for': [], 'reuse_partial': not conflicting_partial}]}),
                        'agent_dialogue': {'transport': 'opencode', 'turns': 2}})
                coder_commands.append(command)
                target = workspace / 'src/A.java'
                if len(coder_commands) == 1:
                    if conflicting_partial:
                        target.write_text(stopped)
                    return _result(command, 'failed', detail='A.java refers to an undefined local name',
                                   error_code='agent_failed')
                refs = command.payload['diagnostic_repair_refs']
                self.assertEqual(1, len(refs))
                consumed_refs.extend(refs)
                self.assertIn('do only the remaining task work', handler.prompt)
                self.assertIn(refs[0]['path'], handler.prompt)
                repair = json.loads((root / refs[0]['path']).read_text())
                self.assertEqual(stopped, repair['files'][0]['before'])
                self.assertEqual(after, repair['files'][0]['after'])
                receipts = json.loads((root / 'artifacts/executions' / command.command_id /
                                       'diagnostic-repair-application.json').read_text())
                expected_status = 'conflict' if conflicting_partial else 'applied'
                self.assertEqual(expected_status, receipts[0]['status'])
                self.assertEqual(refs[0], receipts[0]['repair_ref'])
                self.assertEqual(before if conflicting_partial else after, target.read_text())
                if conflicting_partial:
                    self.assertIn('Conflict/invalid entries were NOT applied', handler.prompt)
                    target.write_text(after)
                (workspace / 'remaining.txt').write_text('done after repair\n')
                return _result(command, 'completed')

            with patch('modport.handlers.CodexStageHandler.__call__', model_boundary), \
                    operations.session(root, run.run_id) as (_, header, runtime, sdk):
                self.assertGreaterEqual(WORKFLOW_VERSION, 40)
                deadline = header['deadline_epoch']
                self.assertIsInstance(deadline, (int, float))
                app = operations._new_application()
                operations._start_development_group(header, app, 'implementation', {
                    'development_tasks': plan['tasks'], 'development_base': base,
                    'goal_scope': 'migration', 'artifact_refs': {'development_plan': plan_ref}})
                group = app['active_group']
                group['goal_scheduled'] = ['A']
                group['results']['goal.g1.A'] = {'outputs': {}}

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

                tick('seed-coder')
                execute_one()
                tick('dispatch-diagnostic-planner')
                execute_one()
                state = hydrate_run_snapshot(root, sdk.get_run(run.run_id))
                planner_tasks = [value for key, value in state['tasks'].items() if key.startswith('revival.')]
                self.assertEqual(1, len(planner_tasks))
                planner_result = planner_tasks[0]['attempts'][-1]['result']['value']
                self.assertEqual('completed', planner_result['status'], planner_result.get('detail'))
                self.assertEqual(1, len(planner_commands))
                self.assertEqual(stopped, (root / coder_commands[0].options['workspace'] / 'src/A.java').read_text())
                tick('dispatch-successor-coder')
                execute_one()
                state = hydrate_run_snapshot(root, sdk.get_run(run.run_id))
                successor = state['tasks']['coder.g1.A']['attempts'][-1]['result']['value']
                self.assertEqual('completed', successor['status'], successor.get('detail'))
                self.assertEqual(consumed_refs, successor['outputs']['diagnostic_repair_refs'])
                self.assertEqual(2, len(coder_commands))
                self.assertNotEqual(coder_commands[0].options['workspace'], coder_commands[1].options['workspace'])
                tick('dispatch-final-integration')
                execute_one()
                state = hydrate_run_snapshot(root, sdk.get_run(run.run_id))
                integrated = state['tasks']['development_integrate']['attempts'][-1]['result']['value']
                self.assertEqual('completed', integrated['status'], integrated.get('detail'))
                self.assertEqual(after, source.read_text())
                self.assertEqual(1, source.read_text().count('int count = 1;'))
                self.assertEqual('done after repair\n', (work / 'remaining.txt').read_text())
                self.assertEqual(stopped, (root / coder_commands[0].options['workspace'] / 'src/A.java').read_text())
                self.assertEqual('', git(work, 'status', '--porcelain'))
                self.assertEqual(deadline, header['deadline_epoch'])
                self.assertEqual(3, app['agent_assignments'])
                self.assertEqual('unverified', successor['outputs']['acceptance_status'])

    def test_planner_fix_is_consumed_once_and_exported_with_remaining_coder_work(self):
        self.run_handoff()

    def test_stale_repair_preserves_source_and_hands_actual_conflict_reference_to_coder(self):
        self.run_handoff(conflicting_partial=True)


if __name__ == '__main__':
    unittest.main()
