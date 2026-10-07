"""Actual stdio tool calls while the public SDK runs the awaited author."""
from dataclasses import dataclass
import json
from pathlib import Path
import select
import subprocess
import sys
import tempfile
import unittest

from modport.contracts import OperationResult
from modport.models import Budget, MigrationRequest
from modport.operations import MigrationOperations
from modport.memory_admission import MemorySnapshot
from modport.rework_tools import prepare_session
from fixtures_modport import FixtureHandler, registry


@dataclass
class RevisableAuthor:
    __execution_kernel_revision__ = 'rework-test-author-1'

    def __call__(self, command):
        request = command.payload.get('reviewer_rework')
        text = 'initial author report' if not request else 'Revision: ' + request['instructions']
        root = Path(command.run_dir)
        path = root / 'logs' / (command.command_id + '.txt')
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        result = FixtureHandler()(command)
        return OperationResult(**{**result.to_dict(), 'outputs': {**result.outputs,
            'last_message': path.relative_to(root).as_posix()}})


@dataclass
class InteractiveReviewer:
    __execution_kernel_revision__ = 'rework-test-reviewer-1'

    def __call__(self, command):
        root = Path(command.run_dir)
        # Two author revisions each include fresh verification. The waiting
        # v26 reviewer finishes before the normal contract freeze runs.
        # Keep this bounded below the Run budget without assuming fast fsyncs.
        session = prepare_session(command, root / 'worktree', 120)
        if session is None:
            raise AssertionError('reviewer was not offered its upstream author')
        child = subprocess.Popen([sys.executable, '-m', 'modport.rework_mcp', '--session', str(session)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1)
        def rpc(identity, method, params):
            child.stdin.write(json.dumps({'jsonrpc': '2.0', 'id': identity, 'method': method, 'params': params}) + '\n')
            child.stdin.flush()
            if not select.select([child.stdout], [], [], 90)[0]:
                raise AssertionError('review tool and upstream author deadlocked')
            return json.loads(child.stdout.readline())
        try:
            rpc(1, 'initialize', {'protocolVersion': '2024-11-05', 'capabilities': {},
                                  'clientInfo': {'name': 'fixture-reviewer', 'version': '1'}})
            listing = rpc(2, 'tools/list', {})
            if 'request_rework' not in str(listing):
                raise AssertionError('missing real tool')
            results = []
            for number in range(3):
                instructions = 'fix ' + str(number) + ' {unclosed report bracket'
                result = rpc(3 + number, 'tools/call', {'name': 'request_rework',
                    'arguments': {'target_agent': 'contract_draft', 'instructions': instructions}})['result']
                results.append(result)
                if number < 2 and (result.get('isError') or instructions not in str(result)):
                    raise AssertionError('raw revised context did not return to the caller: ' + str(result))
                if number == 2 and not result.get('isError'):
                    raise AssertionError('rework limit did not return a tool error')
            (root / 'review-tool-results.json').write_text(json.dumps(results))
        finally:
            child.stdin.close()
            try:
                child.wait(timeout=3)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait()
            child.stdout.close()
            child.stderr.close()
        return FixtureHandler()(command)


class ReviewReworkTests(unittest.TestCase):
    @staticmethod
    def sdk_execution_diagnostic(host, run, execution_id):
        """Retain the SDK failure cause before TemporaryDirectory removes the Run."""
        try:
            with host.session(run.run_dir, run.run_id,
                              allow_terminal_deployment=True) as (_, _, runtime, sdk):
                snapshot = sdk.inspect_execution(execution_id)
                result = snapshot.result
                effect_ids = [] if result is None else list(result.effect_ids)
                effects = {}
                for effect_id in effect_ids:
                    effect = runtime.kernel.get_effect(effect_id)
                    effects[effect_id] = {
                        'record': effect.to_dict(),
                        'events': runtime.kernel.effect_events(effect_id),
                    }
                events = []
                for event in runtime.kernel.events(execution_id):
                    # The submitted event repeats the complete command payload.
                    # Identity and the raw terminal error are sufficient here.
                    item = {key: event.get(key) for key in (
                        'revision', 'event_type', 'from_state', 'to_state',
                        'data', 'created_at')}
                    if item['event_type'] == 'submitted':
                        item['data'] = {'command_omitted': True}
                    events.append(item)
                return {
                    'snapshot': {
                        'execution_id': snapshot.execution_id,
                        'state': snapshot.state,
                        'revision': snapshot.revision,
                        'attempt': snapshot.attempt,
                        'fence': snapshot.fence,
                        'recovery_effect_id': snapshot.recovery_effect_id,
                        'recovery_target_state': snapshot.recovery_target_state,
                        'recovery_reason': snapshot.recovery_reason,
                        'result': None if result is None else {
                            'status': result.status,
                            'error': None if result.error is None else result.error.to_dict(),
                            'effect_ids': effect_ids,
                            'started_at': result.started_at,
                            'completed_at': result.completed_at,
                        },
                    },
                    'events': events,
                    'effects': effects,
                }
        except BaseException as error:
            return {'diagnostic_error': f'{type(error).__name__}: {error}'}

    def test_sdk_author_runs_twice_and_returns_to_one_waiting_review(self):
        self.exercise_rework()

    def test_sdk_waits_for_capacity_and_resumes_same_blocking_mcp_request(self):
        self.exercise_rework(memory_pressure=True)

    def exercise_rework(self, *, memory_pressure=False):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / 'run'
            custom = registry()
            custom['modport.contract_draft'] = RevisableAuthor()
            custom['modport.contract_review'] = InteractiveReviewer()
            pressure_samples = []
            def memory_probe():
                requests = root / 'artifacts/rework-tools'
                if (memory_pressure and len(pressure_samples) < 4
                        and any(requests.glob('*/requests/*.json'))):
                    pressure_samples.append(True)
                    return MemorySnapshot(1024**3, 8 * 1024**3, 'injected pressure')
                return MemorySnapshot(64 * 1024**3, 64 * 1024**3, 'fixture')
            host = MigrationOperations(memory_probe=memory_probe, handlers=custom, isolation_mode='thread')
            request = MigrationRequest('example', 'https://example.invalid/mod.git', '1.20.1', '26.1.2',
                # The assertion concerns the two allowed reworks; leave room
                # for the complete pipeline when other integration tests run.
                budget=Budget(max_seconds=600, max_rework_rounds=2), source_revision='a' * 40)
            run = host.submit(request, run_dir=root, run_id='test')
            finished = host.execute(run)
            state = finished.snapshot
            self.assertFalse(any(wait['state'] == 'open' for wait in state['waits'].values()),
                             state['waits'])
            sdk_diagnostic = None
            if state['state'] != 'succeeded':
                failed = state['tasks'].get('test_execute.g1.scope-001')
                if failed and failed.get('attempts'):
                    execution_id = failed['attempts'][-1]['command']['execution_id']
                    sdk_diagnostic = self.sdk_execution_diagnostic(host, finished, execution_id)
            self.assertEqual('succeeded', state['state'], {
                'reason': state['application_state'].get('terminal_reason'),
                'waits': state.get('waits'),
                'failed_results': {key: value for key, value in
                    state['application_state'].get('effective', {}).items()
                    if value.get('status') != 'completed'},
                'sdk_diagnostic': sdk_diagnostic,
            })
            review = state['tasks']['contract_review']
            self.assertEqual(1, len(review['attempts']))
            ledger = state['application_state']['review_rework']['requests']
            if memory_pressure:
                self.assertEqual(4, len(pressure_samples))
                first = min(ledger.values(), key=lambda row: row['sequence'])
                self.assertIn('waiting_resources', first)
                self.assertFalse(first['waiting_resources'])
            self.assertEqual(['completed', 'completed', 'failed'],
                             [row['state'] for row in sorted(ledger.values(), key=lambda row: row['sequence'])])
            self.assertEqual(2, state['application_state']['rounds']['review_rework:contract_draft'])
            tasks = [task for name, task in state['tasks'].items()
                     if name.startswith('agent-rework.')
                     and task['attempts'][0]['command']['payload']['stage_id'] == 'contract_draft']
            self.assertEqual(2, len(tasks))
            verification = [task for name, task in state['tasks'].items()
                            if name.startswith('agent-rework.')
                            and task['attempts'][0]['command']['payload']['stage_id'] == 'contract_verify']
            self.assertEqual(2, len(verification))
            refreshes = [task for name, task in state['tasks'].items()
                         if name.startswith('agent-rework.')
                         and task['attempts'][0]['command']['payload']['stage_id'] == 'contract_freeze']
            self.assertEqual(0, len(refreshes))
            self.assertIn('contract_freeze', state['tasks'])
            self.assertTrue(all(task['attempts'][0]['state'] == 'succeeded'
                                for task in verification))
            self.assertTrue(all(task['attempts'][0]['command']['causation_id'] == 'test:contract_review:1'
                                for task in tasks))
            self.assertTrue(all(task['attempts'][0]['state'] == 'succeeded' for task in tasks))
            results = json.loads((root / 'review-tool-results.json').read_text())
            self.assertIn('fix 0 {unclosed report bracket', str(results[0]))
            self.assertIn('fix 1 {unclosed report bracket', str(results[1]))


class ReworkCompletionTests(unittest.TestCase):
    def test_reworked_design_replaces_group_result_used_by_execution(self):
        from modport.rework_orchestration import ReviewReworkOrchestration
        record = {'target_agent': 'test_design.scope-a', 'request_id': 'request'}
        app = {'effective': {}, 'active_group': {'results': {
            'test_design.scope-a': {'command_id': 'old-design'}}}}
        result = OperationResult('completed', 'run', 'child-design', 'test_design', 'new-design',
                                 outputs={'workspace': 'workspaces/tests/revised'})
        ReviewReworkOrchestration._apply_tool_update(app, record, 'test_design', result)
        self.assertEqual('new-design', app['active_group']['results']['test_design.scope-a']['command_id'])
        self.assertEqual('new-design', app['effective']['test_design']['command_id'])

    def test_completed_coder_is_reconciled_after_caller_closes_and_builds_again(self):
        from modport.contracts import OperationInput
        from modport.rework_orchestration import ReviewReworkOrchestration

        class Host(ReviewReworkOrchestration):
            def _schedule(self, snapshot, header, app, stage, **kwargs):
                self.scheduled = stage
                return [{'kind': 'dispatch', 'task_id': kwargs['task_id']}]

        with tempfile.TemporaryDirectory() as directory:
            command = OperationInput('run', 'child', 'agent_rework', 'child-exec', directory)
            result = OperationResult('completed', 'run', 'child', 'agent_rework', 'child-exec',
                                     outputs={'after_head': 'b' * 40}, detail='author raw revision')
            record = {'state': 'running', 'task_id': 'child', 'reviewer_execution_id': 'closed-review',
                      'target_stage': 'coder', 'target_agent': 'coder-a', 'reviewer_stage': 'code_review',
                      'request_id': 'request', 'updates': []}
            app = {'review_rework': {'requests': {'request': record}, 'latest_targets': {}, 'sequence': 1},
                   'cancel_sent': [], 'processed': [], 'history': [], 'effective': {}}
            snapshot = {'tasks': {'child': {'attempts': [{'state': 'succeeded',
                'command': {'execution_id': 'child-exec', 'payload': command.to_dict()},
                'result': {'value': result.to_dict()}}]}}}
            host = Host()
            host._review_rework_decision(snapshot, {'run_dir': directory}, app)
            self.assertEqual('target_build', host.scheduled)
            self.assertIn('coder-a', app['effective'])
            self.assertIn('author raw revision', record['text'])
            self.assertEqual(['child-exec'], app['processed'])
            build = OperationInput('run', 'child.verify', 'target_build', 'build-exec', directory)
            snapshot['tasks']['child.verify'] = {'attempts': [{'state': 'running',
                'command': {'execution_id': 'build-exec', 'payload': build.to_dict()}}]}
            operations = host._review_rework_decision(snapshot, {'run_dir': directory}, app)
            self.assertEqual([], operations)
            self.assertEqual('running', record['state'])
            failure = OperationResult('failed', 'run', 'child.verify', 'target_build', 'build-exec',
                                      detail='fresh build failure')
            snapshot['tasks']['child.verify'] = {'attempts': [{'state': 'succeeded',
                'command': {'execution_id': 'build-exec', 'payload': build.to_dict()},
                'result': {'value': failure.to_dict()}}]}
            host._review_rework_decision(snapshot, {'run_dir': directory}, app)
            self.assertEqual('failed', record['state'])
            self.assertIn('author raw revision', record['text'])
            self.assertIn('fresh build failure', record['text'])
            self.assertEqual(2, len(record['updates']))
            self.assertEqual('reviewer_closed_before_rework_result', app['stop_reason'])


if __name__ == '__main__':
    unittest.main()
