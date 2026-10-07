from modport.memory_admission import MemorySnapshot
"""Recovery integration with real SDK storage; no legacy adapter or SQL writes."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from modport import MigrationRequest, MigrationOperations, Budget
from modport.evidence import candidate_fingerprint, verified_path, read_json, atomic_json
from fixtures_modport import registry, Clock


class SDKIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / 'run'
        self.clock = Clock()

    def create(self, *, budget=None, **fixture):
        self.operations = MigrationOperations(memory_probe=lambda: MemorySnapshot(64 * 1024**3, 64 * 1024**3, "fixture"), handlers=registry(**fixture), isolation_mode='thread', clock=self.clock)
        request = MigrationRequest('mod', 'https://example.invalid/mod.git', '1.20.1', '26.1.2',
                                   source_revision='a' * 40, budget=budget or Budget(max_seconds=1200))
        self.run = self.operations.submit(request, run_dir=self.root, run_id='recovery')

    def first(self, sdk, header):
        state = self.operations.tick(sdk, header)
        sdk.flush()
        return state['tasks']['source']['attempts'][0]['command']['execution_id']

    def test_live_lease_is_waited_for_then_expired_delivery_resumes_same_business_attempt(self):
        self.create()
        with self.operations.session(self.root, self.run.run_id) as (_, header, runtime, sdk):
            execution_id = self.first(sdk, header)
            claimed = runtime.kernel.claim('crashed-worker', registry_revision=runtime.registry_revision)
            self.assertIsNotNone(claimed)
            sdk.sync()
            for _ in range(15):
                state = self.operations.tick(sdk, header)
            self.assertEqual(state['state'], 'running')
            self.assertEqual(state['application_state']['rounds'], {})
            self.assertEqual(len(state['tasks']), 1)
            expires = runtime.kernel.get(execution_id).lease.expires_at
        with self.operations.session(self.root, self.run.run_id) as (_, header, runtime, sdk):
            self.assertIsNone(runtime.run_once())
            self.clock.now = expires + 1
            runtime.reap()
            self.assertEqual(runtime.kernel.get(execution_id).state, 'queued')
            self.assertIsNone(runtime.run_once())  # legitimate retry backoff
            self.clock.now = runtime.kernel.get(execution_id).next_attempt_at
            runtime.run_once()
            sdk.sync()
            current = sdk.get_run(self.run.run_id)['tasks']['source']['attempts'][0]
            self.assertEqual(current['state'], 'succeeded')
            self.assertEqual(current['kernel_snapshot']['attempt'], 2)
            self.assertEqual(len(sdk.get_run(self.run.run_id)['tasks']['source']['attempts']), 1)

    def test_exhausted_mechanical_budget_does_not_become_business_rework(self):
        self.create(budget=Budget(max_seconds=None, execution_max_attempts=1))
        with self.operations.session(self.root, self.run.run_id) as (_, header, runtime, sdk):
            execution_id = self.first(sdk, header)
            runtime.kernel.claim('crashed-worker', registry_revision=runtime.registry_revision)
            self.clock.now = runtime.kernel.get(execution_id).lease.expires_at + 1
            runtime.reap()
            sdk.sync()
            state = self.operations.tick(sdk, header)
            self.assertEqual(state['state'], 'failed')
            self.assertEqual(state['application_state']['terminal_reason'], 'execution_dead')
            self.assertEqual(state['application_state']['rounds'], {})

    def test_uncertain_effect_waits_without_budget_charge_or_blind_replay(self):
        self.create(raise_stage='source')
        with self.operations.session(self.root, self.run.run_id) as (_, header, runtime, sdk):
            execution_id = self.first(sdk, header)
            runtime.run_once()
            sdk.sync()
            for _ in range(8):
                state = self.operations.tick(sdk, header)
            self.assertEqual(state['tasks']['source']['attempts'][0]['state'], 'recovery_required')
            self.assertEqual(len([w for w in state['waits'].values() if w['state'] == 'open']), 1)
            self.assertEqual(state['application_state']['rounds'], {})
            self.assertIsNone(runtime.run_once())
        with self.assertRaisesRegex(ValueError, 'no complete stage receipt'):
            self.operations.recover(self.root, self.run.run_id)
        self.assertEqual(self.operations.status(self.root, self.run.run_id).status, 'waiting')

    def test_complete_stage_receipt_recovers_commit_gap_without_reexecuting_handler(self):
        self.create()
        with self.operations.session(self.root, self.run.run_id) as (_, header, runtime, sdk):
            execution_id = self.first(sdk, header)
            with patch.object(runtime.kernel, 'commit_effect', side_effect=RuntimeError('crash before effect commit')):
                runtime.run_once()
            sdk.sync()
            state = self.operations.tick(sdk, header)
            self.assertEqual(state['tasks']['source']['attempts'][0]['state'], 'recovery_required')
            self.assertTrue(
                (self.root / 'artifacts/executions' / execution_id / 'receipt.json').is_file(),
                runtime.kernel.get_effect('modport:' + execution_id).response)
        restored = self.operations.recover(self.root, self.run.run_id)
        self.assertEqual(restored.status, 'running')
        with self.operations.session(self.root, self.run.run_id) as (_, header, runtime, sdk):
            with patch('fixtures_modport.FixtureHandler.__call__', side_effect=AssertionError('must reuse receipt')):
                runtime.run_once()
            sdk.sync()
            self.assertEqual(sdk.get_run(self.run.run_id)['tasks']['source']['attempts'][0]['state'], 'succeeded')

    def test_changed_artifact_allows_complete_recovery_receipt(self):
        self.create()
        with self.operations.session(self.root, self.run.run_id) as (_, header, runtime, sdk):
            self.first(sdk, header)
            with patch.object(runtime.kernel, 'commit_effect', side_effect=RuntimeError('gap')):
                runtime.run_once()
            sdk.sync()
            self.operations.tick(sdk, header)
        (self.root / 'artifacts/source.json').write_text('{"source_commit":"wrong"}')
        restored = self.operations.recover(self.root, self.run.run_id)
        self.assertEqual(restored.status, 'running')
        with self.operations.session(self.root, self.run.run_id) as (_, header, runtime, sdk):
            with patch('fixtures_modport.FixtureHandler.__call__', side_effect=AssertionError('must reuse receipt')):
                self.assertEqual(runtime.run_once().state, 'succeeded')

    def test_committed_effect_redelivery_reuses_result_after_artifact_changes(self):
        self.create()
        with self.operations.session(self.root, self.run.run_id) as (_, header, runtime, sdk):
            execution_id = self.first(sdk, header)
            with patch.object(runtime.kernel, 'complete', side_effect=RuntimeError('crash before execution result')):
                with self.assertRaisesRegex(RuntimeError, 'crash before execution result'):
                    runtime.run_once()
            self.assertEqual(runtime.kernel.get_effect(f'modport:{execution_id}').state, 'committed')
            (self.root / 'artifacts/source.json').write_text('{"source_commit":"changed"}')
            self.clock.now = runtime.kernel.get(execution_id).lease.expires_at + 1
            runtime.reap()
            self.clock.now = runtime.kernel.get(execution_id).next_attempt_at
            with patch('fixtures_modport.FixtureHandler.__call__', side_effect=AssertionError('must reuse committed effect')):
                result = runtime.run_once()
            self.assertEqual(result.state, 'succeeded')

    def test_current_source_report_change_does_not_block_next_stage(self):
        self.create()
        with self.operations.session(self.root, self.run.run_id) as (_, header, runtime, sdk):
            self.first(sdk, header)
            runtime.run_once()
            sdk.sync()
            state = self.operations.tick(sdk, header)
            source_ref = state['application_state']['effective']['source']['outputs']['artifact_refs']['source_evidence']
            verified_path(self.root, source_ref)
            (self.root / 'artifacts/source.json').write_text('{"source_commit":"forged"}')
            sdk.flush()
            result = runtime.run_once()
            self.assertEqual(result.state, 'succeeded')
            self.assertEqual(verified_path(self.root, source_ref).read_text(), '{"source_commit":"forged"}')

    def test_recovery_rejects_changed_receipt_operation_identity(self):
        self.create()
        with self.operations.session(self.root, self.run.run_id) as (_, header, runtime, sdk):
            execution_id = self.first(sdk, header)
            with patch.object(runtime.kernel, 'commit_effect', side_effect=RuntimeError('gap')):
                runtime.run_once()
            sdk.sync()
            self.operations.tick(sdk, header)
        path = self.root / 'artifacts/executions' / execution_id / 'receipt.json'
        original = read_json(path)
        for field, value in [('execution_id', 'other-command'), ('effect_request', {})]:
            with self.subTest(field=field):
                atomic_json(path, {**original, field: value})
                with self.assertRaisesRegex(ValueError, 'does not match the frozen operation'):
                    self.operations.recover(self.root, self.run.run_id)

    def test_cancellation_before_dispatch_cannot_run_late_command(self):
        self.create()
        with self.operations.session(self.root, self.run.run_id) as (_, header, runtime, sdk):
            state = self.operations.tick(sdk, header)
            self.operations.tick(sdk, header, stop_reason='user_cancelled')
            sdk.flush()
            sdk.sync()
            state = self.operations.tick(sdk, header)
            self.assertEqual(state['state'], 'cancelled')
            self.assertIsNone(runtime.run_once())
            self.assertFalse((self.root / 'artifacts/source.json').exists())

    def test_handler_configuration_change_is_rejected_on_resume(self):
        self.create()
        other = MigrationOperations(memory_probe=lambda: MemorySnapshot(64 * 1024**3, 64 * 1024**3, "fixture"), handlers=registry(fail_stage='source', fail_count=1), isolation_mode='thread', clock=self.clock)
        observed = other.status(self.root, self.run.run_id)
        self.assertEqual(observed.snapshot, self.run.snapshot)
        with self.assertRaisesRegex(ValueError, 'deployment changed'):
            other.execute(observed)

    def test_candidate_identity_does_not_hash_mutable_source_files(self):
        root = Path(self.tmp.name)
        source = root / 'src/main/java/example/build/Main.java'
        source.parent.mkdir(parents=True)
        source.write_text('class Main {}')
        before = candidate_fingerprint(root)
        source.write_text('class Main { int changed; }')
        self.assertEqual(before, candidate_fingerprint(root))
        with patch('modport.evidence.file_digest', side_effect=AssertionError('must not hash source')):
            self.assertEqual(candidate_fingerprint(root, host_generation=3), '3')
