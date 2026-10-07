from hashlib import sha256
import unittest
from unittest.mock import patch
from pathlib import Path
import subprocess

from modport.source_compile_probe import prepare_source_probe, compile_source_probe, probe_java_home
from modport.contracts import OperationInput


class SourceCompileProbeTests(unittest.TestCase):
    def setUp(self):
        from test_build_preparation import BuildPreparationTests
        self.fixture = BuildPreparationTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.root, self.work = self.fixture.root, self.fixture.worktree
        self.source = self.work / 'src/main/java/Example.java'
        self.source.parent.mkdir(parents=True)
        self.source.write_text('class Example {}')
        (self.work / 'build.gradle').write_text('custom source build')

    def test_probe_copies_candidate_and_locked_configuration_without_changing_original(self):
        probe, props, record = prepare_source_probe(self.root, self.work, self.fixture.manifest)
        self.assertNotEqual(self.work, probe)
        self.assertEqual('class Example {}', (probe / 'src/main/java/Example.java').read_text())
        self.assertEqual('custom source build', (self.work / 'build.gradle').read_text())
        self.assertEqual(self.fixture.contents['build.gradle'], (probe / 'build.gradle').read_text())
        self.assertEqual(sha256(self.source.read_bytes()).hexdigest(), record['source_files']['src/main/java/Example.java'])
        self.assertFalse(record['project_build_verified'])
        self.assertFalse(record['acceptance_evidence'])
        self.assertEqual((('neo_version', '21.1.77'),), props)

    def test_symlinked_sources_and_tampered_mdk_are_rejected(self):
        link = self.source.parent / 'Linked.java'
        link.symlink_to(self.source)
        with self.assertRaises(ValueError):
            prepare_source_probe(self.root, self.work, self.fixture.manifest)
        link.unlink()
        (self.root / 'toolchains/mdk/build.gradle').write_text('tampered')
        with self.assertRaises(ValueError):
            prepare_source_probe(self.root, self.work, self.fixture.manifest)

    def test_sandbox_compilation_is_provisional_and_detects_input_mutation(self):
        for mutate in (False, True):
            with self.subTest(mutate=mutate):
                command = OperationInput('probe', 'early_compile', 'early_compile',
                                         'probe-' + str(mutate), str(self.root),
                                         options={'workflow_version': 20})
                def execute(args, **kwargs):
                    self.assertIn('bwrap', args[0])
                    self.assertIn('--clearenv', args)
                    self.assertIn('compileJava', args)
                    probe = Path(args[args.index('--bind') + 1])
                    if mutate:
                        (probe / 'src/main/java/Example.java').write_text('different')
                    return subprocess.CompletedProcess(args, 0, 'BUILD SUCCESSFUL')
                with patch('modport.source_compile_probe.probe_java_home', return_value=None), \
                        patch('modport.handlers._exec', side_effect=execute):
                    result = compile_source_probe(command, self.fixture.manifest)
                self.assertEqual('class Example {}', self.source.read_text())
                self.assertIn('gradle_log:provisional_source_compile', result.outputs['artifact_refs'])
                if mutate:
                    self.assertEqual('candidate_identity_mismatch', result.error_code)
                    self.assertFalse(result.outputs['probe_inputs_unchanged'])
                    self.assertFalse(result.outputs['probe_workspace_removed'])
                else:
                    self.assertEqual('completed', result.status)
                    self.assertTrue(result.outputs['probe_workspace_removed'])
                    self.assertFalse(result.outputs['project_build_verified'])
                    self.assertEqual('unverified', result.outputs['acceptance_status'])

    def test_java_probe_uses_authenticated_manifest_and_rejects_changed_bytes(self):
        from dataclasses import replace
        executable = self.root / 'toolchains/gradle-cache/jdk/bin/java'
        executable.parent.mkdir(parents=True)
        executable.write_bytes(b'locked java fixture')
        manifest = replace(self.fixture.manifest, java_toolchain={
            'executable': 'jdk/bin/java', 'java_sha256': sha256(executable.read_bytes()).hexdigest()})
        self.assertEqual(executable.parent.parent, probe_java_home(self.root, manifest))
        executable.write_bytes(b'changed')
        with self.assertRaises(ValueError):
            probe_java_home(self.root, manifest)

    def test_v26_missing_custom_dependencies_remains_a_provisional_diagnostic(self):
        def execute(args, **kwargs):
            return subprocess.CompletedProcess(args, 1,
                '/workspace/src/main/java/Example.java:1: error: package custom.library does not exist')
        for version, expected in ((20, 'failed'), (26, 'completed')):
            with self.subTest(workflow_version=version):
                command = OperationInput('probe', 'early_compile', 'early_compile',
                                         f'probe-v{version}', str(self.root),
                                         options={'workflow_version': version})
                with patch('modport.source_compile_probe.probe_java_home', return_value=None), \
                        patch('modport.handlers._exec', side_effect=execute):
                    result = compile_source_probe(command, self.fixture.manifest)
                self.assertEqual(expected, result.status)
                self.assertEqual('provisional_source_compile_failed',
                                 result.outputs['diagnostic_code'])
                self.assertEqual('failed', result.outputs['probe_status'])
                self.assertEqual(1, result.outputs['probe_returncode'])
                self.assertFalse(result.outputs['project_build_verified'])
                self.assertEqual('unverified', result.outputs['acceptance_status'])
                self.assertIn('gradle_log:provisional_source_compile',
                              result.outputs['artifact_refs'])

    def test_v26_probe_infrastructure_failure_is_not_a_compiler_diagnostic(self):
        command = OperationInput('probe', 'early_compile', 'early_compile',
                                 'probe-infrastructure', str(self.root),
                                 options={'workflow_version': 26})
        with patch('modport.source_compile_probe.probe_java_home', return_value=None), \
                patch('modport.handlers._exec', return_value=subprocess.CompletedProcess(
                    [], 1, 'Could not resolve plugin repository')):
            result = compile_source_probe(command, self.fixture.manifest)
        self.assertEqual('failed', result.status)
        self.assertEqual('provisional_source_compile_failed', result.error_code)
        self.assertEqual(1, result.outputs['probe_returncode'])

    def test_v26_unrelated_java_shaped_log_cannot_complete_probe(self):
        command = OperationInput('probe', 'early_compile', 'early_compile',
                                 'probe-spoofed-log', str(self.root),
                                 options={'workflow_version': 26})
        output = ('Gradle plugin reported: src/main/java/Example.java:1: error: fake\n'
                  '/workspace/src/main/java/Other.java:1: error: not copied\n')
        with patch('modport.source_compile_probe.probe_java_home', return_value=None), \
                patch('modport.handlers._exec', return_value=subprocess.CompletedProcess(
                    [], 1, output)):
            result = compile_source_probe(command, self.fixture.manifest)
        self.assertEqual('failed', result.status)
        self.assertEqual('provisional_source_compile_failed', result.error_code)

    def test_v26_timeout_stays_failed_even_after_partial_compiler_output(self):
        command = OperationInput('probe', 'early_compile', 'early_compile',
                                 'probe-timeout', str(self.root),
                                 options={'workflow_version': 26})
        timeout = subprocess.TimeoutExpired([], 5,
            output=b'/workspace/src/main/java/Example.java:1: error: incomplete')
        with patch('modport.source_compile_probe.probe_java_home', return_value=None), \
                patch('modport.handlers._exec', side_effect=timeout):
            result = compile_source_probe(command, self.fixture.manifest)
        self.assertEqual('failed', result.status)
        self.assertEqual('source_compile_probe_timeout', result.error_code)
        self.assertEqual('timed_out', result.outputs['probe_status'])
