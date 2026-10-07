"""Meaningful optional-projection and native-runtime-copy boundaries."""
import json
import http.client
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from modport.desktop_state import publish_snapshot_safely
from modport.desktop_service import DesktopServer
from modport.windows_build import _materialize_runtime


class DesktopIntegrationSafetyTests(unittest.TestCase):
    def test_service_shutdown_drains_accepted_request_after_disconnect(self):
        entered, release, completed = threading.Event(), threading.Event(), threading.Event()
        class Application:
            def request(self, method, path, body):
                entered.set()
                if not release.wait(5):
                    raise TimeoutError('test request was not released')
                completed.set()
                return {'completed': True}
        server = DesktopServer(('127.0.0.1', 0), Application(), 'a' * 64)
        thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': .01})
        thread.start()
        connection = http.client.HTTPConnection(*server.server_address, timeout=2)
        try:
            connection.request('POST', '/api/runs', '{}', {'Authorization': 'Bearer ' + 'a' * 64, 'Content-Type': 'application/json'})
            self.assertTrue(entered.wait(2))
            connection.close()
            server.shutdown()
            closed = threading.Event()
            def close():
                server.server_close()
                closed.set()
            closing = threading.Thread(target=close)
            closing.start()
            self.assertFalse(closed.wait(.1))
            release.set()
            self.assertTrue(closed.wait(2))
            self.assertTrue(completed.is_set())
            closing.join()
        finally:
            release.set()
            connection.close()
            server.shutdown()
            server.server_close()
            thread.join()

    def test_projection_failure_records_diagnostic_without_escaping(self):
        header = {'run_dir': '/tmp/unused-projection-test', 'run_id': 'desktop-test'}
        with patch('modport.desktop_state.publish_snapshot', side_effect=OSError('disk unavailable')), \
             patch('modport.telemetry.record_event') as record:
            publish_snapshot_safely(header, {'revision': 3})
        self.assertEqual(record.call_args.args[2], 'desktop.error')
        self.assertEqual(record.call_args.args[3]['detail'], 'disk unavailable')

    def test_runtime_copy_owns_files_and_reuses_published_source_navigation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / 'installed-runtime'
            (source / 'bin').mkdir(parents=True)
            (source / 'bin' / 'java.exe').write_text('runtime fixture')
            installed = _materialize_runtime(root / 'instance', source, 'jdk')
            self.assertEqual((installed / 'bin' / 'java.exe').read_text(), 'runtime fixture')
            self.assertNotEqual((installed / 'bin' / 'java.exe').stat().st_ino,
                                (source / 'bin' / 'java.exe').stat().st_ino)
            self.assertEqual(installed, _materialize_runtime(root / 'instance', source, 'jdk'))

    def test_runtime_copy_rejects_redirects_before_publication(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / 'runtime'
            source.mkdir()
            (source / 'redirect').symlink_to(root)
            with self.assertRaises((OSError, ValueError)):
                _materialize_runtime(root / 'instance', source, 'jdk')
            self.assertFalse((root / 'instance' / 'toolchains' / 'native-runtimes' / 'runtimes.json').exists())


if __name__ == '__main__':
    unittest.main()
