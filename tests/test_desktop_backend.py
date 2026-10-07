"""Focused desktop boundaries and actual SDK advisory-task dispatch."""
import copy
from dataclasses import dataclass
import http.client
import json
import os
from pathlib import Path
from types import SimpleNamespace
import subprocess
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from dispatcher_sdk.orchestrator import OrchestratorHost, Operations
from modport.desktop_service import DesktopApplication, DesktopServer, detect_versions, repository_address, revision_name
from modport.desktop_state import DesktopState, managed_instance_id, publish_snapshot, read_json
from modport.desktop_supervisor import chat_decision
from modport.desktop_driver import PersistentSupervisor, main as driver_main, run_instance, save_host_environment, restore_host_environment
from modport.evidence import atomic_json
from modport.handlers import SupervisorHandler
from modport.models import MigrationRequest, Budget
from modport.operations import MigrationOperations
from modport.application_state_storage import hydrate_run_snapshot
from modport.workflow import WORKFLOW_VERSION


@dataclass
class ConversationHandler:
    __execution_kernel_revision__ = 'desktop-conversation-test-v1'

    def __call__(self, command):
        return SupervisorHandler()(command)


class DesktopBackendTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.store = DesktopState(self.temporary.name)
        self.identifier = 'desktop-' + 'a' * 32

    def create_instance(self):
        operations = MigrationOperations(handlers={'modport.supervisor': ConversationHandler()}, isolation_mode='thread')
        request = MigrationRequest('desktop_probe', 'https://github.com/example/project.git', '1.20.1', '1.21.1', budget=Budget(max_seconds=600, max_agent_assignments=5, max_tokens=10000))
        run = operations.submit(request, run_dir=self.store.run_dir(self.identifier), run_id=self.identifier)
        self.store.register(self.identifier, 'Desktop probe')
        header = read_json(run.run_dir / 'run.json')
        publish_snapshot(header, run.snapshot, force=True)
        return operations, run, header

    def test_repository_boundary_and_literal_detection(self):
        self.assertEqual(repository_address('https://gitee.com/team/mod.git'), ('gitee.com', 'team', 'mod'))
        for bad in ['file:///etc/passwd', 'https://127.0.0.1/repo/name', 'https://github.com@localhost/o/r', 'https://github.com:443/o/r', 'https://github.com/o/r?token=secret', 'https://github.com/o/r/../../etc', 'https://github.com/o/r#token']:
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                repository_address(bad)
        for bad in ['--upload-pack=command', 'main..evil', '../main', 'main@{0}', 'branch name', 'main;\ncommand']:
            with self.assertRaises(ValueError):
                revision_name(bad)
        result = detect_versions({'gradle.properties': 'minecraft_version=1.20.1\nforge_version=47.3.0\n', 'build.gradle': 'plugins { id "net.minecraftforge.gradle" }'})
        self.assertEqual(result, {'source_minecraft': '1.20.1', 'source_loader': 'forge', 'source_loader_version': '47.3.0'})
        self.assertNotIn('source_minecraft', detect_versions({'build.gradle': 'minecraft_version=${dynamicVersion}'}))
        self.assertEqual(detect_versions({'gradle.properties': 'mod_id=actual_source_id\n'})['mod_id'], 'actual_source_id')
        self.assertEqual(detect_versions({'mods.toml': 'modId = "source_literal"\n'})['mod_id'], 'source_literal')
        self.assertNotIn('mod_id', detect_versions({'mods.toml': 'modId = "${mod_id}"\n'}))
        fabric = detect_versions({'gradle.properties': 'minecraft_version=1.21.1\nloader_version=0.16.0\nfabric_version=0.102.0+1.21.1\n', 'build.gradle': 'plugins { id "fabric-loom" }'})
        self.assertEqual(fabric, {'source_minecraft': '1.21.1', 'source_loader': 'fabric', 'source_loader_version': '0.16.0'})
        self.assertNotIn('source_loader', detect_versions({'gradle.properties': 'loader_version=1.2.3\n'}))

    def test_cached_projection_preserves_each_task_and_unknown_usage(self):
        operations, run, header = self.create_instance()
        self.assertEqual(header['definition']['workflow_version'], WORKFLOW_VERSION)
        snapshot = copy.deepcopy(run.snapshot)
        snapshot['state'] = 'running'
        for index, execution_state in enumerate(['running', 'planned', 'failed', 'succeeded']):
            snapshot['tasks'][f'coder.task-{index}'] = {'attempts': [{'state': execution_state, 'command': {'execution_id': f'probe:{index}', 'payload': {'stage_id': 'coder', 'options': {'model': 'test'}, 'payload': {'development_task': {'objective': f'Independent task {index}'}}}}, 'result': {'value': {'status': 'failed' if execution_state == 'failed' else 'completed', 'detail': '' if execution_state == 'failed' else f'detail {index}', 'error_code': 'fixture_failure' if execution_state == 'failed' else None}} if execution_state in {'failed', 'succeeded'} else None}]}
        publish_snapshot(header, snapshot, force=True)
        with patch.object(operations, 'status', side_effect=AssertionError('UI must not read SDK status')):
            status = self.store.status(self.identifier)
        tasks = status['stages']['implementation']['items']
        self.assertEqual([item['active_agents'] for item in tasks], [1, 0, 0, 0])
        self.assertTrue(all(item['active_subagents'] is None for item in tasks))
        self.assertEqual([item['state'] for item in tasks], ['running', 'queued', 'failed', 'completed'])
        self.assertEqual(tasks[2]['detail'], '')
        self.assertEqual(tasks[2]['error_code'], 'fixture_failure')
        self.assertIsNone(status['budget']['used_tokens'])
        self.assertFalse(status['budget']['token_usage_complete'])
        self.assertFalse((self.store.root / 'kernel.sqlite3').exists())
        with self.assertRaises(KeyError):
            self.store.run_dir('../../escape')

    def test_actual_sdk_chat_producer_handler_and_projection(self):
        operations, run, header = self.create_instance()
        frozen_deadline = header['deadline_epoch']
        message = self.store.enqueue_message(self.identifier, 'What is the current progress?')
        with operations.session(run.run_dir, run.run_id) as (_, _, _, sdk):
            snapshot = hydrate_run_snapshot(run.run_dir, sdk.get_run(run.run_id))
            app = operations._new_application()
            with patch('modport.opencode_agent.run_agent', return_value=subprocess.CompletedProcess([], 0,
                    json.dumps({'type': 'item.completed', 'item': {'type': 'agent_message', 'text': 'Observed SDK evidence; target acceptance remains unverified.'}}), '')) as agent:
                actions = chat_decision(operations, snapshot, header, app)
                self.assertEqual(app['agent_assignments'], 1)
                self.assertTrue(actions)
                task_id = 'desktop.chat.' + message['id']
                sdk.apply_operations(run.run_id, command_id='desktop-chat-test', expected_revision=snapshot['revision'], expected_generation=snapshot['generation'], operations=actions, application_state=app)
                with OrchestratorHost(sdk, worker_count=1) as host:
                    deadline = time.monotonic() + 10
                    while time.monotonic() < deadline:
                        host.wake(run.run_id)
                        sdk.sync()
                        current = hydrate_run_snapshot(run.run_dir, sdk.get_run(run.run_id))
                        attempt = current['tasks'][task_id]['attempts'][-1]
                        if attempt['state'] in {'succeeded', 'failed'}:
                            break
                        time.sleep(.02)
                    self.assertEqual(attempt['state'], 'succeeded', attempt)
                self.assertEqual(agent.call_count, 1)
                self.assertTrue(agent.call_args.kwargs['no_tools'])
                self.assertTrue(agent.call_args.kwargs['read_only'])
                self.assertEqual(agent.call_args.kwargs['token_budget_root'], run.run_dir)
                self.assertEqual(attempt['command']['payload']['options']['deadline_epoch'], frozen_deadline)
                self.assertEqual(chat_decision(operations, current, header, current['application_state']), [])
                publish_snapshot(header, current, force=True)
                result = self.store.status(self.identifier)
                self.assertEqual(result['messages'][-1]['role'], 'supervisor')
                self.assertEqual(result['messages'][-1]['state'], 'completed')
                self.assertIn('unverified', result['messages'][-1]['content'])
                self.assertFalse(result['supervisor']['busy'])
                self.assertEqual(read_json(run.run_dir / 'run.json')['deadline_epoch'], frozen_deadline)

    def test_advisory_queue_is_closed_when_business_finishes(self):
        operations, run, header = self.create_instance()
        self.store.enqueue_message(self.identifier, 'Do not hold delivery')
        app = operations._new_application()
        self.assertEqual(chat_decision(operations, run.snapshot, header, app, business_operations=[{'kind': 'finish', 'state': 'succeeded'}]), [])
        self.assertEqual(app['agent_assignments'], 0)
        snapshot = copy.deepcopy(run.snapshot)
        snapshot['state'] = 'succeeded'
        publish_snapshot(header, snapshot, force=True)
        status = self.store.status(self.identifier)
        self.assertFalse(status['supervisor']['busy'])
        self.assertEqual(status['messages'][0]['state'], 'failed')
        self.assertEqual(status['acceptance_status'], 'unverified')

    def test_chat_at_assignment_cap_does_not_change_business_lifecycle(self):
        operations, run, header = self.create_instance()
        self.store.enqueue_message(self.identifier, 'Advisory at exhausted cap')
        app = operations._new_application()
        app['agent_assignments'] = header['request']['budget']['max_agent_assignments']
        before = copy.deepcopy(app)
        with patch.object(operations, '_schedule', side_effect=AssertionError('advisory must not enter exhausted scheduler')):
            self.assertEqual(chat_decision(operations, run.snapshot, header, app), [])
        self.assertEqual(app, before)
        self.assertEqual(self.store.messages(self.identifier)[0]['state'], 'failed')

    def test_private_host_environment_roundtrip_excludes_unrelated_secrets(self):
        _, run, _ = self.create_instance()
        values = {'PATH': '/host/tools', 'OPENAI_BASE_URL': 'https://provider.invalid/v1', 'OPENAI_API_KEY': 'private-test-key', 'JAVA_HOME': '/host/java', 'AWS_SECRET_ACCESS_KEY': 'must-not-copy', 'MODPORT_OPENCODE_BIN': '/host/opencode'}
        destination = save_host_environment(self.store, self.identifier, values)
        self.assertNotIn('AWS_SECRET_ACCESS_KEY', json.loads(destination.read_text()))
        self.assertFalse(destination.is_relative_to(run.run_dir))
        if os.name != 'nt':
            self.assertEqual(destination.stat().st_mode & 0o777, 0o600)
            self.assertEqual(destination.parent.stat().st_mode & 0o777, 0o700)
        with patch.dict(os.environ, {'OPENAI_API_KEY': 'wrong-key', 'OPENAI_BASE_URL': 'https://wrong.invalid', 'BAILIAN_API_KEY': 'stale-key'}, clear=False):
            restored = restore_host_environment(self.store, self.identifier)
            self.assertEqual(restored['OPENAI_API_KEY'], 'private-test-key')
            self.assertEqual(os.environ['PATH'], '/host/tools')
            self.assertNotIn('BAILIAN_API_KEY', os.environ)
        self.assertNotIn('private-test-key', json.dumps(self.store.status(self.identifier)))

    def test_linux_unit_has_exact_registered_driver_and_no_provider_secret(self):
        self.store = DesktopState(Path(self.temporary.name) / 'data with spaces % literal')
        _, run, _ = self.create_instance()
        calls = []
        def execute(argv, **kwargs):
            calls.append(argv)
            if 'enable' in argv and any('watchdog' in item for item in argv):
                from modport.platform_runtime import process_birth
                atomic_json(run.run_dir / 'desktop-watchdog-state.json', {'instance': self.identifier,
                    'pid': os.getpid(), 'birth': process_birth(os.getpid()), 'at': time.time(), 'state': 'running'})
            return subprocess.CompletedProcess(argv, 0, 'active\n' if 'is-active' in argv else '', '')
        supervisor = PersistentSupervisor(self.store, platform_name='Linux', execute=execute)
        with patch.dict(os.environ, {'OPENAI_API_KEY': 'private-unit-key'}):
            result = supervisor.launch(self.identifier)
        self.assertEqual(result['kind'], 'systemd')
        unit = (self.store.root / 'supervision' / result['unit']).read_text()
        self.assertIn('Restart=on-failure', unit)
        self.assertIn('RestartPreventExitStatus=78', unit)
        self.assertIn('StartLimitBurst=3', unit)
        self.assertNotIn('StartLimitIntervalSec=0', unit)
        self.assertIn(self.identifier, unit)
        self.assertIn('modport.desktop_driver', unit)
        self.assertIn('WorkingDirectory=' + str(self.store.root).replace('%', '%%') + '\n', unit)
        self.assertNotIn('private-unit-key', unit)
        self.assertTrue(any('is-active' in call for call in calls))
        self.assertNotIn('private-unit-key', json.dumps(calls))

    def test_driver_classifies_business_outcomes_and_unexpected_return(self):
        for index, (state, waiting, expected) in enumerate([
                ('succeeded', False, 0), ('failed', False, 78),
                ('cancelled', False, 78), ('running', True, 78), ('running', False, 1)]):
            with self.subTest(state=state, waiting=waiting):
                self.identifier = 'desktop-' + f'{index:032x}'
                operations, run, header = self.create_instance()
                self.store.update_launch(self.identifier, supervisor={'kind': 'systemd', 'unit': 'registered.service'})
                snapshot = copy.deepcopy(run.snapshot)
                snapshot.update(state=state, application_state={'terminal_reason': 'raw_failure' if state == 'failed' else None})
                if state == 'failed':
                    snapshot['tasks']['coder.failed'] = {'attempts': [{'state': 'succeeded',
                        'result': {'value': {'status': 'blocked', 'error_code': 'dependency_patch_conflict',
                                            'detail': 'raw dependency patch conflict'}}}]}
                if waiting:
                    snapshot['waits'] = {'coordination': {'state': 'open'}}
                with patch('modport.desktop_driver.restore_host_environment'), \
                        patch.object(operations, 'resume', return_value=SimpleNamespace(snapshot=snapshot)) as resume:
                    self.assertEqual(run_instance(self.store.root, self.identifier, operations=operations), expected)
                resume.assert_called_once_with(run.run_dir, run.run_id)
                marker = read_json(run.run_dir / 'desktop-driver-state.json')
                self.assertEqual(marker['business_state'], state)
                self.assertEqual(marker['exit_code'], expected)
                self.assertEqual(marker['execution_run_id'], run.run_id)
                self.assertEqual(read_json(run.run_dir / 'run.json'), header)
                self.assertEqual(self.store.get(self.identifier)['supervisor'], json.dumps({'kind': 'systemd', 'unit': 'registered.service'}))
                if state == 'failed':
                    self.assertEqual(marker['terminal_reason'], 'raw_failure')
                    self.assertEqual(marker['failures'][0]['error_code'], 'dependency_patch_conflict')
                    self.assertEqual(marker['failures'][0]['detail'], 'raw dependency patch conflict')
                    self.assertEqual(self.store.status(self.identifier)['status'], 'failed')

    def test_driver_terminal_relaunch_reads_sdk_and_preserves_reason_without_resume(self):
        operations, run, header = self.create_instance()
        with operations.session(run.run_dir, run.run_id) as (_, _, _, sdk):
            terminal = sdk.apply_operations(run.run_id, command_id='desktop-terminal',
                expected_revision=run.snapshot['revision'], expected_generation=run.snapshot['generation'],
                operations=[Operations.finish('failed')], application_state={'stop_reason': 'dependency_patch_conflict'})
        marker_path = run.run_dir / 'desktop-driver-state.json'
        atomic_json(marker_path, {'execution_run_id': run.run_id, 'business_state': 'failed',
            'sdk_revision': terminal['revision'], 'terminal_reason': 'dependency_patch_conflict',
            'failures': [{'task_id': 'coder.HB-06', 'error_code': 'dependency_patch_conflict', 'detail': 'raw patch conflict'}]})
        # The stale UI projection still says running; SDK authority wins.
        self.assertEqual(self.store.status(self.identifier)['status'], 'queued')
        with patch('modport.desktop_driver.restore_host_environment', side_effect=AssertionError('terminal Run needs no credentials')), \
                patch.object(operations, 'resume', side_effect=AssertionError('terminal Run must not replay')):
            self.assertEqual(run_instance(self.store.root, self.identifier, operations=operations), 78)
        marker = read_json(marker_path)
        self.assertEqual(marker['terminal_reason'], 'dependency_patch_conflict')
        self.assertEqual(marker['failures'][0]['detail'], 'raw patch conflict')
        self.assertEqual(self.store.status(self.identifier)['status'], 'failed')
        self.assertEqual(read_json(run.run_dir / 'run.json'), header)

    def test_terminal_driver_settles_unexecuted_queued_message(self):
        operations, run, _ = self.create_instance()
        self.store.enqueue_message(self.identifier, 'Queued before terminal settlement')
        with operations.session(run.run_dir, run.run_id) as (_, _, _, sdk):
            sdk.apply_operations(run.run_id, command_id='desktop-message-terminal',
                expected_revision=run.snapshot['revision'], expected_generation=run.snapshot['generation'],
                operations=[Operations.finish('failed')], application_state={})
        with patch.object(operations, 'resume', side_effect=AssertionError('terminal SDK must not replay')):
            self.assertEqual(run_instance(self.store.root, self.identifier, operations=operations), 78)
        status = self.store.status(self.identifier)
        self.assertFalse(status['supervisor']['busy'])
        self.assertEqual(status['messages'][0]['state'], 'failed')
        self.assertIn('消息未获调度', status['messages'][1]['content'])

    def test_terminal_driver_marks_missing_running_reply_unverified(self):
        operations, run, _ = self.create_instance()
        message = self.store.enqueue_message(self.identifier, 'Projected running before interruption')
        self.store.settle_messages({'run_id': run.run_id, 'state': 'running',
            'tasks': {'desktop.chat.' + message['id']: {'attempts': [{'state': 'running'}]}}})
        with operations.session(run.run_dir, run.run_id) as (_, _, _, sdk):
            sdk.apply_operations(run.run_id, command_id='desktop-running-message-terminal',
                expected_revision=run.snapshot['revision'], expected_generation=run.snapshot['generation'],
                operations=[Operations.finish('cancelled')], application_state={})
        with patch.object(operations, 'resume', side_effect=AssertionError('terminal SDK must not replay')):
            self.assertEqual(run_instance(self.store.root, self.identifier, operations=operations), 78)
        status = self.store.status(self.identifier)
        self.assertFalse(status['supervisor']['busy'])
        self.assertEqual(status['messages'][0]['state'], 'failed')
        self.assertIn('尚未验证', status['messages'][1]['content'])
        self.assertNotIn('消息未获调度', status['messages'][1]['content'])

    def test_terminal_driver_task_inspection_failure_preserves_message_uncertainty(self):
        from modport.desktop_driver import read_run_availability
        operations, run, _ = self.create_instance()
        self.store.enqueue_message(self.identifier, 'Potentially dispatched before interruption')
        with operations.session(run.run_dir, run.run_id) as (_, _, _, sdk):
            sdk.apply_operations(run.run_id, command_id='desktop-unknown-message-terminal',
                expected_revision=run.snapshot['revision'], expected_generation=run.snapshot['generation'],
                operations=[Operations.finish('failed')], application_state={})
        def observe(root, execution_id, **kwargs):
            if kwargs['sample_limit']:
                raise RuntimeError('bounded task inspection unavailable')
            return read_run_availability(root, execution_id, **kwargs)
        with patch('modport.desktop_driver.read_run_availability', side_effect=observe), \
                patch.object(operations, 'resume', side_effect=AssertionError('terminal SDK must not replay')):
            self.assertEqual(run_instance(self.store.root, self.identifier, operations=operations), 78)
        status = self.store.status(self.identifier)
        self.assertFalse(status['supervisor']['busy'])
        self.assertIn('尚未验证', status['messages'][1]['content'])
        self.assertNotIn('消息未获调度', status['messages'][1]['content'])

    def test_driver_exception_keeps_host_failure_restartable_and_supervisor_identity(self):
        operations, run, _ = self.create_instance()
        self.store.update_launch(self.identifier, supervisor={'kind': 'systemd', 'unit': 'registered.service'})
        with patch('modport.desktop_driver.restore_host_environment'), \
                patch.object(operations, 'resume', side_effect=RuntimeError('raw host failure')):
            with self.assertRaisesRegex(RuntimeError, 'raw host failure'):
                run_instance(self.store.root, self.identifier, operations=operations)
        marker = read_json(run.run_dir / 'desktop-driver-state.json')
        self.assertEqual(marker['business_state'], 'running')
        self.assertEqual(marker['exit_classification'], 'host_failure')
        self.assertEqual(marker['exit_code'], 1)
        from modport.run_monitor import pid_namespace
        self.assertEqual(marker['pid_namespace'], pid_namespace())
        self.assertEqual(marker['execution_run_id'], run.run_id)
        self.assertEqual(self.store.get(self.identifier)['launch_error'], 'raw host failure')
        self.assertIsNotNone(self.store.get(self.identifier)['supervisor'])

    def test_confirmed_api_cancel_reaches_sdk_after_watchdog_service_stop_failure(self):
        operations, run, header = self.create_instance()
        driver_unit = f'modport-{self.identifier}.service'
        watchdog_unit = f'modport-{self.identifier}-watchdog.service'
        self.store.update_launch(self.identifier, supervisor={
            'kind': 'systemd', 'unit': driver_unit,
            'watchdog': {'kind': 'systemd', 'unit': watchdog_unit, 'state': 'running'}})
        calls = []
        def execute(argv, **kwargs):
            calls.append(argv)
            return subprocess.CompletedProcess(argv, 1, '', 'raw watchdog stop failure')
        supervisor = PersistentSupervisor(self.store, platform_name='Linux', execute=execute)
        application = DesktopApplication(self.store.root, operations=operations, supervisor=supervisor)
        cancel = operations.cancel
        def cancel_after_suppression(root, run_id):
            control = read_json(root / 'desktop-watchdog-control.json')
            self.assertTrue(control['suppressed'])
            self.assertEqual(control['reason'], 'user_cancelled')
            self.assertEqual(calls[-1][-1], watchdog_unit)
            return cancel(root, run_id)
        with patch.object(operations, 'cancel', side_effect=cancel_after_suppression) as requested:
            result = application.request('POST', f'/api/runs/{self.identifier}/cancel', {'confirmed': True})
        requested.assert_called_once_with(run.run_dir, run.run_id)
        from modport.run_monitor import read_run_availability
        self.assertEqual(read_run_availability(run.run_dir, run.run_id).run_state, 'cancelled')
        self.assertEqual(result['status'], 'cancelled')
        receipt = json.loads(self.store.get(self.identifier)['supervisor'])
        self.assertIn('raw watchdog stop failure', receipt['watchdog']['stop_error'])
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][-1], watchdog_unit)
        self.assertEqual(read_json(run.run_dir / 'run.json')['deadline_epoch'], header['deadline_epoch'])

    def test_windows_scheduler_suppresses_only_handled_coordination_exit(self):
        argv = ['--data-root', str(self.store.root), '--instance', self.identifier]
        for code, expected in [(0, 0), (78, 0), (1, 1)]:
            with self.subTest(code=code), patch('modport.desktop_driver.platform.system', return_value='Windows'), \
                    patch('modport.desktop_driver.run_instance', return_value=code):
                self.assertEqual(driver_main(argv), expected)
        with patch('modport.desktop_driver.platform.system', return_value='Linux'), \
                patch('modport.desktop_driver.run_instance', return_value=78):
            self.assertEqual(driver_main(argv), 78)

    def test_windows_marker_retains_semantic_failure_when_entrypoint_exits_zero(self):
        operations, run, _ = self.create_instance()
        snapshot = copy.deepcopy(run.snapshot)
        snapshot.update(state='failed', application_state={'stop_reason': 'raw_business_failure'})
        with patch('modport.desktop_driver.platform.system', return_value='Windows'), \
                patch('modport.desktop_driver.restore_host_environment'):
            # run_instance retains the semantic result; main alone applies the
            # Task Scheduler exit translation.
            with patch.object(operations, 'resume', return_value=SimpleNamespace(snapshot=snapshot)):
                self.assertEqual(run_instance(self.store.root, self.identifier, operations=operations), 78)
        marker = read_json(run.run_dir / 'desktop-driver-state.json')
        self.assertEqual(marker['business_state'], 'failed')
        self.assertEqual(marker['terminal_reason'], 'raw_business_failure')
        self.assertEqual(marker['exit_code'], 78)
        self.assertEqual(marker['process_exit_code'], 0)

    def test_current_sdk_successor_keeps_registered_desktop_identity_and_messages(self):
        operations, run, header = self.create_instance()
        message = self.store.enqueue_message(self.identifier, 'Read the successor evidence')
        successor_id = run.run_id + ':continuation'
        successor_header = {**header, 'run_id': successor_id, 'parent_run_id': run.run_id}
        with operations.session(run.run_dir, run.run_id) as (_, _, _, sdk):
            terminal = sdk.apply_operations(run.run_id, command_id='desktop-finish-segment',
                expected_revision=run.snapshot['revision'], expected_generation=run.snapshot['generation'],
                operations=[Operations.finish('failed')], application_state={})
            predecessor = copy.deepcopy(terminal)
            predecessor['application_state'] = {'acceptance_status': 'passed'}
            publish_snapshot(header, predecessor, force=True)
            successor = sdk.continue_run(run.run_id, successor_id, command_id='desktop-next-segment',
                expected_revision=terminal['revision'], expected_generation=terminal['generation'], input=successor_header)
        atomic_json(run.run_dir / 'run.json', successor_header)
        self.assertEqual(managed_instance_id(successor_header), self.identifier)
        # The current header changed before the driver published anything. Old
        # terminal/acceptance state cannot be relabelled as successor evidence.
        unpublished = self.store.status(self.identifier)
        self.assertEqual(unpublished['status'], 'running')
        self.assertEqual(unpublished['execution_run_id'], successor_id)
        self.assertEqual(unpublished['acceptance_status'], 'unverified')
        self.assertTrue(all(not group['items'] for group in unpublished['stages'].values()))
        # The queued message belongs to the logical instance; use a clean queue
        # to prove stale predecessor failure does not reject new messages.
        self.store.fail_message(message['id'], 'superseded test message')
        message = self.store.enqueue_message(self.identifier, 'Read the current successor evidence')
        successor['tasks']['desktop.chat.' + message['id']] = {'attempts': [{'state': 'succeeded',
            'command': {'payload': {'stage_id': 'supervisor'}},
            'result': {'value': {'status': 'completed', 'outputs': {'desktop_chat_reply': 'Current segment evidence'}}}}]}
        with patch('modport.desktop_driver.restore_host_environment'), \
                patch.object(operations, 'resume', return_value=SimpleNamespace(snapshot=successor)) as resume:
            self.assertEqual(run_instance(self.store.root, self.identifier, operations=operations), 1)
        resume.assert_called_once_with(run.run_dir, successor_id)
        status = self.store.status(self.identifier)
        self.assertEqual(status['id'], self.identifier)
        self.assertEqual(status['execution_run_id'], successor_id)
        self.assertEqual(status['messages'][-2]['state'], 'completed')
        self.assertEqual(status['messages'][-1]['content'], 'Current segment evidence')
        self.assertEqual(read_json(run.run_dir / 'desktop-driver-state.json')['execution_run_id'], successor_id)
        self.assertEqual(read_json(run.run_dir / 'run.json')['deadline_epoch'], header['deadline_epoch'])
        with operations.session(run.run_dir, successor_id) as (_, _, _, sdk):
            terminal = sdk.apply_operations(successor_id, command_id='desktop-finish-successor',
                expected_revision=successor['revision'], expected_generation=successor['generation'],
                operations=[Operations.finish('failed')], application_state={})
        predecessor_projection = read_json(run.run_dir / 'desktop-status.json')
        predecessor_projection.update(execution_run_id=run.run_id, acceptance_status='passed')
        predecessor_projection['stages']['implementation'].update(state='completed', items=[{'id': 'predecessor.task'}])
        atomic_json(run.run_dir / 'desktop-status.json', predecessor_projection)
        with patch.object(operations, 'resume', side_effect=AssertionError('terminal successor must not replay')):
            self.assertEqual(run_instance(self.store.root, self.identifier, operations=operations), 78)
        current_projection = self.store.status(self.identifier)
        self.assertEqual(current_projection['sdk_revision'], terminal['revision'])
        self.assertEqual(current_projection['acceptance_status'], 'unverified')
        self.assertEqual(current_projection['stages']['implementation']['items'], [])

    def test_windows_task_requires_live_pid_birth_and_driver_lease(self):
        _, run, _ = self.create_instance()
        calls = []
        def execute(argv, **kwargs):
            calls.append(argv)
            if argv[0] == 'whoami':
                return subprocess.CompletedProcess(argv, 0, '"HOST\\user","S-1-5-21-123-456-789-1001"\n', '')
            if '/Run' in argv:
                atomic_json(run.run_dir / 'desktop-driver-state.json', {'instance': self.identifier, 'pid': 12345, 'birth': 'observed-test-birth', 'at': time.time(), 'state': 'launching'})
                if any('watchdog' in item for item in argv):
                    atomic_json(run.run_dir / 'desktop-watchdog-state.json', {'instance': self.identifier,
                        'pid': 12346, 'birth': 'observed-test-birth', 'at': time.time(), 'state': 'running'})
            return subprocess.CompletedProcess(argv, 0, '', '')
        supervisor = PersistentSupervisor(self.store, platform_name='Windows', execute=execute)
        health = {'pid': 12345, 'birth': 'observed-test-birth', 'status': 'running'}
        with patch('modport.platform_runtime.process_birth', return_value='observed-test-birth'), patch('modport.runner.read_driver_health', return_value=health):
            result = supervisor.launch(self.identifier)
        self.assertEqual(result['driver_state'], 'launching')
        self.assertEqual(result['native_acceptance'], 'pending')
        xml = (self.store.root / 'supervision' / (result['task'] + '.xml')).read_text(encoding='utf-16')
        self.assertIn('InteractiveToken', xml)
        self.assertIn('S-1-5-21-123-456-789-1001', xml)
        self.assertIn('RestartOnFailure', xml)
        self.assertIn('PT0S', xml)
        self.assertTrue(any('/Run' in call for call in calls))

    def test_source_mod_identity_cache_is_bound_to_repository_revision(self):
        class Supervisor:
            def check(self): return True, 'test'
            def launch(self, identifier, *, environment=None): return {'kind': 'test'}
        operations = MigrationOperations(handlers={'modport.supervisor': ConversationHandler()}, isolation_mode='thread')
        application = DesktopApplication(self.store.root, operations=operations, supervisor=Supervisor())
        body = {'project_name': 'Independent display name', 'source_repository': 'https://github.com/example/modrepo', 'source_revision': 'main', 'source_minecraft': '1.20.1', 'target_minecraft': '1.21.1', 'max_seconds': 600, 'max_tokens': 10000, 'model_config': {'default': {'model': 'test-model', 'reasoning_effort': 'low'}}}
        metadata = {'branches': ['main'], 'tags': [], 'default_revision': 'main', 'detected': {'mod_id': 'actual_source_mod'}, 'warnings': []}
        with patch('modport.desktop_service.inspect_repository', return_value=metadata), patch.object(application, 'environment', return_value={'ready': True, 'checks': []}):
            application.request('POST', '/api/repository', {'repository': body['source_repository']})
            result = application.create_run(body)
            header = read_json(application.state.run_dir(result['id']) / 'run.json')
            self.assertEqual(header['request']['mod_id'], 'actual_source_mod')
            self.assertEqual(result['project_name'], body['project_name'])
            other = application.create_run({**body, 'source_revision': 'another-branch'})
            other_header = read_json(application.state.run_dir(other['id']) / 'run.json')
            self.assertEqual(other_header['request']['mod_id'], 'modrepo')
            self.assertIn('mod_id', application.state.status(other['id'])['notice'])

    def test_fabric_fields_reach_frozen_submission(self):
        class Supervisor:
            def launch(self, identifier, *, environment=None): return {'kind': 'test'}
        operations = MigrationOperations(isolation_mode='thread')
        application = DesktopApplication(self.store.root, operations=operations, supervisor=Supervisor())
        body = {'project_name': 'Fabric fields', 'source_repository': 'https://github.com/example/project',
                'source_minecraft': '1.20.1', 'target_minecraft': '1.21.1',
                'source_loader_version': '0.15.0', 'target_loader_version': '0.16.0',
                'max_seconds': 600, 'max_tokens': 10000,
                'model_config': {'default': {'model': 'test-model', 'reasoning_effort': 'low'}}}
        with patch.object(application, 'environment', return_value={'ready': True, 'checks': []}):
            for source, target in [('fabric', 'neoforge'), ('forge', 'fabric')]:
                with self.subTest(source=source, target=target):
                    result = application.request('POST', '/api/runs', {**body, 'source_loader': source, 'target_loader': target})
                    header = read_json(application.state.run_dir(result['id']) / 'run.json')
                    self.assertEqual(header['definition']['workflow_version'], WORKFLOW_VERSION)
                    for key, value in {'source_loader': source, 'target_loader': target,
                                       'source_loader_version': '0.15.0', 'target_loader_version': '0.16.0'}.items():
                        self.assertEqual(header['request'][key], value)

    def test_model_settings_api_persists_and_reaches_new_launch_privately(self):
        class Supervisor:
            def launch(self, identifier, *, environment=None):
                self.environment = environment
        supervisor = Supervisor()
        application = DesktopApplication(self.store.root, operations=MigrationOperations(isolation_mode='thread'), supervisor=supervisor)
        settings = {'providers': [{'id': 'local', 'name': 'Local', 'api_type': 'openai-compatible',
                    'base_url': 'http://127.0.0.1:12345/v1', 'api_key': 'desktop-test-secret',
                    'models': [{'id': name, 'context_window': 64000, 'max_output_tokens': 4000, 'reasoning_efforts': ['high']}
                               for name in ['test-model', 'child-model']]}],
                    'model_config': {'default': {'model': 'local/test-model', 'reasoning_effort': 'high'},
                                     'roles': {'subagent': {'model': 'local/child-model', 'reasoning_effort': 'high'}}}}
        saved = application.request('POST', '/api/model-settings', settings)
        reopened = DesktopApplication(self.store.root, operations=application.operations, supervisor=supervisor)
        self.assertEqual(reopened.request('GET', '/api/model-settings'), saved)
        self.assertNotIn('desktop-test-secret', json.dumps(saved))
        body = {'project_name': 'Configured model', 'source_repository': 'https://github.com/example/project',
                'source_minecraft': '1.20.1', 'target_minecraft': '1.21.1',
                'max_seconds': 600, 'max_tokens': 10000, 'model_config': saved['model_config']}
        with patch.object(reopened, 'environment', return_value={'ready': True, 'checks': []}):
            self.assertEqual(reopened.bootstrap()['model_config'], saved['model_config'])
            result = reopened.create_run(body)
        header = read_json(reopened.state.run_dir(result['id']) / 'run.json')
        self.assertNotIn('desktop-test-secret', json.dumps(header))
        self.assertEqual(header['model_policy']['default']['model'], 'local/test-model')
        self.assertEqual(header['model_policy']['roles']['subagent']['model'], 'local/child-model')
        self.assertIn('desktop-test-secret', supervisor.environment.values())

    def test_bootstrap_subagent_default_tracks_saved_coder(self):
        application = DesktopApplication(self.store.root, supervisor=object())
        with patch.object(application, 'environment', return_value={'ready': True, 'checks': []}):
            result = application.bootstrap()
        role = next(row for row in result['roles'] if row['id'] == 'subagent')
        self.assertEqual(result['model_config']['roles']['coder'], role['selection'])
        self.assertNotIn('subagent', result['model_config']['roles'])

    def test_local_selection_token_freezes_current_files_for_submission(self):
        class Supervisor:
            def launch(self, identifier, *, environment=None): return {'kind': 'test'}
        application = DesktopApplication(self.store.root, operations=MigrationOperations(isolation_mode='thread'), supervisor=Supervisor())
        with tempfile.TemporaryDirectory(prefix='modport-local-project-') as directory:
            source = Path(directory)
            (source / 'gradle.properties').write_text('mod_id=closed_source\nminecraft_version=1.20.1\nforge_version=47.3.0\n')
            (source / 'Example.java').write_text('current source content')
            selection = application.request('POST', '/api/local-source', {'path': str(source)})
            self.assertEqual(selection['detected']['mod_id'], 'closed_source')
            self.assertEqual(selection['detected']['source_loader'], 'forge')
            body = {'project_name': 'Closed source', 'source_mode': 'local', 'local_source_token': selection['token'],
                    'source_minecraft': '1.20.1', 'target_minecraft': '1.21.1',
                    'max_seconds': 600, 'max_tokens': 10000,
                    'model_config': {'default': {'model': 'test-model', 'reasoning_effort': 'low'}}}
            with patch.object(application, 'environment', return_value={'ready': True, 'checks': []}):
                with self.assertRaisesRegex(ValueError, '重新选择'):
                    application.create_run({**body, 'local_source_token': str(source)})
                result = application.create_run(body)
            root = application.state.run_dir(result['id'])
            header = read_json(root / 'run.json')
            details = read_json(root / 'desktop-instance.json')['source_details']
            snapshot = Path(details['snapshot_path'])
            self.assertEqual(header['request']['source_repository'], snapshot.as_uri())
            self.assertEqual(header['request']['source_revision'], details['source_revision'])
            self.assertIs(header['request']['source_snapshot'], True)
            self.assertEqual(header['request']['mod_id'], 'closed_source')
            (source / 'Example.java').write_text('later local edit')
            self.assertEqual((snapshot / 'Example.java').read_text(), 'current source content')
            self.assertFalse((source / '.git').exists())

    def test_api_rejects_arbitrary_paths_parameters_and_unconfirmed_cancel(self):
        application = DesktopApplication(self.store.root)
        for path in ['/api/runs/../../etc', '/api/runs/arbitrary', '/api/command', '/api/bootstrap?path=/etc/passwd']:
            with self.subTest(path=path), self.assertRaises((ValueError, KeyError)):
                application.request('GET', path)
        with self.assertRaises(ValueError):
            application.request('POST', '/api/setup', {'action': 'shell', 'command': 'whoami'})
        with self.assertRaises(ValueError):
            application.create_run({'output_root': '/etc', 'project_name': 'Unsafe'})

    def test_authenticated_loopback_host_and_origin_boundaries(self):
        class Application:
            def request(self, method, path, body):
                return {'workflow_version': WORKFLOW_VERSION}
        token = 'b' * 64
        try:
            server = DesktopServer(('127.0.0.1', 0), Application(), token)
        except PermissionError as error:
            self.skipTest('restricted sandbox cannot bind loopback; run focused test outside sandbox: ' + str(error))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            port = server.server_address[1]
            def request(headers):
                connection = http.client.HTTPConnection('127.0.0.1', port, timeout=5)
                connection.request('GET', '/api/bootstrap', headers=headers)
                response = connection.getresponse()
                data = response.read()
                connection.close()
                return response.status, data
            self.assertEqual(request({})[0], 401)
            self.assertEqual(request({'Authorization': 'Bearer ' + token})[0], 200)
            self.assertEqual(request({'Authorization': 'Bearer ' + token, 'Host': 'evil.invalid'})[0], 403)
            self.assertEqual(request({'Authorization': 'Bearer ' + token, 'Origin': 'https://evil.invalid'})[0], 403)
            self.assertEqual(request({'Authorization': 'Bearer ' + 'x' * 1000})[0], 401)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)


if __name__ == '__main__':
    unittest.main()
