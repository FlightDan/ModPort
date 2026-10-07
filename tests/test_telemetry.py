import concurrent.futures
import csv
import errno
import hashlib
import json
import os
from pathlib import Path
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time
import tracemalloc
import unittest
from unittest.mock import patch

from modport.contracts import OperationInput
from modport import telemetry
from modport.audit_storage import store_data
from modport.telemetry import ProcessAudit, probe_process, export_report, operation_context, record_event, record_sdk_events, redact, run_process, public_last_message


class TelemetryTests(unittest.TestCase):
    def test_process_exit_records_scoped_evidence_without_claiming_shared_oom(self):
        from modport.process_diagnostics import CgroupMemoryEvents
        before = CgroupMemoryEvents('/test-scope', {'oom_kill': 0}, 1)
        after = CgroupMemoryEvents('/test-scope', {'oom_kill': 1}, 2)
        with patch('modport.process_diagnostics.read_process_memory_events', return_value=before), \
                patch('modport.process_diagnostics.read_memory_events', return_value=after), \
                operation_context(self.operation()):
            result = run_process([sys.executable, '-c',
                'import os,signal;os.kill(os.getpid(),signal.SIGKILL)'],
                cwd=self.root, log=self.root / 'killed.log')
        self.assertEqual(-signal.SIGKILL, result.returncode)
        report, _ = self.report()
        finished = next(event for event in report['events'] if event['kind'] == 'process.finished')
        evidence = finished['payload']
        self.assertEqual('/test-scope', evidence['memory_before']['scope'])
        self.assertEqual(1, evidence['memory_after']['counters']['oom_kill'])
        self.assertEqual('cgroup_oom_observed', evidence['process_diagnostic']['classification'])
        self.assertEqual('unknown', evidence['process_diagnostic']['attribution'])

    def test_large_prompt_uses_stdin_without_truncation_or_deadlock(self):
        import hashlib
        content = '完整失败上下文\n' * 30000 + 'test-stdin-secret-value'
        code = ('import hashlib,sys; '
                'sys.stdout.write("ready" * 40000 + "\\n"); sys.stdout.flush(); '
                'sys.stderr.write("error" * 40000 + "\\n"); sys.stderr.flush(); '
                'print(hashlib.sha256(sys.stdin.buffer.read()).hexdigest())')
        result = run_process([sys.executable, '-c', code], cwd=self.root,
            log=self.root / 'large-input.log', input_text=content,
            env={'TEST_API_KEY': 'test-stdin-secret-value'}, timeout=10)
        self.assertEqual(0, result.returncode)
        self.assertIn(hashlib.sha256(content.encode()).hexdigest(), result.stdout)
        saved, = (self.root / 'audit-logs').glob('*/stdin.log')
        self.assertNotIn('test-stdin-secret-value', saved.read_text())
        self.assertIn('完整失败上下文', saved.read_text())

    def test_large_input_timeout_and_spawn_failure_keep_redacted_audit(self):
        content = '完整历史' * 100000 + 'test-stdin-secret-value'
        for name, args, error in (
            ('timeout', [sys.executable, '-c', 'import time;time.sleep(10)'], subprocess.TimeoutExpired),
            ('spawn', ['/does-not-exist'], FileNotFoundError),
        ):
            with self.subTest(name=name), self.assertRaises(error):
                run_process(args, cwd=self.root, log=self.root / (name + '.log'),
                    input_text=content, env={'TEST_API_KEY': 'test-stdin-secret-value'}, timeout=.2)
            saved = (self.root / (name + '.log.stdin.txt')).read_text()
            self.assertNotIn('test-stdin-secret-value', saved)
            self.assertIn('完整历史', saved)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def operation(self, **options):
        return OperationInput('run', 'task', 'stage', 'command', str(self.root), options=options)

    def report(self):
        paths = export_report(self.root)
        return json.loads(paths['json'].read_text()), paths

    def test_stream_usage_attempts_and_redaction(self):
        code = 'import json,sys;print(json.dumps({"type":"turn.completed","usage":{"input_tokens":100,"cached_input_tokens":60,"output_tokens":8}}));print("Authorization: Bearer abcsecret",file=sys.stderr)'
        for _ in range(2):
            with operation_context(self.operation(model='requested', reasoning_effort='high')):
                result = run_process([sys.executable, '-c', code], cwd=self.root, log=self.root / 'stage.log')
                self.assertEqual(result.returncode, 0)
                self.assertIn('abcsecret', result.stdout)
        report, _ = self.report()
        self.assertEqual(len(list((self.root / 'audit-logs').iterdir())), 2)
        group = report['usage_summary'][0]
        self.assertEqual(group['input_tokens'], 200)
        self.assertEqual(group['cached_input_tokens'], 120)
        self.assertEqual(group['output_tokens'], 16)
        self.assertIsNone(group['reasoning_output_tokens'])
        self.assertIsNone(group['reported_model'])
        self.assertEqual(group['requested_model'], 'requested')
        for path in self.root.rglob('*.log'):
            self.assertNotIn('abcsecret', path.read_text())

    def test_timeout_retains_already_reported_usage(self):
        code = 'import json,time;print(json.dumps({"type":"turn.completed","usage":{"input_tokens":10,"output_tokens":2}}),flush=True);time.sleep(10)'
        with operation_context(self.operation()):
            with self.assertRaises(subprocess.TimeoutExpired):
                run_process([sys.executable, '-c', code], cwd=self.root, log=self.root / 'a.log', timeout=.3)
        report, _ = self.report()
        self.assertEqual(report['usage_summary'][0]['input_tokens'], 10)
        self.assertIsNone(report['usage_summary'][0]['cached_input_tokens'])
        self.assertTrue(any(e.get('status') == 'timeout' for e in report['events']))

    def test_timeout_still_applies_after_both_output_streams_close(self):
        import time
        code = 'import os,time;os.close(1);os.close(2);time.sleep(5)'
        started = time.monotonic()
        with operation_context(self.operation()), self.assertRaises(subprocess.TimeoutExpired):
            run_process([sys.executable, '-c', code], cwd=self.root, log=self.root / 'closed.log', timeout=.2)
        self.assertLess(time.monotonic() - started, 3)
        report, _ = self.report()
        self.assertTrue(any(event.get('status') == 'timeout' for event in report['events']))

    @unittest.skipUnless(hasattr(os, 'fork') and hasattr(os, 'setsid'), 'requires POSIX detached processes')
    def test_timeout_bounds_detached_pipe_drain_and_preserves_pending_logs(self):
        code = '''import os, sys, time
if os.fork() == 0:
    os.setsid()
    with open(sys.argv[1], 'w') as marker:
        marker.write(str(os.getpid()))
    secret = os.environ['TEST_API_KEY'].encode()
    os.write(1, b'pending stdout ' + secret)
    os.write(2, b'pending stderr ' + secret)
    time.sleep(8)
    os._exit(0)
while not os.path.exists(sys.argv[1]):
    time.sleep(.01)
if sys.argv[2] == 'exit':
    os._exit(0)
time.sleep(8)
'''
        secret = 'test-detached-secret'
        for leader in ('running', 'exit'):
            with self.subTest(leader=leader):
                marker = self.root / (leader + '.pid')
                log = self.root / (leader + '.log')
                started = time.monotonic()
                try:
                    with self.assertRaises(subprocess.TimeoutExpired) as caught:
                        run_process([sys.executable, '-c', code, str(marker), leader],
                            cwd=self.root, log=log, timeout=.3, combine_output=False,
                            input_text='original prompt ' + secret, env={'TEST_API_KEY': secret})
                    self.assertLess(time.monotonic() - started, 3)
                    self.assertEqual(caught.exception.output, 'pending stdout ' + secret)
                    self.assertEqual(caught.exception.stderr, 'pending stderr ' + secret)
                    saved = log.read_text()
                    self.assertIn('pending stdout [REDACTED]', saved)
                    self.assertIn('pending stderr [REDACTED]', saved)
                    self.assertIn('descendant pipes remained open', saved)
                    self.assertNotIn(secret, saved)
                    self.assertEqual(Path(str(log) + '.stdin.txt').read_text(),
                                     'original prompt [REDACTED]')
                finally:
                    # The SDK normally contains this descendant. This direct
                    # subprocess test must clean up its own detached fixture.
                    if marker.exists():
                        try:
                            os.kill(int(marker.read_text()), signal.SIGKILL)
                        except ProcessLookupError:
                            pass
        report, _ = self.report()
        finals = [event for event in report['events'] if event['kind'] == 'process.finished']
        self.assertEqual(len(finals), 2)
        self.assertTrue(all(event['status'] == 'timeout' and
                            event['payload']['output_drain_incomplete'] for event in finals))

    @unittest.skipUnless(sys.platform.startswith('linux'), 'uses Linux environment size rejection')
    def test_e2big_reports_os_rejection_without_dumping_environment_or_prompt(self):
        log = self.root / 'oversized.log'
        # This is an actual OS rejection, not an application-side size cap.
        environment_value = 'private-environment-' + 'x' * (2 * 1024 * 1024)
        prompt = 'original prompt must stay in the stdin audit'
        with self.assertRaises(OSError) as caught:
            run_process([sys.executable, '-c', 'pass'], cwd=self.root, log=log,
                env={'MODPORT_TEST_LARGE_ENV': environment_value}, input_text=prompt, timeout=2)
        self.assertEqual(caught.exception.errno, errno.E2BIG)
        self.assertIn('arguments or environment', str(caught.exception))
        self.assertIsNone(caught.exception.filename)
        report, _ = self.report()
        final, = [event for event in report['events'] if event['kind'] == 'process.finished']
        self.assertEqual(final['status'], 'failed')
        self.assertIsNone(final['payload']['returncode'])
        self.assertIn('E2BIG', final['payload']['launch_error'])
        for diagnostic in (str(caught.exception), json.dumps(report), log.read_text()):
            self.assertNotIn('private-environment-', diagnostic)
            self.assertNotIn(prompt, diagnostic)
        self.assertEqual(Path(str(log) + '.stdin.txt').read_text(), prompt)

    def test_last_message_uses_public_json_event_and_existing_redaction(self):
        from unittest.mock import patch
        events = [
            {"type": "item.completed", "item": {"type": "reasoning", "text": "not a public message"}},
            {"type": "item.completed", "item": {"type": "agent_message", "text": "API_KEY=example-value; actual-sample-value"}},
        ]
        with patch.dict('os.environ', {'TEST_API_KEY': 'actual-sample-value'}):
            message = public_last_message('\n'.join(json.dumps(event) for event in events))
        self.assertNotIn('actual-sample-value', message)
        self.assertNotIn('example-value', message)
        self.assertNotIn('not a public message', message)
        self.assertIn('No final', public_last_message('not json'))

    def test_parallel_and_replay(self):
        def write(i):
            return record_event(self.root, str(i % 25), 'test', {'n': i % 25})
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            self.assertEqual(sum(pool.map(write, range(100))), 25)
        events = [{'event_id': '1', 'type': 'start'}, {'event_id': '2', 'type': 'stop'}]
        self.assertEqual(record_sdk_events(self.root, events), 2)
        self.assertEqual(record_sdk_events(self.root, events), 0)
        self.assertEqual(len(self.report()[0]['events']), 27)

    def test_unfinished_start_unknown_and_html_safe(self):
        attack = '</script><script>alert(1)</script>'
        record_event(self.root, 'start', 'process.started', {'output': attack}, invocation_id='missing', agent_id=attack, requested_model='x')
        report, paths = self.report()
        self.assertEqual(report['events'][0]['status'], 'interrupted_or_unknown')
        self.assertEqual(report['usage_summary'], [])
        self.assertNotIn(attack, paths['html'].read_text())
        self.assertIn('\\u003c/script', paths['html'].read_text())

    def test_complete_streamed_outputs_have_bounded_hashed_html_summary(self):
        attack = '</script><script>alert(1)</script>'
        for index in range(130):
            record_event(self.root, f'large-{index:03}', 'sdk.event',
                         {'attack': attack, 'body': str(index) + ':' + 'x' * 2048},
                         agent_id='=spreadsheet_formula')

        paths = export_report(self.root)
        report = json.loads(paths['json'].read_text())
        with paths['csv'].open(newline='', encoding='utf-8') as file:
            rows = list(csv.DictReader(file))
        html = paths['html'].read_text()

        self.assertEqual(len(report['events']), 130)
        self.assertEqual(len(rows), 130)
        self.assertTrue(all(row['agent_id'].startswith("'=") for row in rows))
        self.assertLess(len(html.encode()), 200_000)
        self.assertIn('"event_count": 130', html)
        self.assertIn('"omitted_events": 30', html)
        self.assertIn('href="audit.json"', html)
        self.assertIn('href="audit.csv"', html)
        self.assertIn(hashlib.sha256(paths['json'].read_bytes()).hexdigest(), html)
        self.assertIn(hashlib.sha256(paths['csv'].read_bytes()).hexdigest(), html)
        self.assertNotIn(attack, html)
        self.assertIn('\\u003c/script', html)

    def test_export_peak_memory_does_not_scale_with_all_event_payloads(self):
        for index in range(24):
            record_event(self.root, f'memory-{index:03}', 'sdk.event',
                         {'body': str(index) + ':' + 'x' * 200_000})

        tracemalloc.start()
        try:
            paths = export_report(self.root)
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()

        self.assertLess(peak, 6_000_000)
        self.assertEqual(len(json.loads(paths['json'].read_text())['events']), 24)

    def test_html_bounds_oversized_top_level_metadata(self):
        oversized = 'metadata-' * 100_000
        record_event(self.root, 'metadata', 'usage',
                     {'input_tokens': 1, 'cached_input_tokens': None,
                      'output_tokens': 1, 'reasoning_output_tokens': None,
                      'granularity': 'turn_aggregate'},
                     agent_id=oversized, unexpected_metadata=oversized)

        paths = export_report(self.root)
        event, = json.loads(paths['json'].read_text())['events']
        html = paths['html'].read_text()

        self.assertEqual(event['agent_id'], oversized)
        self.assertEqual(event['unexpected_metadata'], oversized)
        self.assertLess(len(html.encode()), 30_000)
        self.assertNotIn(oversized, html)
        self.assertIn('metadata-metadata-', html)
        self.assertNotIn('unexpected_metadata', html)

    def test_export_resolves_and_verifies_external_event_data(self):
        data = {'agent_id': 'external', 'payload': {'body': '完整证据' * 30_000}}
        serialized = store_data(self.root, data)
        with telemetry._connect(self.root) as connection:
            connection.execute('INSERT INTO events VALUES (?,?,?,?)',
                               ('external', '2026-01-01T00:00:00+00:00',
                                'sdk.event', serialized))

        paths = export_report(self.root)
        event, = json.loads(paths['json'].read_text())['events']
        with paths['csv'].open(newline='', encoding='utf-8') as file:
            row, = list(csv.DictReader(file))

        self.assertEqual(event['payload'], data['payload'])
        self.assertEqual(json.loads(row['payload']), data['payload'])
        self.assertIn(hashlib.sha256(paths['json'].read_bytes()).hexdigest(),
                      paths['html'].read_text())

    def test_export_uses_one_snapshot_when_writer_commits_during_iteration(self):
        record_event(self.root, 'first', 'test', {'n': 1})
        connection = sqlite3.connect(self.root / 'audit.sqlite3')
        try:
            connection.execute('PRAGMA journal_mode=WAL').fetchone()
        finally:
            connection.close()
        original = telemetry._iter_snapshot_events

        def insert_while_exporting(connection, root, usage_invocations, finished):
            for index, event in enumerate(original(connection, root, usage_invocations, finished)):
                if index == 0:
                    self.assertTrue(record_event(self.root, 'late', 'test', {'n': 2}))
                yield event

        with patch.object(telemetry, '_iter_snapshot_events', new=insert_while_exporting):
            paths = export_report(self.root)
        report = json.loads(paths['json'].read_text())
        with paths['csv'].open(newline='', encoding='utf-8') as file:
            rows = list(csv.DictReader(file))

        self.assertEqual([event['event_id'] for event in report['events']], ['first'])
        self.assertEqual([row['event_id'] for row in rows], ['first'])
        self.assertIn('"event_count": 1', paths['html'].read_text())
        self.assertIn('"omitted_events": 0', paths['html'].read_text())
        second = json.loads(export_report(self.root)['json'].read_text())
        self.assertEqual(len(second['events']), 2)

    def test_concurrent_exports_leave_complete_current_and_previous_generations(self):
        record_event(self.root, 'one', 'test', {'n': 1})
        destination = self.root / 'concurrent-report'
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            reports = list(pool.map(lambda _: export_report(self.root, destination), range(8)))
        self.assertEqual(8, len(reports))
        current = destination / os.readlink(destination / '.audit-current')
        generations = list((destination / '.audit-generations').glob('generation-*'))
        self.assertEqual(2, len(generations))
        self.assertIn(current, generations)
        for generation in generations:
            document = json.loads((generation / 'audit.json').read_text())
            self.assertEqual(['one'], [item['event_id'] for item in document['events']])
            manifest = json.loads((generation / 'manifest.json').read_text())
            self.assertTrue(manifest['artifacts'])
        self.assertTrue(all(path.is_file() for path in reports[-1].values()))

    def test_generation_failure_does_not_publish_partial_report(self):
        destination = self.root / 'existing-report'
        destination.mkdir()
        originals = {}
        for extension in ('json', 'csv', 'md', 'html'):
            path = destination / ('audit.' + extension)
            originals[extension] = ('existing-' + extension).encode()
            path.write_bytes(originals[extension])
        record_event(self.root, 'event', 'test', {'n': 1})

        with patch.object(telemetry, '_html', side_effect=RuntimeError('render failed')):
            with self.assertRaises(RuntimeError):
                export_report(self.root, destination)

        for extension, content in originals.items():
            self.assertEqual((destination / ('audit.' + extension)).read_bytes(), content)
        self.assertEqual(list(destination.glob('.audit-export-*')), [])

    def test_interrupted_generation_commit_keeps_previous_report_complete(self):
        destination = self.root / 'report'
        record_event(self.root, 'first', 'test', {'n': 1})
        first_paths = export_report(self.root, destination)
        previous_target = os.readlink(destination / '.audit-current')
        previous_bytes = {
            extension: first_paths[extension].read_bytes()
            for extension in ('json', 'csv', 'md', 'html')
        }

        record_event(self.root, 'second', 'test', {'n': 2})
        replace_symlink = telemetry._replace_symlink

        def interrupt_current_pointer(path, target):
            if path.name == '.audit-current':
                raise OSError('simulated interruption before commit')
            return replace_symlink(path, target)

        with patch.object(telemetry, '_replace_symlink', new=interrupt_current_pointer):
            with self.assertRaisesRegex(OSError, 'simulated interruption'):
                export_report(self.root, destination)

        self.assertEqual(os.readlink(destination / '.audit-current'), previous_target)
        for extension, content in previous_bytes.items():
            alias = destination / ('audit.' + extension)
            self.assertTrue(alias.is_symlink())
            self.assertEqual(os.readlink(alias), '.audit-current/' + alias.name)
            self.assertEqual(alias.read_bytes(), content)
        generations = sorted((destination / '.audit-generations').glob('generation-*'))
        self.assertEqual(len(generations), 2)
        self.assertTrue(all((path / 'manifest.json').is_file() for path in generations))

        completed_paths = export_report(self.root, destination)
        completed = json.loads(completed_paths['json'].read_text())
        self.assertEqual([event['event_id'] for event in completed['events']],
                         ['first', 'second'])
        generations = sorted((destination / '.audit-generations').glob('generation-*'))
        self.assertEqual(len(generations), 2)
        self.assertTrue((destination / previous_target / 'manifest.json').is_file())

    def test_generation_directory_fsync_failure_is_not_ignored(self):
        destination = self.root / 'report'
        record_event(self.root, 'first', 'test', {'n': 1})
        paths = export_report(self.root, destination)
        previous_target = os.readlink(destination / '.audit-current')
        previous_json = paths['json'].read_bytes()
        record_event(self.root, 'second', 'test', {'n': 2})
        fsync_directory = telemetry._fsync_directory

        def fail_generation_store(path):
            if Path(path).name == '.audit-generations':
                raise OSError('simulated directory fsync failure')
            return fsync_directory(path)

        with patch.object(telemetry, '_fsync_directory', new=fail_generation_store):
            with self.assertRaisesRegex(OSError, 'directory fsync failure'):
                export_report(self.root, destination)

        self.assertEqual(os.readlink(destination / '.audit-current'), previous_target)
        self.assertEqual(paths['json'].read_bytes(), previous_json)

    def test_responses_details_granularity_model(self):
        code = 'import json; print(json.dumps({"type":"response.completed","response":{"id":"response1","model":"actual","usage":{"input_tokens":50,"input_tokens_details":{"cached_tokens":40},"output_tokens":5,"output_tokens_details":{"reasoning_tokens":2}}}}))'
        with operation_context(self.operation(model='requested')):
            run_process([sys.executable, '-c', code], cwd=self.root, log=self.root / 'a.log')
        group = self.report()[0]['usage_summary'][0]
        self.assertEqual(group['granularity'], 'api_response')
        self.assertEqual(group['reported_model'], 'actual')
        self.assertEqual(group['reasoning_output_tokens'], 2)

    def test_response_replay_and_failed_process(self):
        event = {'type': 'response.completed', 'response': {'id': 'stable', 'model': 'model', 'usage': {'input_tokens': 3, 'output_tokens': 1}}}
        script = self.root / 'fake.py'
        script.write_text('import json,sys\nevent=' + repr(event) + '\nprint(json.dumps(event));print(json.dumps(event));sys.exit(7)')
        with operation_context(self.operation()):
            result = run_process([sys.executable, str(script)], cwd=self.root, log=self.root / 'failed.log')
        self.assertEqual(result.returncode, 7)
        report, _ = self.report()
        self.assertEqual(report['usage_summary'][0]['observations'], 1)
        self.assertEqual(report['usage_summary'][0]['input_tokens'], 3)
        self.assertTrue(any(e.get('status') == 'failed' for e in report['events']))

    def test_launch_failure_recorded_and_unknown_usage(self):
        with operation_context(self.operation()):
            with self.assertRaises(FileNotFoundError):
                run_process(['/does-not-exist'], cwd=self.root, log=self.root / 'failed.log')
        report, _ = self.report()
        started = next(e for e in report['events'] if e['kind'] == 'process.started')
        self.assertEqual(started['usage_availability'], 'unavailable')
        self.assertEqual(report['usage_summary'], [])

    def test_probe_keeps_stderr_out_of_stdout(self):
        with operation_context(self.operation()):
            result = probe_process([sys.executable, '-c', 'import sys;print("out");print("err",file=sys.stderr)'], cwd=self.root, stderr=subprocess.DEVNULL)
        self.assertEqual(result.stdout, 'out\n')
        self.assertIsNone(result.stderr)
        self.assertTrue(any('err' in p.read_text() for p in (self.root / 'audit-logs').rglob('stderr.log')))

    def test_manual_process_audit_stream_and_timeout(self):
        with operation_context(self.operation()):
            audit = ProcessAudit(self.root, ['fake-client'], cwd=self.root, log=self.root / 'client.log', env={'GITHUB_TOKEN': 'secret-value'})
            audit.write(b'healthy marker\nAuthorization: Bearer secret-')
            self.assertIn('healthy marker', (audit.folder / 'combined.log').read_text())
            audit.write(b'value\nlast partial')
            audit.finish('timeout', -9)
            audit.finish('completed', 0)
        report, _ = self.report()
        self.assertNotIn('secret-value', (self.root / 'client.log').read_text())
        self.assertIn('last partial', (self.root / 'client.log').read_text())
        finals = [e for e in report['events'] if e['kind'] == 'process.finished']
        self.assertEqual(len(finals), 1)
        self.assertEqual(finals[0]['status'], 'timeout')

    def test_known_secret_and_private_reasoning(self):
        code = 'import json;print("env-secret-value");print(json.dumps({"type":"item.completed","item":{"type":"reasoning","text":"hidden thought"}}))'
        script = self.root / 'fake.py'
        script.write_text(code)
        with operation_context(self.operation()):
            run_process([sys.executable, str(script)], cwd=self.root, log=self.root / 'a.log', env={'TEST_API_KEY': 'env-secret-value'})
        report, _ = self.report()
        self.assertNotIn('hidden thought', json.dumps(report))
        for path in self.root.rglob('*.log'):
            self.assertNotIn('hidden thought', path.read_text())
            self.assertNotIn('env-secret-value', path.read_text())
        self.assertEqual(redact(['--api-key', 'private', 'https://me:pw@example.test']), ['--api-key', '[REDACTED]', 'https://[REDACTED]@example.test'])


if __name__ == '__main__':
    unittest.main()
