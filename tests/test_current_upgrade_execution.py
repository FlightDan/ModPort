"""The selected predecessor upgrades without reviving a stopped desktop watchdog."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from dispatcher_sdk.orchestrator import Orchestrator, Operations

from modport.application_state_storage import hydrate_run_snapshot
from modport.continuation import _upgrade_watchdog_policy, continue_from_planner
from modport.contracts import OperationResult
from modport.evidence import atomic_json
from modport.kernel_runtime import open_runtime
from modport.memory_admission import MemorySnapshot
from modport.models import Budget, MigrationRequest
from modport.operations import MigrationOperations
from modport.sdk_compat import SDK_VERSION
from modport import workflow_upgrade
from modport.workflow import WORKFLOW_VERSION, WorkflowDefinition


class PrepareOnlyHandler:
    __execution_kernel_revision__ = 'upgrade-prepare-only-test'

    def __call__(self, _command):
        raise AssertionError('preparing an upgrade must not execute any assignment')


class CurrentUpgradeExecutionTests(unittest.TestCase):
    def test_public_sdk_upgrade_preserves_history_deadline_workspace_and_watchdog_stop(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            previous_id = 'selected-predecessor'
            successor_id = 'current-successor'
            request = MigrationRequest('fixture', 'https://example.invalid/fixture.git',
                '1.20.1', '26.1.2', budget=Budget(max_seconds=3600, max_agent_assignments=30)).to_dict()
            request.pop('wiki_enabled')
            definition = WorkflowDefinition(request,
                version=workflow_upgrade.UPGRADE_SOURCE_WORKFLOW_VERSION).to_dict()
            handlers = {row['handler_id']: PrepareOnlyHandler() for row in definition['stages']}
            owner = MigrationOperations(handlers=handlers, isolation_mode='thread',
                memory_probe=lambda: MemorySnapshot(64 * 1024**3, 64 * 1024**3, 'test capacity'))
            header = {'format_version': 2, 'run_id': previous_id, 'run_dir': str(root),
                'logical_run_id': 'earlier-historical-segment',
                'request': request, 'definition': definition, 'sdk_version': SDK_VERSION,
                'sdk_identity': {}, 'initial_refs': {}, 'prior_findings': [],
                'rubric_sha256': 'host-rubric-provenance', 'started_at': time.time(),
                'deadline_epoch': time.time() + 3600,
                'watchdog_policy': {'enabled': True, 'inactivity_seconds': 600}}
            app = owner._new_application()
            failure = OperationResult('failed', previous_id, 'target_build', 'target_build',
                previous_id + ':target_build:1', detail='retained compiler failure', error_code='target_build_failed')
            app['effective']['target_build'] = failure.to_dict()
            app.update(agent_assignments=5, terminal_reason='target_build_failed', active_stage=None)
            workspace = root / 'worktree'
            workspace.mkdir()
            (workspace / 'legal-current-edit.txt').write_text('legal edit before upgrade\n')
            control = root / 'desktop-watchdog-control.json'
            atomic_json(control, {'instance': root.name, 'suppressed': True,
                                  'reason': 'user_stop', 'at': time.time()})
            control_bytes = control.read_bytes()
            with open_runtime(root, handlers=handlers, isolation_mode='thread') as runtime:
                header['registry_revision'] = runtime.registry_revision
                atomic_json(root / 'run.json', header)
                sdk = Orchestrator(root / 'orchestrator.sqlite3', runtime.kernel, runtime=runtime)
                try:
                    sdk.create_run(previous_id, command_id='create', input=header, definition=definition)
                    sdk.apply_operations(previous_id, command_id='settle-failed', expected_revision=0,
                        operations=[Operations.finish('failed')], application_state=app)
                    original = deepcopy(hydrate_run_snapshot(root, sdk.get_run(previous_id)))
                finally:
                    sdk.close()
            frozen_header = deepcopy(header)
            with patch.object(workflow_upgrade, 'UPGRADE_SOURCE_RUN_ID', previous_id):
                successor = continue_from_planner(owner, root, previous_id, next_run_id=successor_id,
                    reason='User-authorized current workflow after merge repair', upgrade_workflow=True)
            current = successor.snapshot['input']
            self.assertEqual(WORKFLOW_VERSION, current['definition']['workflow_version'])
            self.assertEqual(workflow_upgrade.UPGRADE_SOURCE_WORKFLOW_VERSION,
                             current['workflow_upgrade']['from_version'])
            self.assertEqual(WORKFLOW_VERSION, current['workflow_upgrade']['to_version'])
            self.assertEqual(header['deadline_epoch'], current['deadline_epoch'])
            self.assertEqual(header['request']['budget'], current['request']['budget'])
            self.assertNotIn('wiki_enabled', current['request'])
            self.assertEqual('earlier-historical-segment', current['logical_run_id'])
            self.assertEqual(5, current['continuation']['agent_assignments_carried'])
            self.assertFalse(current['watchdog_policy']['enabled'])
            self.assertEqual(control_bytes, control.read_bytes())
            self.assertEqual('legal edit before upgrade\n', (workspace / 'legal-current-edit.txt').read_text())
            archived = json.loads((root / 'artifacts/continuations' / previous_id / 'run.json').read_text())
            self.assertEqual(frozen_header, archived)
            self.assertEqual(WORKFLOW_VERSION, json.loads((root / 'workflow-definition.json').read_text())['workflow_version'])
            with open_runtime(root, handlers=handlers, isolation_mode='thread') as runtime:
                sdk = Orchestrator(root / 'orchestrator.sqlite3', runtime.kernel, runtime=runtime)
                try:
                    prior = hydrate_run_snapshot(root, sdk.get_run(previous_id))
                    self.assertEqual(original['input'], prior['input'])
                    self.assertEqual(original['definition'], prior['definition'])
                    self.assertEqual(original['application_state'], prior['application_state'])
                finally:
                    sdk.close()
            self.assertTrue(successor.snapshot['tasks'])
            self.assertTrue(all(row['attempts'][-1]['state'] == 'pending_dispatch'
                                for row in successor.snapshot['tasks'].values()))

    def test_disabled_policy_survives_upgrade_without_desktop_control(self):
        with tempfile.TemporaryDirectory() as directory:
            policy, diagnostic = _upgrade_watchdog_policy(Path(directory), {
                'watchdog_policy': {'enabled': False, 'inactivity_seconds': 600}})
            self.assertFalse(policy['enabled'])
            self.assertIsNone(diagnostic)

    def test_unreadable_stop_control_is_diagnostic_and_does_not_enable_watchdog(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'desktop-watchdog-control.json').write_text('invalid')
            policy, diagnostic = _upgrade_watchdog_policy(root, {
                'watchdog_policy': {'enabled': True, 'inactivity_seconds': 600}})
            self.assertFalse(policy['enabled'])
            self.assertTrue(diagnostic)


if __name__ == '__main__':
    unittest.main()
