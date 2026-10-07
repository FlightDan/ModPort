"""Current author compiler registration and diagnostic failure delivery."""
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import time
import unittest
from unittest.mock import patch

from modport.contracts import OperationInput, OperationResult
from modport.artifact_verification import (ArtifactTestDesignHandler,
    declared_test_sources, junit_test_sources, _protected_tree)
from modport.opencode_shell_mcp import _compile_artifact_harness, _dispatch
from modport.prompts import build_prompt


class ArtifactCompilerTests(unittest.TestCase):
    def test_current_artifact_author_receives_selected_repairs_in_all_dialogue_phases(self):
        with TemporaryDirectory() as folder:
            root = Path(folder)
            repair = 'artifacts/handoff/files/repairs/Minecraft1201Startup.java'
            for stage in ('contract_draft', 'contract_revise', 'artifact_test_design'):
                for phase in ('plan', 'execute'):
                    with self.subTest(stage=stage, phase=phase):
                        command = OperationInput('run', stage, stage, stage, str(root),
                            artifact_refs={
                                'artifact_handoff': {'path': 'artifacts/handoff/manifest.json'},
                                'handoff:repairs/Minecraft1201Startup.java': {'path': repair}},
                            options={'workflow_version': 31, 'dialogue_phase': phase,
                                     'validation_policy': {'scope': 'artifact_verification'}})
                        build_prompt('Repair the assigned harness.', command, root, {}, {})
                        packet = json.loads((root / 'artifacts/executions' / stage /
                            ('task-instructions.' + phase + '.json')).read_text())
                        self.assertIn('user-authorized repaired sources', packet['task'])
                        self.assertIn(str(root / repair), packet['task'])
                        self.assertIn('Apply target repairs to the target harness only', packet['task'])
                        self.assertIn('not current approvals', packet['task'])

    def test_compiler_session_exposes_only_host_owned_compilation(self):
        with patch('modport.opencode_shell_mcp._response') as response:
            _dispatch({'kind': 'artifact_compile'}, {'id': 1, 'method': 'tools/list'})
        tools = response.call_args.kwargs['result']['tools']
        self.assertEqual(['compile_artifact_harness'], [tool['name'] for tool in tools])
        self.assertNotIn('command', tools[0]['inputSchema']['properties'])
        with patch('modport.opencode_shell_mcp._response') as response:
            _dispatch({'kind': 'artifact_compile'}, {
                'id': 2, 'method': 'tools/call', 'params': {
                    'name': 'run_project_command', 'arguments': {'command': 'runClient'}}})
        self.assertIn('disabled', response.call_args.kwargs['error']['message'])

    def test_raw_compiler_failure_reaches_author_with_exact_binary_wiring(self):
        with TemporaryDirectory() as folder:
            root = Path(folder)
            workspace = root / 'worktree'
            workspace.mkdir()
            script = root / 'artifacts/harness-wiring/current-artifact.init.gradle'
            script.parent.mkdir(parents=True)
            script.write_text('// host binary wiring')
            command = OperationInput('run', 'design', 'artifact_test_design', 'current-design',
                str(root), payload={'request': {'target_minecraft': '26.1.2'}}, options={
                    'workflow_version': 31, 'artifact_init_script': str(script),
                    'artifact_runtime_directory': str(root / 'artifacts/artifact-runtime/current-design')})
            session = {'root': str(root), 'workspace': str(workspace),
                'kind': 'artifact_compile', 'command_id': command.command_id,
                'operation': command.to_dict(), 'java_home': str(root / 'java25'),
                'deadline_epoch': time.time() + 60}
            with patch('modport.harness_wiring.characterization_init_scripts', return_value=()), \
                    patch('modport.handlers._sandboxed_build_command', return_value=['sandbox']) as sandbox, \
                    patch('modport.opencode_shell_mcp._bounded_command', return_value={
                        'exit_code': 1, 'stdout': '', 'stderr': 'error: cannot find symbol Wolf'}):
                result = _compile_artifact_harness(session, {'timeout_seconds': 30})
            args = sandbox.call_args.args[2]
            self.assertEqual(['compileJava', 'compileTestJava'], args[-2:])
            self.assertIn('/modport-wiring/current-artifact.init.gradle', args)
            self.assertEqual(Path(session['java_home']), sandbox.call_args.kwargs['java_home'])
            prepared_options = sandbox.call_args.kwargs['operation'].options
            self.assertEqual(command.options, {key: prepared_options[key] for key in command.options})
            self.assertLessEqual(prepared_options['model_deadline_epoch'], session['deadline_epoch'])
            self.assertLessEqual(prepared_options['model_deadline_epoch'], time.time() + 30)
            self.assertEqual(1, result['exit_code'])
            self.assertIn('cannot find symbol Wolf', result['stderr'])
            self.assertFalse(result['acceptance_evidence'])
            archived = json.loads((root / result['artifact_path']).read_text())
            self.assertEqual(result['stderr'], archived['stderr'])

    def test_caller_cannot_select_a_gameplay_task(self):
        with self.assertRaisesRegex(ValueError, 'only timeout_seconds'):
            _compile_artifact_harness({}, {'command': 'runClient'})

    def test_declared_junit_inside_harness_is_compiled_as_a_test(self):
        with TemporaryDirectory() as folder:
            root = Path(folder)
            lock = root / 'lock.json'
            lock.write_text(json.dumps({'test_evidence': {'case': {
                'result_identity': {'classname': 'example.ExampleTest'},
                'test_source_files': ['.modport/harness/example/ExampleTest.java',
                                      '.modport/harness/example/RuntimeHelper.java']}}}))
            command = OperationInput('run', 'design', 'artifact_test_design', 'design', str(root),
                artifact_refs={'functional_contract_lock': {'path': 'lock.json'}})
            with patch('modport.artifact_verification.verified_path', return_value=lock):
                self.assertEqual(('.modport/harness/example/ExampleTest.java',),
                                 junit_test_sources(command))

    def test_target_author_and_runtime_receive_current_frozen_contract(self):
        with TemporaryDirectory() as folder:
            root = Path(folder)
            target = root / 'worktree/.modport/functional-contract.json'
            target.parent.mkdir(parents=True)
            product = root / 'worktree/Product.java'
            product.write_text('{"contract_id":"stale-handoff"}')
            os.link(product, target)
            lock = root / 'lock.json'
            lock.write_text('{"contract_id":"current-frozen","test_evidence":{}}')
            command = OperationInput('run', 'design', 'artifact_test_design', 'design', str(root),
                artifact_refs={'functional_contract_lock': {'path': 'lock.json'}})
            def author(prepared):
                self.assertEqual('current-frozen', json.loads(target.read_text())['contract_id'])
                return OperationResult('completed')
            with patch('modport.artifact_verification.verified_path', return_value=lock), \
                    patch('modport.artifact_verification.prepare_artifact_runtime',
                          return_value=({}, None, None, command)), \
                    patch('modport.artifact_verification._protected_tree', return_value={}), \
                    patch('modport.artifact_verification.file_digest', return_value='existing-host-reference'), \
                    patch('modport.handlers.CodexStageHandler', return_value=author):
                self.assertEqual('completed', ArtifactTestDesignHandler()(command).status)
            archived = root / 'artifacts/executions/design/target-contract-before-adaptation.json'
            self.assertEqual('stale-handoff', json.loads(archived.read_text())['contract_id'])
            self.assertEqual('stale-handoff', json.loads(product.read_text())['contract_id'])

    def test_only_frozen_test_files_are_mutable_and_contract_remains_protected(self):
        with TemporaryDirectory() as folder:
            root = Path(folder)
            workspace = root / 'worktree'
            java = workspace / '.modport/tests/example/Case.java'
            java.parent.mkdir(parents=True)
            java.write_text('original test')
            contract = workspace / '.modport/functional-contract.json'
            contract.write_text('frozen contract')
            lock = root / 'lock.json'
            lock.write_text(json.dumps({'test_evidence': {'case': {
                'test_source_files': [java.relative_to(workspace).as_posix()]}}}))
            command = OperationInput('run', 'design', 'artifact_test_design', 'design', str(root),
                artifact_refs={'functional_contract_lock': {'path': 'lock.json'}})
            with patch('modport.artifact_verification.verified_path', return_value=lock):
                allowed = declared_test_sources(command)
                before = _protected_tree(workspace, include_protocol=True, mutable_paths=allowed)
                java.write_text('adapted target API')
                self.assertEqual(before, _protected_tree(workspace, include_protocol=True, mutable_paths=allowed))
                contract.write_text('changed contract')
                self.assertNotEqual(before, _protected_tree(workspace, include_protocol=True, mutable_paths=allowed))
                lock.write_text(json.dumps({'test_evidence': {'case': {
                    'test_source_files': ['.modport/../../product.java']}}}))
                with self.assertRaisesRegex(ValueError, 'contained'):
                    declared_test_sources(command)


if __name__ == '__main__':
    unittest.main()
