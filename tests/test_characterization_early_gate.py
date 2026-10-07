import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from modport.handlers import BaselineContractVerificationHandler
import test_handlers


class EarlyCharacterizationGateTests(unittest.TestCase):
    def test_nonobject_contract_fails_without_launching_project_code(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            fixtures = test_handlers.HandlerTests()
            source, _ = fixtures._write_contract_fixture(root)
            source.write_text('[]')
            command = fixtures._command(root, 'baseline_contract_verify')
            with patch('modport.handlers._exec') as execute:
                result = BaselineContractVerificationHandler()(command)
            self.assertEqual('contract_invalid', result.error_code)
            execute.assert_not_called()

    def test_supervisor_actual_model_prompt_has_self_contained_tool_policy(self):
        import subprocess
        from modport.handlers import CodexStageHandler
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            (root / 'worktree').mkdir()
            command = test_handlers.HandlerTests._command(root, 'supervisor')
            prompts = []
            def execute(args, **kwargs):
                prompts.append(kwargs['input_text'])
                kwargs['log'].write_text('supervisor response')
                return subprocess.CompletedProcess(args, 0, json.dumps({
                    'type': 'item.completed', 'item': {'type': 'agent_message', 'text': 'done'}}))
            with patch('modport.handlers._exec', side_effect=execute):
                result = CodexStageHandler('Supplied packet: no intervention. Do not use tools.')(command)
            self.assertEqual('completed', result.status, result.detail)
            self.assertEqual(1, len(prompts))
            self.assertIn('self-contained', prompts[0])
            self.assertIn('do not use tools or read external files', prompts[0])
            self.assertNotIn('Read the shared', prompts[0])
            self.assertNotIn('agent_rules.md', prompts[0])

    def test_published_target_build_contract_passes_preflight_with_whitespace(self):
        from modport.author_contracts import target_build_requirements, target_build_prompt
        from modport.handlers import AcceptancePreflightHandler
        from modport.models import LockedManifest
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            command = test_handlers.HandlerTests()._target_contract_fixture(root)
            manifest = LockedManifest.from_mapping(json.loads((root / 'artifacts/locked-manifest.json').read_text()))
            contract = target_build_requirements(manifest)
            prompt = target_build_prompt(manifest)
            work = root / 'worktree'
            (work / contract['properties_file']).write_text(''.join(
                f'  {key} = {value}  \n' for key, value in contract['properties'].items()))
            (work / contract['build_file']).write_text(
                'java { toolchain { languageVersion = JavaLanguageVersion . of ( ' + contract['java_version'] + ' ) } }\n')
            result = AcceptancePreflightHandler()(command)
            self.assertEqual('completed', result.status, result.detail)
            self.assertIn(contract['build_file'], prompt)
            self.assertIn(contract['properties_file'], prompt)
            for value in contract['properties'].values():
                self.assertIn(value, prompt)
            (work / contract['properties_file']).write_text('minecraft_version=wrong\nneo_version=wrong\n')
            result = AcceptancePreflightHandler()(command)
            self.assertEqual('target_toolchain_mismatch', result.error_code)
