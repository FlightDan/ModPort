"""Web reads real SDK snapshots without advancing application state."""
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from dispatcher_sdk.execution_kernel import ExecutionCommandV2, RetryPolicy
from dispatcher_sdk.orchestrator import Orchestrator
from modport.sdk_compat import SDK_VERSION
from modport.web_data import ProgressStore, _LIST_SNAPSHOT_MAX_BYTES, _safe, _step
from modport import MigrationOperations, MigrationRequest
from fixtures_modport import registry


class DetailCompatibilityStore(ProgressStore):
    """Exercise the retained full-detail reader used by on-demand step views."""

    def list_runs(self):
        with self._lock:
            rows = []
            for identifier, path in self._discover().items():
                try:
                    large = self._database_footprint(path) > _LIST_SNAPSHOT_MAX_BYTES
                except Exception:
                    large = False
                row = self._list_preview(identifier, path) if large else self._get(identifier, path)
                rows.append(self._with_driver_health(row, path))
            return sorted(rows, key=lambda row: (row['state'] in {'running', 'waiting'}, row['created_at']), reverse=True)

    def get_run(self, identifier):
        with self._lock:
            path = self._discover().get(identifier)
            if path is None:
                raise KeyError(identifier)
            if self._database_footprint(path) > _LIST_SNAPSHOT_MAX_BYTES:
                return self._with_driver_health(self._large_detail(identifier, path), path)
            return self._with_driver_health(self._get(identifier, path), path)

    def get_step(self, identifier, step_id):
        run = self.get_run(identifier)
        for group in run['groups']:
            for step in group['steps']:
                if step['id'] != step_id:
                    continue
                if step['result'] in {'暂无结果', 'completed', 'agent completed',
                                      'agent assignment completed', 'coding agent completed',
                                      'fixture result'}:
                    path = self._discover().get(identifier)
                    for ref in self._summary_refs.get(identifier, {}).get(step_id, []):
                        try:
                            summary = self._summary(path, ref)
                        except (OSError, ValueError, UnicodeError):
                            step['summary_error'] = '已有摘要不可用（引用无效、校验失败或文件过大）'
                            continue
                        if summary:
                            step['result'] = summary
                            step.pop('summary_error', None)
                            break
                return step
        raise KeyError(step_id)


