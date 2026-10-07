"""Behavioral checks for the command boundary used by OpenCode."""
from pathlib import Path
from hashlib import sha256
from io import BytesIO
import io
import json
import subprocess
import tempfile
import time
import unittest
import zipfile
from unittest.mock import patch

from modport.environment_wrapper_cache import _gradle_url_hash
from modport.opencode_shell_mcp import (
    _bounded_command, _direct_gradle_wrapper, _dispatch, _read_run_artifact,
    _run, _run_characterization, _seed_wrapper_for_project_command, _validate_scope,
    prepare_sandbox_tool,
)


class BoundedCommandTests(unittest.TestCase):
    def test_direct_wrapper_command_uses_verified_host_cache_and_private_home(self):
        url = 'https://services.gradle.org/distributions/gradle-8.1.1-bin.zip'
        archive_output = BytesIO()
        with zipfile.ZipFile(archive_output, 'w') as archive:
            archive.writestr('gradle-8.1.1/bin/gradle', b'fixture launcher')
        archive = archive_output.getvalue()
        digest = sha256(archive).hexdigest()

        class Response:
            status = 200

            def __init__(self):
                self.offset = 0

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def geturl(self):
                return url

            def read(self, limit=-1):
                if limit < 0:
                    limit = len(archive) - self.offset
                result = archive[self.offset:self.offset + limit]
                self.offset += len(result)
                return result

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / 'run'
            workspace = root / 'worktree'
            properties = workspace / 'gradle/wrapper/gradle-wrapper.properties'
            properties.parent.mkdir(parents=True)
            properties.write_text('distributionUrl=https\\://services.gradle.org/distributions/gradle-8.1.1-bin.zip\n'
                                  f'distributionSha256Sum={digest}\n', encoding='utf-8')
            (root / 'run.json').write_text(json.dumps({
                'format_version': 2, 'run_id': 'run', 'run_dir': str(root),
                'definition': {'workflow_version': 26},
                'request': {'dependency_cache': str(Path(temporary) / 'dependency-cache')},
            }))
            session = {'root': str(root), 'workspace': str(workspace),
                       'command_id': 'run:coder:1', 'deadline_epoch': time.time() + 20}
            with patch('modport.environment_wrapper_cache._open_https_response',
                       return_value=Response()) as opened:
                result = _seed_wrapper_for_project_command(session, './gradlew help', 10)
            self.assertEqual('stored', result['state'])
            opened.assert_called_once()
            zip_path = (root / 'toolchains/gradle-cache/wrapper/dists/gradle-8.1.1-bin'
                        / _gradle_url_hash(url) / 'gradle-8.1.1-bin.zip')
            self.assertEqual(digest, sha256(zip_path.read_bytes()).hexdigest())
            with patch('modport.environment_wrapper_cache._open_https_response',
                       side_effect=AssertionError('hot cache must not download')):
                hot = _seed_wrapper_for_project_command(session, 'bash ./gradlew help', 10)
            self.assertEqual('hit', hot['state'])
            self.assertTrue(zip_path.is_file())


            from modport.handlers import _sandboxed_build_command
            with patch('modport.handlers.shutil.which', return_value='/usr/bin/bwrap'), \
                    patch('modport.dependency_build.dependency_mounts',
                          side_effect=lambda _root, args: ([], args)):
                sandbox = _sandboxed_build_command(
                    root, workspace, ['/bin/sh', '-lc', './gradlew help'])
            self.assertIn('--clearenv', sandbox)
            cache_mount = sandbox.index(str(root / 'toolchains/gradle-cache'))
            self.assertEqual(['--bind', str(root / 'toolchains/gradle-cache'), '/gradle-cache'],
                             sandbox[cache_mount - 1:cache_mount + 2])
            self.assertNotIn(str(Path(temporary) / 'environment-cache'), sandbox)
            with patch('modport.environment_wrapper_cache._open_https_response',
                       side_effect=AssertionError('MCP hot command must not download')), \
                    patch('modport.handlers._sandboxed_build_command', return_value=['/bin/true']), \
                    patch('modport.opencode_shell_mcp._bounded_command', return_value={
                        'exit_code': 0, 'stdout': '', 'stderr': '', 'timed_out': False,
                    }):
                executed = _run(session, './gradlew help', 10)
            self.assertEqual('hit', executed['wrapper_distribution_cache']['state'])
            self.assertTrue((root / executed['artifact_path']).is_file())

            for text in ('echo gradlew', 'grep gradlew file', 'gradlew help',
                         './gradlew help; echo done', 'sh -lc "./gradlew help"'):
                with self.subTest(text=text):
                    self.assertFalse(_direct_gradle_wrapper(text))
                    self.assertIsNone(_seed_wrapper_for_project_command(session, text, 10))
            self.assertTrue(_direct_gradle_wrapper('./gradlew help'))
            self.assertTrue(_direct_gradle_wrapper('sh gradlew help'))

            header = json.loads((root / 'run.json').read_text())
            header['definition']['workflow_version'] = 25
            (root / 'run.json').write_text(json.dumps(header))
            self.assertIsNone(_seed_wrapper_for_project_command(session, './gradlew help', 10))
            with patch('modport.handlers._sandboxed_build_command', return_value=['/bin/true']), \
                    patch('modport.opencode_shell_mcp._bounded_command', return_value={
                        'exit_code': 0, 'stdout': '', 'stderr': '', 'timed_out': False,
                    }):
                legacy = _run(session, './gradlew help', 10)
            self.assertNotIn('wrapper_distribution_cache', legacy)
            header['definition']['workflow_version'] = 26
            header['run_id'] = 'another-run'
            (root / 'run.json').write_text(json.dumps(header))
            with self.assertRaisesRegex(ValueError, 'identity'):
                _seed_wrapper_for_project_command(session, './gradlew help', 10)

    def test_wrapper_seed_and_build_setup_share_tool_deadline(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / 'run'
            workspace = root / 'worktree'
            workspace.mkdir(parents=True)
            session = {'root': str(root), 'workspace': str(workspace),
                       'command_id': 'run:coder:1', 'deadline_epoch': time.time() + 10}

            def slow_seed(*_args):
                time.sleep(0.06)
                return {'state': 'hit'}

            def slow_setup(*_args, **_kwargs):
                time.sleep(0.06)
                return ['/bin/true']

            with patch('modport.opencode_shell_mcp._seed_wrapper_for_project_command',
                       side_effect=slow_seed), \
                    patch('modport.handlers._sandboxed_build_command', side_effect=slow_setup), \
                    patch('modport.opencode_shell_mcp._bounded_command', return_value={
                        'exit_code': 0, 'stdout': '', 'stderr': '', 'timed_out': False,
                    }) as executed:
                result = _run(session, './gradlew help', 3)
            self.assertEqual('hit', result['wrapper_distribution_cache']['state'])
            self.assertLess(executed.call_args.args[2], 2.95)
            self.assertGreater(executed.call_args.args[2], 0)

    def test_host_network_deny_keeps_artifact_reader_but_rejects_project_command(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / 'run'
            workspace = root / 'worktree'
            workspace.mkdir(parents=True)
            artifact = root / 'artifacts' / 'summary.txt'
            artifact.parent.mkdir()
            artifact.write_text('synthetic evidence\n')
            with patch.dict('os.environ', {'MODPORT_DENY_NETWORK_TOOLS': '1'}):
                config = prepare_sandbox_tool(root, workspace, 'deny-task', 10)
            descriptor = root / 'artifacts' / 'executions' / 'deny-task' / 'opencode-shell' / 'session.json'
            self.assertIs(json.loads(descriptor.read_text())['allow_project_command'], False)
            requests = [
                {'jsonrpc': '2.0', 'id': 1, 'method': 'tools/list'},
                {'jsonrpc': '2.0', 'id': 2, 'method': 'tools/call',
                 'params': {'name': 'run_project_command',
                            'arguments': {'command': 'touch should-not-exist'}}},
                {'jsonrpc': '2.0', 'id': 3, 'method': 'tools/call',
                 'params': {'name': 'read_run_artifact',
                            'arguments': {'path': 'artifacts/summary.txt'}}},
            ]
            result = subprocess.run(config['modport_sandbox']['command'],
                                    input=''.join(json.dumps(row) + '\n' for row in requests),
                                    text=True, capture_output=True, timeout=5, check=False)
            self.assertEqual(0, result.returncode, result.stderr)
            replies = [json.loads(line) for line in result.stdout.splitlines()]
            self.assertEqual(['read_run_artifact'], [tool['name']
                             for tool in replies[0]['result']['tools']])
            self.assertEqual(-32602, replies[1]['error']['code'])
            self.assertFalse((workspace / 'should-not-exist').exists())
            self.assertEqual('synthetic evidence\n',
                             replies[2]['result']['structuredContent']['content_utf8'])

    def test_editable_diagnostic_session_rejects_project_commands_at_mcp_boundary(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            workspace = root / 'workspaces' / 'diagnosis'
            workspace.mkdir(parents=True)
            artifact = root / 'artifacts' / 'error.txt'
            artifact.parent.mkdir()
            artifact.write_text('confirmed typo')
            config = prepare_sandbox_tool(root, workspace, 'diagnostic', 10,
                                          read_only=False, allow_project_commands=False)
            requests = [
                {'jsonrpc': '2.0', 'id': 1, 'method': 'tools/list'},
                {'jsonrpc': '2.0', 'id': 2, 'method': 'tools/call', 'params': {
                    'name': 'run_project_command', 'arguments': {'command': 'touch forbidden'}}},
                {'jsonrpc': '2.0', 'id': 3, 'method': 'tools/call', 'params': {
                    'name': 'read_run_artifact', 'arguments': {'path': 'artifacts/error.txt'}}},
            ]
            completed = subprocess.run(config['modport_sandbox']['command'],
                input=''.join(json.dumps(row) + '\n' for row in requests),
                text=True, capture_output=True, timeout=5, check=True)
            replies = [json.loads(line) for line in completed.stdout.splitlines()]
            self.assertEqual(['read_run_artifact'], [row['name'] for row in replies[0]['result']['tools']])
            self.assertEqual(-32602, replies[1]['error']['code'])
            self.assertFalse((workspace / 'forbidden').exists())
            self.assertEqual('confirmed typo', replies[2]['result']['structuredContent']['content_utf8'])

    def test_legacy_sandbox_descriptor_keeps_default_allow_on_recovery(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / 'run'
            workspace = root / 'worktree'
            workspace.mkdir(parents=True)
            with patch.dict('os.environ', {'MODPORT_DENY_NETWORK_TOOLS': '0'}):
                prepare_sandbox_tool(root, workspace, 'legacy-task', 10)
            descriptor = root / 'artifacts' / 'executions' / 'legacy-task' / 'opencode-shell' / 'session.json'
            legacy = json.loads(descriptor.read_text())
            legacy.pop('allow_project_command')
            descriptor.write_text(json.dumps(legacy))
            with patch.dict('os.environ', {'MODPORT_DENY_NETWORK_TOOLS': '0'}):
                prepare_sandbox_tool(root, workspace, 'legacy-task', 10)
            self.assertIs(json.loads(descriptor.read_text())['allow_project_command'], True)
            descriptor.write_text(json.dumps(legacy))
            with patch.dict('os.environ', {'MODPORT_DENY_NETWORK_TOOLS': '1'}):
                with self.assertRaisesRegex(ValueError, 'identity changed'):
                    prepare_sandbox_tool(root, workspace, 'legacy-task', 10)

    def test_registered_mcp_process_verifies_host_artifact_digest(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / 'run'
            workspace = root / 'worktree'
            workspace.mkdir(parents=True)
            artifact = root / 'artifacts' / 'summary.txt'
            artifact.parent.mkdir()
            artifact.write_text('认证摘要中的中文\n', encoding='utf-8')
            expected = sha256(artifact.read_bytes()).hexdigest()
            config = prepare_sandbox_tool(root, workspace, 'plan-task', 10)
            command = config['modport_sandbox']['command']
            requests = [
                {'jsonrpc': '2.0', 'id': 1, 'method': 'initialize',
                 'params': {'protocolVersion': '2025-06-18'}},
                {'jsonrpc': '2.0', 'id': 2, 'method': 'tools/list'},
                {'jsonrpc': '2.0', 'id': 3, 'method': 'tools/call',
                 'params': {'name': 'read_run_artifact', 'arguments': {
                     'path': 'artifacts/summary.txt', 'expected_sha256': expected}}},
                {'jsonrpc': '2.0', 'id': 4, 'method': 'tools/call',
                 'params': {'name': 'read_run_artifact', 'arguments': {
                     'path': 'artifacts/summary.txt', 'offset': 1,
                     'expected_sha256': expected}}},
            ]
            result = subprocess.run(command, input=''.join(json.dumps(row) + '\n'
                                    for row in requests), text=True, capture_output=True,
                                    timeout=5, check=False)
            self.assertEqual(0, result.returncode, result.stderr)
            replies = [json.loads(line) for line in result.stdout.splitlines()]
            self.assertEqual([1, 2, 3, 4], [reply['id'] for reply in replies])
            reader = next(tool for tool in replies[1]['result']['tools']
                          if tool['name'] == 'read_run_artifact')
            self.assertIn('expected_sha256', reader['inputSchema']['properties'])
            self.assertIn('expected_sha256', reader['description'])
            content = replies[2]['result']['structuredContent']
            self.assertEqual(expected, content['verified_sha256'])
            self.assertEqual('认证摘要中的中文\n', content['content_utf8'])
            self.assertTrue(replies[3]['result']['isError'])
            self.assertIn('UTF-8', replies[3]['result']['structuredContent']['detail'])

    def test_host_artifact_reader_is_bounded_and_rejects_sibling_or_symlink(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / 'run'
            artifacts = root / 'artifacts'
            artifacts.mkdir(parents=True)
            sibling = Path(temporary) / 'sibling' / 'artifacts'
            sibling.mkdir(parents=True)
            target = artifacts / 'fact.txt'
            target.write_text('nonce=48392\n')
            (artifacts / 'linked.txt').symlink_to(target)
            (artifacts / 'hardlinked.txt').hardlink_to(target)
            session = {'root': str(root), 'deadline_epoch': time.time() + 10}
            with self.assertRaisesRegex(ValueError, 'unlinked'):
                _read_run_artifact(session, str(target), 6, 5)
            (artifacts / 'hardlinked.txt').unlink()
            result = _read_run_artifact(session, str(target), 6, 5)
            self.assertEqual('48392', result['content_utf8'])
            self.assertEqual(11, result['next_offset'])
            self.assertEqual('artifacts/fact.txt', result['path'])
            expected = sha256(target.read_bytes()).hexdigest()
            verified = _read_run_artifact(session, str(target), expected_sha256=expected)
            self.assertEqual(expected, verified['verified_sha256'])
            with self.assertRaisesRegex(ValueError, 'does not match'):
                _read_run_artifact(session, str(target), expected_sha256='0' * 64)
            output = io.StringIO()
            with patch('sys.stdout', output):
                _dispatch(session, {'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
                                    'params': {'name': 'read_run_artifact',
                                               'arguments': {'path': str(target),
                                                             'expected_sha256': expected}}})
            response = json.loads(output.getvalue())['result']
            self.assertFalse(response['isError'])
            self.assertEqual(expected, response['structuredContent']['verified_sha256'])
            tool_list = io.StringIO()
            with patch('sys.stdout', tool_list):
                _dispatch(session, {'jsonrpc': '2.0', 'id': 2, 'method': 'tools/list'})
            reader = next(tool for tool in json.loads(tool_list.getvalue())['result']['tools']
                          if tool['name'] == 'read_run_artifact')
            self.assertIn('expected_sha256', reader['inputSchema']['properties'])
            for forbidden in (str(sibling / 'fact.txt'), str(artifacts / 'linked.txt'),
                              str(artifacts / '..' / 'secret.txt')):
                with self.subTest(forbidden=forbidden), self.assertRaises((ValueError, OSError)):
                    _read_run_artifact(session, forbidden)

    def test_tool_rejects_run_artifacts_and_path_like_command_ids(self):
        root = Path('/tmp/modport-run')
        with self.assertRaises(ValueError):
            _validate_scope(root, root / 'artifacts', 'command')
        with self.assertRaises(ValueError):
            _validate_scope(root, root / 'worktree', '../command')
        _validate_scope(root, root / 'workspaces' / 'skills' / 'java', 'command:1')

    def test_large_output_is_drained_with_a_bounded_result(self):
        result = _bounded_command(
            ['python3', '-c', 'print("x" * 1000000)'], Path('/tmp'), 5)
        self.assertEqual(result['exit_code'], 0)
        self.assertEqual(len(result['stdout']), 32768)
        self.assertTrue(result['stdout_truncated'])

    def test_timeout_ends_command_group(self):
        result = _bounded_command(['/bin/sh', '-c', 'sleep 5'], Path('/tmp'), 0.2)
        self.assertTrue(result['timed_out'])
        self.assertIsNone(result['exit_code'])

    def test_command_cannot_consume_mcp_protocol_stdin(self):
        result = _bounded_command(['/bin/sh', '-c', 'read value; printf "empty:%s" "$value"'],
                                  Path('/tmp'), 1)
        self.assertEqual(result['exit_code'], 0)
        self.assertEqual(result['stdout'], 'empty:')

    def test_tool_result_links_command_to_host_artifact(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            workspace = root / 'worktree'
            workspace.mkdir()
            session = {'root': str(root), 'workspace': str(workspace),
                       'command_id': 'command:1', 'deadline_epoch': time.time() + 10}
            with patch('modport.handlers._sandboxed_build_command',
                       return_value=['/bin/echo', 'observed']):
                result = _run(session, 'echo observed', 1)
            self.assertEqual(result['exit_code'], 0)
            self.assertEqual(result['request_command_id'], 'command:1')
            self.assertEqual(result['command_redacted'], 'echo observed')
            self.assertTrue((root / result['artifact_path']).is_file())
            self.assertTrue(result['activity_marker_written'])
            latest = (root / 'artifacts' / 'executions' / 'command:1'
                      / 'opencode-shell' / 'latest-receipt.json')
            self.assertEqual({'schema_version': 1, 'command_id': 'command:1',
                              'receipt': Path(result['artifact_path']).name},
                             json.loads(latest.read_text()))

    def test_activity_marker_failure_does_not_repeat_completed_command(self):
        from modport.evidence import atomic_json

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            workspace = root / 'worktree'
            workspace.mkdir()
            session = {'root': str(root), 'workspace': str(workspace),
                       'command_id': 'command:1', 'deadline_epoch': time.time() + 10}

            def write(path, value):
                if path.name == 'latest-receipt.json':
                    raise OSError('activity marker unavailable')
                atomic_json(path, value)

            with patch('modport.handlers._sandboxed_build_command',
                       return_value=['/bin/echo', 'observed']), \
                    patch('modport.evidence.atomic_json', side_effect=write):
                result = _run(session, 'echo observed', 1)
            self.assertEqual(0, result['exit_code'])
            self.assertFalse(result['activity_marker_written'])
            self.assertTrue((root / result['artifact_path']).is_file())


class CharacterizationToolTests(unittest.TestCase):
    def test_dynamic_author_contract_failure_writes_fresh_typed_host_receipt(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / 'run'
            workspace = root / 'worktree'
            workspace.mkdir(parents=True)
            subprocess.run(['git', 'init', '-q'], cwd=workspace, check=True)
            subprocess.run(['git', 'config', 'user.email', 'test@example.invalid'],
                           cwd=workspace, check=True)
            subprocess.run(['git', 'config', 'user.name', 'Test'], cwd=workspace, check=True)
            (workspace / 'source.txt').write_text('candidate source\n', encoding='utf-8')
            subprocess.run(['git', 'add', 'source.txt'], cwd=workspace, check=True)
            subprocess.run(['git', 'commit', '-qm', 'initial'], cwd=workspace, check=True)
            source_commit = subprocess.run(
                ['git', 'rev-parse', 'HEAD'], cwd=workspace, check=True,
                stdout=subprocess.PIPE, text=True,
            ).stdout.strip()
            contract_path = workspace / '.modport' / 'functional-contract.json'
            contract_path.parent.mkdir(parents=True)
            contract_path.write_text(json.dumps({
                'baseline_gradle_tasks': ['test'],
                'test_evidence': {},
                'behaviors': [],
            }), encoding='utf-8')
            session = {
                'root': str(root), 'workspace': str(workspace),
                'command_id': 'run:contract_draft:1',
                'source_commit': source_commit,
                'contract_sha256': None,
                'contract_path': contract_path.relative_to(root).as_posix(),
                'test_cases': {}, 'init_scripts': [], 'full_tasks': [],
                'prior_case_receipts': {}, 'dynamic_contract': True,
                'execution_nonce': 'registration-nonce',
                'assertions_reviewed': False,
                'deadline_epoch': time.time() + 60,
                'max_invocation_seconds': 60,
            }
            first = _run_characterization(session, {
                'scope': 'selected', 'test_ids': ['case.one'],
            })
            second = _run_characterization(session, {
                'scope': 'selected', 'test_ids': ['case.one'],
            })

            for result in (first, second):
                self.assertEqual('modport.characterization_verification.v1',
                                 result['receipt_type'])
                self.assertEqual('invalid_assertion_or_source_anchor', result['outcome'])
                self.assertEqual('assertion_invalid', result['category'])
                self.assertFalse(result['case_execution_evidence'])
                artifact = root / result['receipt_path']
                self.assertTrue(artifact.is_file())
                receipt = json.loads(artifact.read_text(encoding='utf-8'))
                self.assertEqual(result['receipt_sha256'], receipt['receipt_sha256'])
                receipt.pop('receipt_sha256')
                actual = sha256(json.dumps(
                    receipt, ensure_ascii=False, sort_keys=True, separators=(',', ':')
                ).encode('utf-8')).hexdigest()
                self.assertEqual(result['receipt_sha256'], actual)
            self.assertNotEqual(first['execution_nonce'], second['execution_nonce'])
            self.assertNotEqual(first['invocation_id'], second['invocation_id'])


if __name__ == '__main__':
    unittest.main()
