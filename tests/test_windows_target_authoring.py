"""Portable native target-path authoring checks, not Windows game acceptance."""
from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from modport.client_harness import client_harness_support_files
from modport import independent_tests, memory_admission
from modport.memory_admission import MemorySnapshot
from modport.workflow import WORKFLOW_VERSION


class WindowsTargetAuthoringTests(unittest.TestCase):
    def test_windows_preflight_does_not_claim_display_opengl_or_audio_success(self):
        source = client_harness_support_files(workflow_version=WORKFLOW_VERSION)['preflight.py']
        namespace = {'__name__': 'portable_fixture'}
        exec(compile(source, 'preflight.py', 'exec'), namespace)
        namespace['os'] = SimpleNamespace(name='nt', environ={})
        output = io.StringIO()
        with redirect_stdout(output), patch.object(sys, 'argv', ['preflight.py', '--game-directory=game']), \
                patch.object(namespace['shutil'], 'which', return_value=None):
            namespace['main']()
        report = json.loads(output.getvalue().removeprefix('MODPORT_CLIENT_PREFLIGHT '))
        self.assertEqual(report['platform'], 'windows')
        self.assertEqual(report['display']['status'], 'unknown')
        self.assertEqual(report['opengl']['status'], 'unknown')
        self.assertEqual(report['audio_devices']['status'], 'unknown')
        self.assertEqual(report['xvfb']['status'], 'not_applicable')
        self.assertFalse(report['acceptance_evidence'])

    def test_generated_client_launcher_native_branch_reaches_workload_without_xvfb(self):
        files = client_harness_support_files(workflow_version=WORKFLOW_VERSION)
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            # Actual producer preflight, with only OS identity substituted.
            preflight = files['preflight.py'].replace('def main():',
                'from types import SimpleNamespace\nos = SimpleNamespace(name="nt", environ={})\ndef main():')
            (root/'preflight.py').write_text(preflight)
            namespace = {'__name__': 'portable_fixture', '__file__': str(root/'launch.py')}
            exec(compile(files['launch.py'], 'launch.py', 'exec'), namespace)
            namespace['os'] = SimpleNamespace(name='nt', environ={'PATH': ''}, chmod=os.chmod)
            output = io.StringIO()
            with redirect_stdout(output), patch.object(sys, 'argv',
                    ['launch.py', '--timeout', '5', '--', sys.executable, '-c', 'raise SystemExit(0)']), \
                    patch.object(namespace['shutil'], 'which', side_effect=AssertionError('native launch must not query Xvfb')), \
                    patch.object(namespace['selectors'], 'DefaultSelector', side_effect=AssertionError('native pipes must not use selectors')):
                result = namespace['main']()
            self.assertEqual(result, 0, output.getvalue())
            report = json.loads(output.getvalue().removeprefix('MODPORT_CLIENT_PREFLIGHT '))
            self.assertEqual(report['process_cleanup_scope'], 'outer_host_appcontainer_job')
            self.assertEqual(report['display']['status'], 'unknown')
            self.assertFalse(report['display_owned'])
            self.assertFalse(report['acceptance_evidence'])
            self.assertTrue(all(child.poll() is not None for child in namespace['owned']))

    def test_native_readonly_test_runner_uses_declared_output_grants(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            workspace = root/'isolated-tests'
            workspace.mkdir()
            with patch.object(independent_tests, 'os', SimpleNamespace(name='nt')), \
                    patch.object(independent_tests.handlers, '_locked_java_home', return_value=root/'jdk'), \
                    patch.object(independent_tests.handlers, '_sandboxed_build_command', return_value=['native-sandbox']) as builder:
                command = independent_tests._readonly_build_command(root, workspace, ['bash', '/workspace/gradlew', 'test'])
            self.assertEqual(command, ['native-sandbox'])
            self.assertTrue(builder.call_args.kwargs['readonly_workspace'])
            self.assertEqual(set(builder.call_args.kwargs['writable_workspace_paths']), set(independent_tests._OUTPUTS))
            self.assertTrue(all((workspace/name).is_dir() for name in independent_tests._OUTPUTS))

    def test_windows_memory_admission_uses_physical_and_job_available_observation(self):
        with patch.object(memory_admission, 'os', SimpleNamespace(name='nt')), \
                patch('modport.platform_runtime.windows_memory_snapshot', return_value=(123, 456, 'windows_physical+job')) as probe:
            self.assertEqual(memory_admission.host_memory_snapshot(), MemorySnapshot(123, 456, 'windows_physical+job'))
            probe.assert_called_once_with()
        with patch.object(memory_admission, 'os', SimpleNamespace(name='nt')), \
                patch('modport.platform_runtime.windows_memory_snapshot', return_value=(None, None, 'job_query_unavailable')):
            self.assertEqual(memory_admission.host_memory_snapshot(), MemorySnapshot(None, None, 'job_query_unavailable'))


if __name__ == '__main__':
    unittest.main()