class WebDataTests(unittest.TestCase):
    def test_health_refresh_does_not_depend_on_sdk_database_changes(self):
        row = {'state': 'running', 'active_steps': ['迁移执行']}
        health = {'pid': 123, 'birth': 'identity', 'status': 'running', 'timestamp': 100}
        with patch('modport.runner.read_driver_health', return_value=health), \
                patch('modport.run_monitor.process_alive', return_value=False):
            observed = ProgressStore._with_driver_health(row, Path('/unused'))
            with patch('modport.web_data.time.time', return_value=105):
                recent = ProgressStore._with_driver_health(row, Path('/unused'))
        self.assertEqual('running', row['state'])
        self.assertEqual('running', observed['execution_state'])
        self.assertEqual('interrupted', observed['state'])
        self.assertIn('不能直接重跑', observed['active_steps'][0])
        self.assertEqual('running', recent['execution_state'])
        self.assertEqual('running', recent['state'])
        self.assertEqual('uncertain', recent['runner_health']['status'])
        self.assertEqual(['迁移执行'], recent['active_steps'])

    def test_other_pid_namespace_keeps_recent_run_visible_as_running(self):
        row = {'state': 'running', 'active_steps': ['迁移执行']}
        health = {'pid': 123, 'birth': 'identity', 'pid_namespace': 'pid:[driver]',
                  'status': 'running', 'timestamp': 100}
        with patch('modport.runner.read_driver_health', return_value=health), \
                patch('modport.run_monitor.pid_namespace', return_value='pid:[observer]'), \
                patch('modport.run_monitor.process_alive', return_value=True) as alive, \
                patch('modport.web_data.time.time', return_value=105):
            observed = ProgressStore._with_driver_health(row, Path('/unused'))
        alive.assert_not_called()
        self.assertEqual('running', observed['state'])
        self.assertEqual('uncertain', observed['runner_health']['status'])

    def test_other_pid_namespace_stale_heartbeat_is_unresponsive(self):
        row = {'state': 'running', 'active_steps': ['迁移执行']}
        health = {'pid': 123, 'birth': 'identity', 'pid_namespace': 'pid:[driver]',
                  'status': 'running', 'timestamp': 100}
        with patch('modport.runner.read_driver_health', return_value=health), \
                patch('modport.run_monitor.pid_namespace', return_value='pid:[observer]'), \
                patch('modport.web_data.time.time', return_value=140):
            observed = ProgressStore._with_driver_health(row, Path('/unused'))
        self.assertEqual('interrupted', observed['state'])
        self.assertEqual('unresponsive', observed['runner_health']['status'])
        self.assertEqual('driver_heartbeat_stale_pid_namespace_unverified',
                         observed['runner_health']['reason'])

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.run = self.root / 'one'
        self.run.mkdir()
        self.header = {'format_version': 2, 'sdk_identity': {'source_version': SDK_VERSION},
                       'run_id': 'example', 'run_dir': str(self.run), 'started_at': 100,
                       'request': {'mod_id': 'Example', 'source_minecraft': '1.20.1',
                                   'target_minecraft': '26.1.2'}}
        self.write_header()
        self.sdk = Orchestrator(self.run / 'orchestrator.sqlite3', None)
        self.addCleanup(self.sdk.close)
        self.sdk.create_run('example', command_id='create', input=self.header)
        self.store = DetailCompatibilityStore(self.root)

    def write_header(self):
        (self.run / 'run.json').write_text(json.dumps(self.header))

    def expire(self):
        self.store._cache = {key: (0, fp, value) for key, (_, fp, value) in self.store._cache.items()}

    def test_real_snapshot_leaves_source_and_receipts_unchanged(self):
        original = self.sdk.get_run('example')
        receipt = self.sdk.get_command_receipt('example', 'create')
        database = self.run / 'orchestrator.sqlite3'
        before = hashlib.sha256(database.read_bytes()).hexdigest()
        result = self.store.list_runs()[0]
        self.assertFalse(result['stale'], result['error'])
        self.assertEqual('unverified', result['acceptance_status'])
        self.assertEqual(result['mod_id'], 'Example')
        self.assertEqual(result['source_version'], '1.20.1')
        self.assertEqual(len(result['id']), 64)
        self.assertEqual(self.sdk.get_run('example'), original)
        self.assertEqual(self.sdk.get_command_receipt('example', 'create'), receipt)
        self.assertEqual(hashlib.sha256(database.read_bytes()).hexdigest(), before)
        self.assertEqual(result['groups'][0]['steps'][0]['result'], '暂无结果')
        self.assertEqual(self.store.get_step(result['id'], result['groups'][0]['steps'][0]['id'])['label'], '获取源码')

    def test_singleflight_cache_and_defensive_copies(self):
        with patch.object(self.store, '_read', wraps=self.store._read) as read:
            with ThreadPoolExecutor(max_workers=6) as pool:
                results = list(pool.map(lambda _: self.store.list_runs(), range(6)))
            self.assertEqual(read.call_count, 1)
            results[0][0]['groups'].clear()
            self.assertTrue(self.store.list_runs()[0]['groups'])

    def test_large_database_list_uses_bounded_header_preview(self):
        identifier = hashlib.sha256(b'one').hexdigest()
        with patch.object(self.store, '_database_footprint', return_value=64 * 1024 * 1024 + 1), \
                patch('modport.web_data.snapshot_databases') as backup:
            row = self.store.list_runs()[0]
        self.assertEqual(row['id'], identifier)
        self.assertEqual(row['run_id'], 'example')
        self.assertTrue(row['stale'])
        self.assertEqual(row['groups'], [])
        self.assertIn('数据库较大', row['error'])
        backup.assert_not_called()
        # The preview is deliberately not cached: opening the selected Run
        # still follows the normal authenticated SDK snapshot path.
        with patch.object(self.store, '_read', wraps=self.store._read) as read:
            full = self.store.get_run(identifier)
        self.assertFalse(full['stale'], full['error'])
        self.assertTrue(full['groups'])
        read.assert_called_once()

    def test_large_detail_returns_while_snapshot_loads(self):
        identifier = hashlib.sha256(b'one').hexdigest()
        started = threading.Event()
        release = threading.Event()

        def delayed_read(*args):
            started.set()
            release.wait(2)
            return {'id': identifier, 'state': 'running', 'stale': False, 'groups': []}

        try:
            with patch.object(self.store, '_database_footprint', return_value=64 * 1024 * 1024 + 1), \
                    patch.object(self.store, '_read', side_effect=delayed_read):
                row = self.store.get_run(identifier)
                self.assertTrue(row['detail_pending'])
                self.assertIn('正在读取', row['error'])
                self.assertTrue(started.wait(1))
        finally:
            release.set()

    def test_real_fork_join_dependencies_match_projected_ids(self):
        graph = {'implementation': [], 'coder.g1.a': ['implementation'],
                 'coder.g1.b': ['implementation'],
                 'development_integrate': ['coder.g1.a', 'coder.g1.b']}
        operations = []
        for task_id, dependencies in graph.items():
            command = ExecutionCommandV2(
                execution_id=task_id, idempotency_key=task_id, registry_revision='fixture',
                correlation_id='example', causation_id=None, handler_id='fixture',
                handler_contract_version=1, retry_policy=RetryPolicy(), timeout_seconds=5,
                payload={'stage_id': task_id.split('.')[0]}).to_dict()
            operations.append({'kind': 'add_task', 'task_id': task_id,
                               'command': command, 'dependencies': dependencies})
        self.sdk.apply_operations('example', command_id='fork-join', expected_revision=0,
                                  operations=operations)
        original = self.sdk.get_run('example')
        row = self.store.list_runs()[0]
        self.assertFalse(row['stale'], row['error'])
        steps = {step['id']: step for group in row['groups'] for step in group['steps']}
        opaque = lambda value: hashlib.sha256(value.encode()).hexdigest()
        for task_id, dependencies in graph.items():
            step = steps[opaque(task_id)]
            self.assertTrue(step['scheduled'])
            self.assertEqual(step['dependencies'], list(map(opaque, dependencies)))
        placeholders = [step for step in steps.values() if not step['scheduled']]
        self.assertTrue(placeholders)
        self.assertTrue(all(step['dependencies'] == [] for step in placeholders))
        self.assertEqual(self.sdk.get_run('example'), original)

    def test_missing_dependency_references_remain_opaque_and_untruncated(self):
        dependencies = ['missing.%d' % index for index in range(25)]
        dependencies.append('missing.token=private-credential')
        snapshot = self.sdk.get_run('example')
        snapshot['tasks'] = {'coder.g1': {'task_id': 'coder.g1',
                                        'dependencies': dependencies, 'attempts': []}}
        with patch('modport.web_data.Orchestrator.get_run', return_value=snapshot):
            row = self.store.list_runs()[0]
        self.assertFalse(row['stale'], row['error'])
        steps = [step for group in row['groups'] for step in group['steps']]
        step = next(step for step in steps if step['scheduled'])
        self.assertEqual(step['dependencies'], [hashlib.sha256(value.encode()).hexdigest()
                                                 for value in dependencies])
        self.assertTrue(set(step['dependencies']).isdisjoint(item['id'] for item in steps))
        self.assertNotIn('private-credential', json.dumps(row))

    def test_dependency_metadata_does_not_expose_raw_identifiers(self):
        secret = 'dependency.token=private-credential'
        step = _step('coder', {'task_id': 'coder', 'dependencies': [secret], 'attempts': []})
        self.assertTrue(step['scheduled'])
        self.assertNotIn('private-credential', json.dumps(step))
        self.assertEqual(step['dependencies'], [hashlib.sha256(secret.encode()).hexdigest()])
        placeholder = _step('coder')
        self.assertFalse(placeholder['scheduled'])
        self.assertEqual(placeholder['dependencies'], [])

    def test_preserves_last_good_result_on_refresh_failure(self):
        original = self.store.list_runs()[0]
        self.expire()
        self.header['started_at'] = 101
        self.write_header()
        with patch('modport.web_data.snapshot_databases', side_effect=OSError('/secret/private/path')):
            failed = self.store.list_runs()[0]
        self.assertTrue(failed['stale'])
        self.assertEqual(failed['observed_at'], original['observed_at'])
        self.assertEqual(failed['groups'], original['groups'])
        self.assertNotIn('/secret', failed['error'])

    def test_unchanged_running_database_reuses_projection_after_ttl(self):
        original = self.store.list_runs()[0]
        self.assertEqual(original['state'], 'running')
        self.expire()
        with patch.object(self.store, '_read', side_effect=AssertionError('repeated backup')):
            refreshed = self.store.list_runs()[0]
        self.assertEqual(refreshed, original)

    def test_large_read_does_not_label_changed_source_as_fresh(self):
        identifier = hashlib.sha256(b'one').hexdigest()
        fingerprint = self.store._fingerprint(self.run)
        self.store._large_loads[identifier] = (fingerprint, 'running', None)

        def changing_read(*args):
            self.header['started_at'] = 101
            self.write_header()
            return {'id': identifier, 'state': 'running', 'stale': False, 'groups': []}

        with patch.object(self.store, '_read', side_effect=changing_read):
            self.store._load_large(identifier, self.run, fingerprint)
        self.assertNotIn(identifier, self.store._cache)
        self.assertEqual(self.store._large_loads[identifier][1], 'error')

    def test_snapshot_budget_error_is_visible_without_persistent_cache(self):
        from modport.snapshot_storage import SnapshotLimitError
        with patch('modport.web_data.snapshot_databases', side_effect=SnapshotLimitError('size limit')):
            row = self.store.list_runs()[0]
        self.assertTrue(row['stale'])
        self.assertIn('资源限制', row['error'])
        self.assertFalse((self.root / '.modport-web-cache').exists())

    def test_large_detail_retries_transient_failure_after_backoff(self):
        identifier = hashlib.sha256(b'one').hexdigest()
        fingerprint = self.store._fingerprint(self.run)
        self.store._large_loads[identifier] = (fingerprint, 'running', None)
        with patch.object(self.store, '_read', side_effect=OSError('busy')):
            self.store._load_large(identifier, self.run, fingerprint)
        retry_at = self.store._large_retry_at[identifier]
        with patch('modport.web_data.time.monotonic', return_value=retry_at - 1), \
                patch('modport.web_data.threading.Thread') as worker:
            row = self.store._large_detail(identifier, self.run)
        self.assertFalse(row['detail_pending'])
        worker.assert_not_called()
        with patch('modport.web_data.time.monotonic', return_value=retry_at + 1), \
                patch('modport.web_data.threading.Thread') as worker:
            row = self.store._large_detail(identifier, self.run)
        self.assertTrue(row['detail_pending'])
        worker.return_value.start.assert_called_once()

    def test_detail_backups_are_temporary_even_above_list_threshold(self):
        with patch.object(self.store, '_database_footprint', return_value=65 * 1024 * 1024):
            with self.store._snapshot('example', self.run) as snapshot:
                self.assertTrue(snapshot.is_file())
                directory = snapshot.parent
            self.assertFalse(directory.exists())
        self.assertFalse((self.root / '.modport-web-cache').exists())

    def test_terminal_cache_invalidates_on_database_change(self):
        self.sdk.apply_operations('example', command_id='finish', expected_revision=0,
                                  operations=[{'kind': 'finish', 'state': 'cancelled'}])
        original = self.store.list_runs()[0]
        self.assertEqual(original['state'], 'cancelled')
        self.assertEqual(original['runner_health']['status'], 'stopped')
        self.assertEqual(original['runner_health']['reason'], 'sdk_run_terminal')
        detail = self.store.get_run(original['id'])
        self.assertEqual(detail['runner_health']['status'], 'stopped')
        self.expire()
        with patch.object(self.store, '_read', wraps=self.store._read) as read:
            self.store.list_runs()
            self.assertEqual(read.call_count, 0)
            self.expire()
            self.header['started_at'] = 200
            self.write_header()
            updated = self.store.list_runs()[0]
            self.assertEqual(read.call_count, 1)
            self.assertTrue(updated['stale'])

    def test_invalid_and_legacy_runs_do_not_hide_valid_run(self):
        bad = self.root / 'bad'
        bad.mkdir()
        (bad / 'run.json').write_text('{')
        legacy = self.root / 'legacy'
        legacy.mkdir()
        (legacy / 'control.sqlite3').touch()
        rows = self.store.list_runs()
        self.assertEqual(len(rows), 3)
        self.assertEqual(sum(not row['stale'] for row in rows), 1)
        self.assertEqual(sum(row['state'] == 'unavailable' for row in rows), 2)
        legacy_row = next(row for row in rows if row['run_id'] == 'legacy')
        self.assertIn('旧版运行记录', legacy_row['error'])
        self.assertNotIn('FileNotFoundError', legacy_row['error'])
        self.assertEqual(legacy_row['groups'], [])

    def test_symlink_discovery_sidecars_and_unknown_ids(self):
        (self.root / 'alias').symlink_to(self.run, target_is_directory=True)
        self.assertEqual(len(self.store.list_runs()), 1)
        for identifier in ('../one', str(self.run), 'a' * 64):
            with self.assertRaises(KeyError):
                self.store.get_run(identifier)
        self.expire()
        (self.run / 'orchestrator.sqlite3-journal').symlink_to(self.root / 'outside')
        self.assertTrue(self.store.list_runs()[0]['stale'])

    def test_incompatible_database_rejected_before_constructor(self):
        with patch('modport.web_data.inspect_storage', return_value={'compatible': False, 'orchestrator_schema': 'unsupported'}), patch('modport.web_data.Orchestrator') as sdk:
            self.assertTrue(self.store.list_runs()[0]['stale'])
            sdk.assert_not_called()

    def test_business_failure_retry_and_context_allowlist(self):
        attempt = {'state': 'succeeded', 'command': {'payload': {'stage_id': 'target_build',
            'payload': {'request': {'source_minecraft': '1.20.1', 'target_minecraft': '26.1.2'},
                        'prompt': 'secret prompt'}, 'options': {'token': 'secret token'}}},
            'result': {'value': {'status': 'failed', 'detail': 'build failed password=hidden',
                'error_code': 'build_failed', 'outputs': {'summary': '<script>text</script>',
                'prompt': 'secret prompt', 'log': '/private/build.log'}}},
            'kernel_snapshot': {'attempt': 3}}
        step = _step('target_build', {'task_id': 'target_build.g2', 'dependencies': ['development_integrate'],
                                    'attempts': [attempt, attempt]})
        self.assertEqual(step['state'], 'failed')
        self.assertEqual(step['attempts'][0]['execution_retries'], 2)
        self.assertIn('返工', step['attempts'][-1]['label'])
        serialized = json.dumps(step)
        self.assertNotIn('hidden', serialized)
        self.assertNotIn('secret prompt', serialized)
        self.assertNotIn('/private/build.log', serialized)
        self.assertIn('源版本：1.20.1', step['inputs'])

    def test_all_active_parallel_tasks_and_waits_are_projected(self):
        snapshot = self.sdk.get_run('example')
        snapshot['tasks'] = {name: {'task_id': name, 'dependencies': [], 'attempts': [
            {'state': state, 'command': {'payload': {'stage_id': stage}}, 'result': None}]}
            for name, stage, state in [('coder.g1.a', 'coder', 'running'),
                                       ('coder.g1.b', 'coder', 'queued'),
                                       ('contract_draft', 'contract_draft', 'running')]}
        snapshot['waits'] = {'admin': {'state': 'open'}}
        with patch('modport.web_data.Orchestrator.get_run', return_value=snapshot):
            row = self.store.list_runs()[0]
        self.assertFalse(row['stale'], row['error'])
        self.assertEqual(len(row['active_steps']), 4)
        self.assertIn('等待外部处理', row['active_steps'])

    def test_real_modport_header_is_supported(self):
        from modport import MigrationRequest, MigrationOperations
        from fixtures_modport import registry
        operations = MigrationOperations(handlers=registry(), isolation_mode='thread')
        operations.submit(MigrationRequest('real', 'https://example.invalid/mod.git',
            '1.20.1', '26.1.2', source_revision='a' * 40),
            run_dir=self.root / 'real', run_id='real')
        rows = self.store.list_runs()
        row = next(row for row in rows if row['mod_id'] == 'real')
        self.assertFalse(row['stale'], row['error'])

    def test_waiting_and_terminal_reason(self):
        snapshot = self.sdk.get_run('example')
        snapshot['waits'] = {'admin': {'state': 'open'}}
        snapshot['application_state'] = {'terminal_reason': 'needs_credentials token=secret'}
        with patch('modport.web_data.Orchestrator.get_run', return_value=snapshot):
            row = self.store.list_runs()[0]
        self.assertEqual(row['state'], 'waiting')
        self.assertNotIn('secret', row['error'])
        self.expire()
        snapshot['state'] = 'failed'
        self.sdk.apply_operations('example', command_id='finish', expected_revision=0,
                                  operations=[{'kind': 'finish', 'state': 'failed'}])
        with patch('modport.web_data.Orchestrator.get_run', return_value=snapshot):
            row = self.store.list_runs()[0]
        self.assertEqual(row['state'], 'failed')
        self.assertEqual(row['active_steps'], [])

    def test_inherited_effective_and_invalidated_success(self):
        snapshot = self.sdk.get_run('example')
        source = {'task_id': 'source', 'stage_id': 'source', 'command_id': 'old-source',
                  'status': 'completed', 'detail': 'inherited source', 'outputs': {}}
        snapshot['application_state'] = {'effective': {'source': source}, 'processed': ['old-build']}
        snapshot['tasks'] = {'target_build': {'task_id': 'target_build', 'dependencies': ['source'],
            'attempts': [{'state': 'succeeded', 'command': {'execution_id': 'old-build',
                'payload': {'stage_id': 'target_build'}},
                'result': {'value': {'status': 'completed', 'detail': 'old build'}}}]}}
        with patch('modport.web_data.Orchestrator.get_run', return_value=snapshot):
            row = self.store.list_runs()[0]
        steps = [step for group in row['groups'] for step in group['steps']]
        source_step = next(step for step in steps if step['label'] == '获取源码')
        build = next(step for step in steps if step['label'] == '目标构建')
        self.assertEqual(source_step['state'], 'succeeded')
        self.assertTrue(source_step['inherited'])
        self.assertFalse(build['inherited'])
        self.assertEqual(source_step['result'], 'inherited source')
        self.assertTrue(source_step['scheduled'])
        self.assertEqual(source_step['dependencies'], [])
        self.assertEqual(build['state'], 'pending')
        self.assertTrue(build['scheduled'])
        self.assertEqual(build['dependencies'], [source_step['id']])
        self.assertIn('失效', build['progress'])
        self.assertEqual(build['attempts'][0]['state'], 'succeeded')

    def test_rejected_review_is_failed_even_when_execution_completed(self):
        task = {'task_id': 'code_review', 'attempts': [{'state': 'succeeded',
            'command': {}, 'result': {'value': {'status': 'completed',
                'outputs': {'verdict': 'rejected'}}}}]}
        step = _step('code_review', task)
        self.assertEqual(step['state'], 'failed')
        self.assertEqual(step['attempts'][0]['state'], 'failed')

    def test_replaced_generation_is_history_not_outstanding_rework(self):
        snapshot = self.sdk.get_run('example')
        snapshot['state'] = 'succeeded'
        outcome = {'task_id': 'acceptance_build.g2', 'stage_id': 'acceptance_build',
                   'command_id': 'new-build', 'status': 'completed', 'outputs': {}}
        snapshot['application_state'] = {'effective': {'acceptance_build': outcome},
                                         'processed': ['old-build', 'new-build']}
        snapshot['tasks'] = {
            name: {'task_id': name, 'dependencies': ['target_build.' + name.rsplit('.', 1)[-1]],
                'attempts': [{'state': 'succeeded',
                'command': {'execution_id': command, 'payload': {'stage_id': 'acceptance_build'}},
                'result': {'value': {**outcome, 'task_id': name, 'command_id': command}}}]}
            for name, command in [('acceptance_build.g1', 'old-build'), ('acceptance_build.g2', 'new-build')]}
        with patch('modport.web_data.Orchestrator.get_run', return_value=snapshot):
            row = self.store.list_runs()[0]
        self.assertEqual('succeeded', row['execution_state'])
        self.assertEqual('unverified', row['acceptance_status'])
        steps = [step for group in row['groups'] for step in group['steps']
                 if step['label'].startswith('验收构建')]
        self.assertEqual([step['state'] for step in steps], ['superseded', 'succeeded'])
        self.assertEqual(steps[0]['attempts'][0]['state'], 'succeeded')
        self.assertTrue(all(step['scheduled'] for step in steps))
        self.assertEqual([step['dependencies'] for step in steps], [
            [hashlib.sha256(('target_build.g%d' % generation).encode()).hexdigest()]
            for generation in (1, 2)])

    def test_referenced_summary_is_bounded_contained_and_verified(self):
        snapshot = self.sdk.get_run('example')
        message = self.run / 'summary.md'
        message.write_text('Summary token=hidden')
        ref = {'path': 'summary.md', 'sha256': hashlib.sha256(message.read_bytes()).hexdigest()}
        snapshot['tasks'] = {'migration_plan': {'task_id': 'migration_plan', 'dependencies': [],
            'attempts': [{'state': 'succeeded', 'command': {'payload': {'stage_id': 'migration_plan'}},
                'result': {'value': {'status': 'completed', 'outputs': {
                    'artifact_refs': {'planning_summary:migration_plan': ref}}}}}]}}
        with patch('modport.web_data.Orchestrator.get_run', return_value=snapshot):
            row = self.store.list_runs()[0]
        step = next(step for group in row['groups'] for step in group['steps'] if step['label'] == '迁移方案')
        self.assertIn('Summary', self.store.get_step(row['id'], step['id'])['result'])
        self.assertNotIn('hidden', self.store.get_step(row['id'], step['id'])['result'])
        with self.assertRaises(ValueError):
            self.store._summary(self.run, {**ref, 'sha256': '0' * 64})
        outside = self.root / 'private.txt'
        outside.write_text('private')
        for bad in ({'path': '../private.txt'}, {'path': str(outside)}):
            with self.assertRaises(ValueError):
                self.store._summary(self.run, bad)
        message.write_bytes(b'x' * 65537)
        with self.assertRaises(ValueError):
            self.store._summary(self.run, {'path': 'summary.md'})
        pipe = self.run / 'summary-pipe'
        os.mkfifo(pipe)
        with self.assertRaises(ValueError):
            self.store._summary(self.run, {'path': pipe.name})

    def test_bounded_redaction(self):
        result = _safe('token=secret https://user:pass@example.org Bearer abc ghp_123456789012345')
        for secret in ('secret', 'user:pass', 'abc', 'ghp_123456789012345'):
            self.assertNotIn(secret, result)
        self.assertLessEqual(len(_safe('x' * 100000)), 801)


