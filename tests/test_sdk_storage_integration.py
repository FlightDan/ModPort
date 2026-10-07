"""Current SDK storage, event cursors, and generation fence checks."""
from pathlib import Path
import json
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from dispatcher_sdk.orchestrator import RevisionConflict
from fixtures_modport import Clock, registry
from modport import MigrationOperations, MigrationRequest
from modport.operations import SUBSCRIPTION
from modport.application_state_storage import unpack_application_state
from modport.workflow import WORKFLOW_VERSION


class SDKStorageIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / 'run'
        self.operations = MigrationOperations(handlers=registry(), isolation_mode='thread', clock=Clock())
        self.operations.submit(MigrationRequest('mod', 'https://example.invalid/mod.git', '1.20.1', '26.1.2'),
                               run_dir=self.root, run_id='test')

    def test_reopened_generation_can_make_policy_decisions_and_ack_events(self):
        with self.operations.session(self.root, 'test') as (_, header, runtime, sdk):
            self.assertEqual(header['definition']['workflow_version'], WORKFLOW_VERSION)
            original_input = sdk.get_run('test')['input']
            failed = sdk.apply_operations('test', command_id='stop', expected_revision=0,
                operations=[{'kind': 'finish', 'state': 'failed'}])
            sdk.reopen_run('test', command_id='reopen', expected_revision=failed['revision'],
                actor='test', authorization_source='regression-test', reason='test-recovery',
                target_deployment={'registry_revision': runtime.registry_revision},
                decision={'start_stage': 'source', 'reused_artifacts': [], 'invalidated_artifacts': []},
                application_state=self.operations._new_application())
            state = self.operations.tick(sdk, header)
            self.assertEqual(state['generation'], 1)
            self.assertIn('source', state['tasks'])
            command = state['tasks']['source']['attempts'][-1]['command']
            self.assertEqual(command['payload']['options']['workflow_version'], WORKFLOW_VERSION)
            sdk.flush()
            returned = runtime.run_once()
            self.assertIsNotNone(returned)
            self.assertEqual(returned.state, 'succeeded',
                             runtime.kernel.get_effect('modport:' + command['execution_id']).response)
            sdk.sync()
            state = self.operations.tick(sdk, header)
            attempt = state['tasks']['source']['attempts'][-1]
            self.assertEqual(attempt['state'], 'succeeded')
            result = attempt['result']['value']
            self.assertEqual(result['status'], 'completed')
            self.assertEqual(result['command_id'], command['execution_id'])
            self.assertEqual(state['application_state']['effective']['source'], result)
            self.assertTrue((self.root / 'artifacts/source.json').is_file())
            self.assertEqual(state['generation'], 1)
            self.assertEqual(state['input'], original_input)
            # An unchanged policy takes the cursor-only ACK branch.
            sdk.apply_operations('test', command_id='signal', expected_revision=state['revision'],
                expected_generation=1, operations=[{'kind': 'signal', 'signal_id': 'notice', 'payload': {}}])
            self.operations.tick(sdk, header)
            observed = sdk.observe('test', subscription=SUBSCRIPTION)
            self.assertEqual(observed['cursor'], observed['event_high_watermark'])
            with self.assertRaises(RevisionConflict):
                sdk.apply_operations('test', command_id='stale', expected_revision=observed['snapshot']['revision'],
                                     expected_generation=0, operations=[])

    def test_repeated_large_policy_state_keeps_sdk_history_bounded(self):
        large = "state-body-" + "x" * (512 * 1024)
        with self.operations.session(self.root, 'test') as (_, header, _, sdk):
            for revision in range(80):
                app = self.operations._new_application()
                app['repair_feedback'] = [{'body': large}]
                app['gap_revision'] = revision
                with patch.object(self.operations, '_decision', return_value=([], app)):
                    hydrated = self.operations.tick(sdk, header)
                self.assertEqual(app, hydrated['application_state'])

            stored = sdk.get_run('test')['application_state']
            self.assertEqual(app, unpack_application_state(self.root, stored))
            self.assertLess(len(json.dumps(stored)), 16 * 1024)

        with sqlite3.connect(self.root / 'orchestrator.sqlite3') as connection:
            largest = connection.execute(
                "SELECT MAX(length(CAST(value AS BLOB))) FROM sdk_run_history "
                "WHERE section='root' AND item_key='application_state'"
            ).fetchone()[0]
        self.assertLess(largest, 16 * 1024)
        self.assertLess((self.root / 'orchestrator.sqlite3').stat().st_size, 4 * 1024 * 1024)

    def test_terminal_audit_drains_all_pages_and_replays_without_duplicate_events(self):
        with self.operations.session(self.root, 'test') as (_, header, _, sdk):
            for i in range(215):
                sdk.apply_operations('test', command_id=f'event-{i}', expected_revision=i,
                    operations=[{'kind': 'signal', 'signal_id': f's{i}', 'payload': {}}])
            sdk.apply_operations('test', command_id='stop', expected_revision=215,
                                 operations=[{'kind': 'finish', 'state': 'failed'}])
            captured = []
            with patch('modport.operations.record_sdk_events', side_effect=lambda root, events: captured.extend(events)):
                self.operations.tick(sdk, header)
                observed = sdk.observe('test', subscription=SUBSCRIPTION)
                self.assertEqual(observed['cursor'], observed['event_high_watermark'])
                self.assertGreater(len({event['sequence'] for event in captured}), 200)
                count = len(captured)
                self.operations.tick(sdk, header)
                self.assertEqual(count, len(captured))

    def test_audit_write_failure_does_not_ack_source_events(self):
        with self.operations.session(self.root, 'test') as (_, header, _, sdk):
            sdk.apply_operations('test', command_id='stop', expected_revision=0,
                                 operations=[{'kind': 'finish', 'state': 'failed'}])
            before = sdk.observe('test', subscription=SUBSCRIPTION)['cursor']
            with patch('modport.telemetry._connect', side_effect=OSError('disk unavailable')):
                with self.assertRaises(OSError):
                    self.operations.tick(sdk, header)
            self.assertEqual(sdk.observe('test', subscription=SUBSCRIPTION)['cursor'], before)

    def test_first_execute_tick_finishes_and_drains_its_final_event(self):
        run = self.operations.status(self.root, 'test')
        self.operations.clock.now += 50000
        result = self.operations.execute(run)
        self.assertEqual(result.status, 'failed')
        with self.operations.session(self.root, 'test') as (_, _, _, sdk):
            observed = sdk.observe('test', subscription=SUBSCRIPTION)
            self.assertEqual(observed['cursor'], observed['event_high_watermark'])
