"""Security boundary tests for the managed OpenCode process environment."""
from pathlib import Path
from tempfile import TemporaryDirectory
import json
import signal
import socket
import subprocess
import sys
import time
import unittest
from unittest.mock import patch

from modport.opencode_runtime import (
    OpenCodeConfig,
    OpenCodeCleanupError,
    OpenCodeError,
    OpenCodeEventConnectTimeout,
    OpenCodeHTTPError,
    OpenCodeServer,
    _child_exited_without_reap,
    _ensure_managed_directory,
    _managed_environment,
)


class OpenCodeRuntimeEnvironmentTests(unittest.TestCase):
    def test_child_exit_probe_does_not_reap_owned_process(self):
        child = subprocess.Popen([sys.executable, '-c', 'raise SystemExit(7)'])
        try:
            deadline = time.monotonic() + 2
            while not _child_exited_without_reap(child) and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertTrue(_child_exited_without_reap(child))
            self.assertIsNone(child.returncode)
            self.assertEqual(7, child.wait(timeout=1))
        finally:
            if child.returncode is None:
                child.kill()
                child.wait(timeout=1)

    def test_failed_startup_propagates_unconfirmed_cleanup_without_private_error(self):
        class Child:
            pid = 900003
            args = ['/fixture/opencode']
            returncode = None

            def poll(self):
                return None

        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            with patch('modport.opencode_runtime.subprocess.Popen', return_value=Child()), \
                    patch('modport.opencode_runtime._available_loopback_port', return_value=19001), \
                    patch('modport.opencode_runtime._process_birth', return_value='birth'), \
                    patch.object(OpenCodeServer, '_request', return_value={'version': 'older'}), \
                    patch.object(OpenCodeServer, 'close', return_value={
                        'cleanup_confirmed': False, 'target_pid': 900003,
                        'detail': 'private provider message'}):
                with self.assertRaises(OpenCodeCleanupError) as caught:
                    OpenCodeServer.start(cwd=root, xdg_root=root / 'xdg',
                                         executable=sys.executable)
        self.assertEqual(900003, caught.exception.cleanup_diagnostic['target_pid'])
        self.assertNotIn('private provider message', str(caught.exception))

    def test_startup_exit_signals_group_before_reaping_leader(self):
        class Child:
            pid = 900004
            args = ['/fixture/opencode']
            returncode = None

            def poll(self):
                return self.returncode

            def wait(self, timeout=None):
                self.returncode = 1
                return 1

        child = Child()
        signals = []

        def group_signal(_pid, sig):
            signals.append(sig)

        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            with patch('modport.opencode_runtime.subprocess.Popen', return_value=child), \
                    patch('modport.opencode_runtime._available_loopback_port', return_value=19002), \
                    patch('modport.opencode_runtime._process_birth', return_value='birth'), \
                    patch('modport.opencode_runtime._child_exited_without_reap', return_value=True), \
                    patch('modport.opencode_runtime.os.killpg', side_effect=group_signal):
                with self.assertRaises(OpenCodeCleanupError):
                    OpenCodeServer.start(cwd=root, xdg_root=root / 'xdg',
                                         executable=sys.executable)
        self.assertEqual(signal.SIGKILL, signals[0])
        self.assertEqual(0, signals[-1])

    def test_server_cleanup_is_bounded_when_owned_child_cannot_be_reaped(self):
        class StuckChild:
            pid = 900001
            returncode = None

            def __init__(self):
                self.wait_timeouts = []
                self.direct_kills = 0

            def poll(self):
                return None

            def wait(self, timeout=None):
                self.wait_timeouts.append(timeout)
                raise subprocess.TimeoutExpired('opencode', timeout)

            def kill(self):
                self.direct_kills += 1

        child = StuckChild()
        server = object.__new__(OpenCodeServer)
        server.process = child
        server.process_diagnostic = None
        server.process_birth = 'fixture-birth'
        server.memory_before = None
        server.diagnostic_errors = []
        server.version = 'fixture'
        server.executable = '/fixture/opencode'
        server.executable_sha256 = 'a' * 64
        server.stderr_path = '/fixture/stderr.log'
        server._closed = False
        server.process_identity = {}
        server.argv = ['/fixture/opencode']
        server.cwd = '/fixture'
        with patch('modport.opencode_runtime.os.killpg'):
            diagnostic = server.close()
        self.assertEqual([2.0, 1.0], child.wait_timeouts)
        self.assertEqual(1, child.direct_kills)
        self.assertFalse(diagnostic['cleanup_confirmed'])
        self.assertIsNone(diagnostic['returncode'])
        self.assertIsNone(server.process_diagnostic)

    def test_context_exit_rejects_unconfirmed_server_cleanup(self):
        server = object.__new__(OpenCodeServer)
        server.process = type('Child', (), {'pid': 900007})()
        server.process_birth = 'boot:123'
        with patch.object(server, 'close', return_value={
                'cleanup_confirmed': False, 'target_pid': 900007,
                'detail': 'private provider message'}):
            with self.assertRaises(OpenCodeCleanupError) as caught:
                with server:
                    pass
        self.assertEqual(900007, caught.exception.cleanup_diagnostic['target_pid'])
        self.assertNotIn('private provider message', str(caught.exception))

    def test_leader_exit_does_not_confirm_surviving_process_group(self):
        class ExitingLeader:
            pid = 900002
            returncode = None

            def poll(self):
                return self.returncode

            def wait(self, timeout=None):
                self.returncode = 0
                return 0

        server = object.__new__(OpenCodeServer)
        server.process = ExitingLeader()
        server.process_diagnostic = None
        server.process_birth = 'fixture-birth'
        server.memory_before = None
        server.diagnostic_errors = []
        server.version = 'fixture'
        server.executable = '/fixture/opencode'
        server.executable_sha256 = 'a' * 64
        server.stderr_path = '/fixture/stderr.log'
        server._closed = False
        server.process_identity = {}
        server.argv = ['/fixture/opencode']
        server.cwd = '/fixture'

        def group_signal(_pid, sig):
            if sig == signal.SIGKILL:
                raise PermissionError('synthetic group denial')
            self.assertEqual(0, sig)

        with patch('modport.opencode_runtime.os.killpg', side_effect=group_signal) as signaled, \
                patch('modport.opencode_runtime.PROCESS_GROUP_EXIT_WAIT_SECONDS', 0.01):
            diagnostic = server.close()
        self.assertEqual(signal.SIGKILL, signaled.call_args_list[0].args[1])
        self.assertTrue(diagnostic['leader_exited'])
        self.assertFalse(diagnostic['process_group_gone'])
        self.assertFalse(diagnostic['cleanup_confirmed'])
        self.assertIsNone(server.process_diagnostic)

    def test_server_cleanup_waits_for_descendant_group_to_disappear(self):
        class ExitingLeader:
            pid = 900012
            returncode = None

            def wait(self, timeout=None):
                self.returncode = -9
                return -9

            def poll(self):
                return self.returncode

        server = object.__new__(OpenCodeServer)
        server.process = ExitingLeader()
        server.process_diagnostic = None
        server.process_birth = 'fixture-birth'
        server.memory_before = None
        server.diagnostic_errors = []
        server.version = 'fixture'
        server.executable = '/fixture/opencode'
        server.executable_sha256 = 'a' * 64
        server.stderr_path = '/fixture/stderr.log'
        server._closed = False
        server.process_identity = {}
        server.argv = ['/fixture/opencode']
        server.cwd = '/fixture'
        checks = 0

        def group_signal(_pid, sig):
            nonlocal checks
            if sig == 0:
                checks += 1
                if checks >= 3:
                    raise ProcessLookupError('descendants settled')

        with patch('modport.opencode_runtime.os.killpg', side_effect=group_signal):
            diagnostic = server.close()
        self.assertGreaterEqual(checks, 3)
        self.assertTrue(diagnostic['cleanup_confirmed'])
        self.assertTrue(diagnostic['process_group_gone'])
        self.assertGreater(diagnostic['group_exit_wait_seconds'], 0)

    def test_failed_group_cleanup_records_bounded_member_states(self):
        from modport.opencode_runtime import _process_group_members

        with TemporaryDirectory() as temporary:
            proc_root = Path(temporary)
            for pid, state, group in ((101, 'Z', 4321), (102, 'S', 4321),
                                      (103, 'R', 9999)):
                entry = proc_root / str(pid)
                entry.mkdir()
                fields = [state, '1', str(group), *(['0'] * 16), '222']
                (entry / 'stat').write_text(
                    f"{pid} (worker (test)) {' '.join(fields)}\n", encoding='utf-8')
            observation = _process_group_members(4321, proc_root=proc_root)
            limited = _process_group_members(4321, proc_root=proc_root,
                                             max_entries=1)

        self.assertEqual([(101, 'Z'), (102, 'S')],
                         [(member['pid'], member['state'])
                          for member in observation['members']])
        self.assertTrue(observation['scan_complete'])
        self.assertFalse(observation['truncated'])
        self.assertTrue(limited['scan_limited'])
        self.assertFalse(limited['scan_complete'])
        self.assertEqual(1, limited['scanned_entries'])

    def test_cleanup_accepts_only_complete_zombie_only_group(self):
        class ExitingLeader:
            pid = 900013
            returncode = None

            def wait(self, timeout=None):
                self.returncode = -9
                return -9

            def poll(self):
                return self.returncode

        cases = (
            ([{'pid': 1, 'state': 'Z'}], True, False, False, [], True),
            ([{'pid': 1, 'state': 'Z'}, {'pid': 2, 'state': 'S'}], True, False, False, [], False),
            ([{'pid': 1, 'state': 'Z'}], False, False, False, [], False),
            ([{'pid': 1, 'state': 'Z'}], True, True, False, [], False),
            ([{'pid': 1, 'state': 'Z'}], True, False, True, [], False),
            ([{'pid': 1, 'state': 'Z'}], True, False, False, ['PermissionError'], False),
            ([{'pid': 1, 'state': 'R'}], True, False, False, [], False),
            ([], True, False, False, [], False),
        )
        for members, complete, truncated, limited, errors, expected in cases:
            with self.subTest(members=members, complete=complete, truncated=truncated):
                server = object.__new__(OpenCodeServer)
                server.process = ExitingLeader()
                server.process_diagnostic = None
                server.process_birth = 'fixture-birth'
                server.memory_before = None
                server.diagnostic_errors = []
                server.version = 'fixture'
                server.executable = '/fixture/opencode'
                server.executable_sha256 = 'a' * 64
                server.stderr_path = '/fixture/stderr.log'
                server._closed = False
                server.process_identity = {}
                server.argv = ['/fixture/opencode']
                server.cwd = '/fixture'
                observation = {'members': members, 'scan_complete': complete,
                               'truncated': truncated, 'scan_limited': limited,
                               'scan_errors': errors}
                with patch('modport.opencode_runtime.os.killpg'), \
                        patch('modport.opencode_runtime._process_group_members',
                              return_value=observation), \
                        patch('modport.opencode_runtime.PROCESS_GROUP_EXIT_WAIT_SECONDS', 0):
                    diagnostic = server.close()
                self.assertEqual(expected, diagnostic['cleanup_confirmed'])
                self.assertFalse(diagnostic['process_group_gone'])
                self.assertEqual(expected, diagnostic['process_group_quiescent'])
                self.assertEqual('zombie_only' if expected else 'unconfirmed',
                                 diagnostic['group_quiescence_reason'])

    def test_zombie_scan_cannot_override_failed_group_probe(self):
        class ExitingLeader:
            pid = 900014
            returncode = None

            def wait(self, timeout=None):
                self.returncode = -9
                return -9

            def poll(self):
                return self.returncode

        for fail_probe in (1, 2):
            with self.subTest(fail_probe=fail_probe):
                server = object.__new__(OpenCodeServer)
                server.process = ExitingLeader()
                server.process_diagnostic = None
                server.process_birth = 'fixture-birth'
                server.memory_before = None
                server.diagnostic_errors = []
                server.version = 'fixture'
                server.executable = '/fixture/opencode'
                server.executable_sha256 = 'a' * 64
                server.stderr_path = '/fixture/stderr.log'
                server._closed = False
                server.process_identity = {}
                server.argv = ['/fixture/opencode']
                server.cwd = '/fixture'
                probes = 0

                def group_signal(_pid, sig):
                    nonlocal probes
                    if sig == 0:
                        probes += 1
                        if probes == fail_probe:
                            raise PermissionError('group probe unavailable')

                observation = {'members': [{'pid': 101, 'state': 'Z'}],
                               'scan_complete': True, 'truncated': False,
                               'scan_limited': False, 'scan_errors': []}
                with patch('modport.opencode_runtime.os.killpg', side_effect=group_signal), \
                        patch('modport.opencode_runtime._process_group_members',
                              return_value=observation), \
                        patch('modport.opencode_runtime.PROCESS_GROUP_EXIT_WAIT_SECONDS', 0):
                    diagnostic = server.close()
                self.assertFalse(diagnostic['cleanup_confirmed'])
                self.assertEqual('unconfirmed', diagnostic['group_quiescence_reason'])
                self.assertIn('PermissionError', [row['error'] for row in
                              diagnostic['collection_errors']])

    def test_zombie_group_second_scan_must_remain_quiescent(self):
        class ExitingLeader:
            pid = 900015
            returncode = None

            def wait(self, timeout=None):
                self.returncode = -9
                return -9

            def poll(self):
                return self.returncode

        first = {'members': [{'pid': 101, 'state': 'Z'}],
                 'scan_complete': True, 'truncated': False,
                 'scan_limited': False, 'scan_errors': []}
        for second in (
            {**first, 'members': [{'pid': 101, 'state': 'Z'},
                                  {'pid': 102, 'state': 'S'}]},
            {**first, 'scan_complete': False, 'scan_errors': ['PermissionError']},
        ):
            with self.subTest(second=second):
                server = object.__new__(OpenCodeServer)
                server.process = ExitingLeader()
                server.process_diagnostic = None
                server.process_birth = 'fixture-birth'
                server.memory_before = None
                server.diagnostic_errors = []
                server.version = 'fixture'
                server.executable = '/fixture/opencode'
                server.executable_sha256 = 'a' * 64
                server.stderr_path = '/fixture/stderr.log'
                server._closed = False
                server.process_identity = {}
                server.argv = ['/fixture/opencode']
                server.cwd = '/fixture'
                with patch('modport.opencode_runtime.os.killpg'), \
                        patch('modport.opencode_runtime._process_group_members',
                              side_effect=[first, second]), \
                        patch('modport.opencode_runtime.PROCESS_GROUP_EXIT_WAIT_SECONDS', 0):
                    diagnostic = server.close()
                self.assertFalse(diagnostic['cleanup_confirmed'])
                self.assertEqual(first, diagnostic['process_group_observation_initial'])
                self.assertEqual(second, diagnostic['process_group_observation'])

    def test_event_header_wait_is_bounded_for_reader_close(self):
        with TemporaryDirectory() as temporary:
            server = object.__new__(OpenCodeServer)
            server.base_url = 'http://127.0.0.1:1'
            server.env = {'OPENCODE_SERVER_USERNAME': 'test',
                          'OPENCODE_SERVER_PASSWORD': 'test'}
            with patch('modport.opencode_runtime.urlopen', side_effect=socket.timeout) as opened:
                with self.assertRaises(OpenCodeEventConnectTimeout):
                    list(server.events(cwd=Path(temporary), deadline=time.monotonic() + 30))
            self.assertLessEqual(opened.call_args.kwargs['timeout'], 2.0)

    def test_environment_is_allowlisted_and_config_is_host_owned(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            workspace = root / "workspace"
            workspace.mkdir()
            data_home = root / "user-opencode-data"
            environment = _managed_environment(
                cwd=workspace,
                config=OpenCodeConfig(mcp={"host_tool": {"type": "local"}}),
                env={
                    "PATH": "/usr/bin",
                    "HOME": str(root),
                    "XDG_DATA_HOME": str(data_home),
                    "OPENAI_API_KEY": "fixture-openai-key",
                    "OPENAI_BASE_URL": "https://api.example.invalid",
                    "AWS_SECRET_ACCESS_KEY": "fixture-aws-secret",
                    "GITHUB_TOKEN": "fixture-github-token",
                    "SSH_AUTH_SOCK": "/tmp/agent.sock",
                    "CODEX_API_KEY": "fixture-codex-key",
                    "OPENCODE_CONFIG_DIR": str(root / "untrusted-config"),
                },
                xdg_root=root / "managed",
            )

            self.assertEqual(str(data_home), environment["XDG_DATA_HOME"])
            self.assertEqual("fixture-openai-key", environment["OPENAI_API_KEY"])
            self.assertEqual("https://api.example.invalid", environment["OPENAI_BASE_URL"])
            for name in ("AWS_SECRET_ACCESS_KEY", "GITHUB_TOKEN", "SSH_AUTH_SOCK",
                         "CODEX_API_KEY", "OPENCODE_CONFIG_DIR"):
                self.assertNotIn(name, environment)
            self.assertEqual("true", environment["OPENCODE_DISABLE_PROJECT_CONFIG"])
            self.assertTrue(Path(environment["XDG_CONFIG_HOME"]).is_relative_to(
                root / "managed" / "config"))
            self.assertEqual(
                {"host_tool": {"type": "local"}},
                json.loads(environment["OPENCODE_CONFIG_CONTENT"])["mcp"],
            )

    def test_managed_xdg_path_rejects_a_symlinked_ancestor(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            real = root / "real"
            real.mkdir()
            linked = root / "linked"
            linked.symlink_to(real, target_is_directory=True)

            with self.assertRaisesRegex(OpenCodeError, "symlink"):
                _ensure_managed_directory(linked / "opencode-state")
            self.assertFalse((real / "opencode-state").exists())

    def test_single_message_lookup_uses_session_route_and_preserves_http_error(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            server = object.__new__(OpenCodeServer)
            server._session_directories = {}
            response = {"info": {"id": "msg_current", "role": "assistant"},
                        "parts": [{"type": "tool", "tool": "example"}]}
            with patch.object(server, "_request", return_value=response) as request:
                result = server.get_message("ses_current", "msg_current", cwd=root,
                                            deadline=12.5)
            self.assertEqual(response, result)
            self.assertEqual(request.call_args.args[:2], (
                "GET", "/session/ses_current/message/msg_current"))
            self.assertEqual(request.call_args.kwargs["query"], {"directory": str(root.resolve())})
            self.assertEqual(request.call_args.kwargs["deadline"], 12.5)

            raw_error = OpenCodeHTTPError(400, "Expected OutputFormatJsonSchema")
            with patch.object(server, "_request", side_effect=raw_error):
                with self.assertRaises(OpenCodeHTTPError) as raised:
                    server.get_message("ses_current", "msg_current", cwd=root)
            self.assertIs(raised.exception, raw_error)

if __name__ == "__main__":
    unittest.main()