class BoundedWebStatusTests(unittest.TestCase):
    """Current list projection and selected SDK evidence are separate reads."""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.run = MigrationOperations(handlers=registry(), isolation_mode='thread').submit(
            MigrationRequest('example', 'https://example.invalid/mod.git', '1.20.1', '26.1.2',
                             source_revision='a' * 40),
            run_dir=self.root / 'run', run_id='bounded-web-status')
        self.store = ProgressStore(self.root)

    def test_missing_monitor_keeps_run_visible_with_unknown_wait(self):
        row = self.store.list_runs()[0]
        self.assertEqual(row['run_id'], self.run.run_id)
        self.assertEqual(row['state'], 'unknown')
        self.assertEqual(row['current_wait']['status'], 'unknown')
        self.assertNotIn('无法读取运行摘要（AttributeError）', row['error'] or '')

    def test_selected_evidence_reads_bounded_sdk_summary(self):
        monitor = self.run.run_dir / 'artifacts' / 'monitor' / 'monitor-status.json'
        monitor.parent.mkdir(parents=True)
        monitor.write_text(json.dumps({
            'run_id': self.run.run_id, 'revision': 0, 'run_state': 'running',
            'observed_at': time.time(), 'complete': True,
            'snapshot_consistency': 'consistent',
            'counts': {'task_count': 0, 'open_waits': 0},
            'active_samples': [], 'execution_progress': [],
        }))
        row = self.store.list_runs()[0]
        self.assertEqual(row['current_wait']['status'], 'unknown')
        evidence = self.store.get_evidence(row['id'])
        self.assertEqual(evidence['run_id'], self.run.run_id)
        self.assertEqual(evidence['revision'], 0)
        self.assertIn('last_code_change', evidence)
        self.assertIn('last_successful_authenticated_verification', evidence)
        self.assertIn('current_wait', evidence)


if __name__ == '__main__':
    unittest.main()
