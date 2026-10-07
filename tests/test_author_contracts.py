"""Published author examples exercise the real evidence gates without project execution."""
from copy import deepcopy
import unittest

from modport.author_contracts import characterization_evidence_schema
from modport.handlers import _test_evidence_declarations, _validate_evidence_record
from modport.rubric import acceptance_rubric


class AuthorEvidenceContractTests(unittest.TestCase):
    def test_published_runtime_and_static_examples_pass_real_gates(self):
        schema = characterization_evidence_schema()
        rubric = acceptance_rubric()
        for kind, declaration_key, record_key in (
            ('runtime', 'declaration', 'runtime_record'),
            ('static_client', 'static_client_declaration', 'static_client_record'),
        ):
            with self.subTest(kind=kind):
                declaration = deepcopy(schema['examples'][declaration_key])
                record = deepcopy(schema['examples'][record_key])
                test_id = record['test_id']
                contract = {'rubric_id': rubric['rubric_id'], 'rubric_version': rubric['rubric_version'],
                            'behaviors': [{'id': 'example', 'side': 'client', 'test_mapping': [test_id]}],
                            'test_evidence': {test_id: declaration}, 'baseline_evidence_files': [declaration['path']]}
                declarations = _test_evidence_declarations(contract, rubric)
                marker = _validate_evidence_record(test_id=test_id, declaration=declarations[test_id],
                    record=record, source_commit=record['source_fingerprint'],
                    execution_nonce=record['execution_nonce'], executor_fingerprint=None)
                self.assertEqual(marker is not None, kind == 'runtime')
                for key in ('execution_inputs', 'observations'):
                    for invalid in ('nonempty text', [], {}):
                        with self.subTest(key=key, invalid=invalid), self.assertRaises(ValueError):
                            _validate_evidence_record(test_id=test_id, declaration=declarations[test_id],
                                record={**record, key: invalid}, source_commit=record['source_fingerprint'],
                                execution_nonce=record['execution_nonce'], executor_fingerprint=None)

    def test_mapping_example_enforces_global_uniqueness(self):
        rubric = acceptance_rubric()
        contract = deepcopy(characterization_evidence_schema()['examples']['mapping'])
        contract.update(rubric_id=rubric['rubric_id'], rubric_version=rubric['rubric_version'])
        _test_evidence_declarations(contract, rubric)
        contract['behaviors'].append({'id': 'other', 'test_mapping': ['example.test']})
        with self.assertRaisesRegex(ValueError, 'globally unique'):
            _test_evidence_declarations(contract, rubric)

    def test_actual_author_prompt_reads_current_template_with_frozen_legacy_rubric(self):
        from dataclasses import replace
        from hashlib import sha256
        import json
        from pathlib import Path
        import subprocess
        from tempfile import TemporaryDirectory
        from types import SimpleNamespace
        from unittest.mock import patch
        from test_handlers import HandlerTests
        from modport.handlers import CodexStageHandler, _validated_characterization_contract
        from modport.prompts import STAGE_PROMPTS

        for version in (3, 4):
            with self.subTest(rubric_version=version), TemporaryDirectory() as raw:
                root = Path(raw)
                (root / 'worktree').mkdir()
                command = HandlerTests._command(root, 'implementation')
                rubric_path = root / command.artifact_refs['acceptance_rubric']['path']
                rubric = json.loads(rubric_path.read_text())
                rubric['rubric_version'] = version
                if version == 3:
                    # Historical schema has field lists but lacks the new typed examples.
                    for key in ('record_shapes', 'record_schemas', 'declaration_schemas', 'examples',
                                'behavior_schema', 'contract_guidance', 'semantics'):
                        rubric['test_evidence_schema'].pop(key, None)
                rubric_path.write_text(json.dumps(rubric))
                command = replace(command, artifact_refs={**command.artifact_refs,
                    'acceptance_rubric': {**command.artifact_refs['acceptance_rubric'],
                                         'sha256': sha256(rubric_path.read_bytes()).hexdigest()}})
                captured = []
                def author(*args, **kwargs):
                    prompt = kwargs['prompt']
                    captured.append(prompt)
                    encoded = prompt.split('read the complete JSON contract at ', 1)[1]
                    ref, _ = json.JSONDecoder().raw_decode(encoded)
                    template = Path(ref['path'])
                    self.assertTrue(template.is_absolute())
                    self.assertEqual(ref['sha256'], sha256(template.read_bytes()).hexdigest())
                    schema = json.loads(template.read_text())
                    self.assertEqual(characterization_evidence_schema(), schema)
                    self.assertNotIn(json.dumps(schema), prompt)
                    contract = {**schema['examples']['contract'],
                                'source_fingerprint': 'source-commit', 'rubric_id': rubric['rubric_id'],
                                'rubric_version': rubric['rubric_version']}
                    typed = _validated_characterization_contract(contract)
                    self.assertTrue(typed.entries[0].preconditions)
                    _test_evidence_declarations(contract, rubric)
                    return subprocess.CompletedProcess([], 0, '')
                compressor = SimpleNamespace(compress=lambda text, **_:
                                             SimpleNamespace(text=text, metadata={}))
                with patch('modport.handlers.PromptCompressor.from_environment',
                           return_value=compressor), \
                        patch('modport.opencode_agent.run_agent', side_effect=author):
                    result = CodexStageHandler(STAGE_PROMPTS['implementation'])(command)
                self.assertEqual(result.status, 'completed', result.detail)
                self.assertEqual(len(captured), 1)

    def test_published_complete_contract_reaches_and_passes_baseline_runtime_gate(self):
        from dataclasses import replace
        import json
        from pathlib import Path
        import subprocess
        from tempfile import TemporaryDirectory
        from unittest.mock import patch
        from test_handlers import HandlerTests
        from modport.handlers import BaselineContractVerificationHandler
        from modport.evidence import atomic_json
        with TemporaryDirectory() as raw:
            root = Path(raw)
            command = HandlerTests._command(root, 'contract_verify')
            command = replace(command, options={**command.options, 'workflow_version': 13})
            schema = characterization_evidence_schema()
            contract = deepcopy(schema['examples']['contract'])
            atomic_json(root / 'baseline/.modport/functional-contract.json', contract)
            atomic_json(root / 'artifacts/source.json', {'source_commit': 'source-commit'})
            (root / 'logs').mkdir()
            for declaration in contract['test_evidence'].values():
                for relative in declaration['test_source_files']:
                    source = root / 'baseline' / relative
                    source.parent.mkdir(parents=True, exist_ok=True)
                    source.write_text('class ExampleTest { void testBehavior() {} }')
            environment = {}
            def sandbox(_root, _worktree, args, **kwargs):
                environment.update(kwargs['environment'])
                self.assertEqual(args[-1], 'test')
                return ['mock-gradle']
            def execute(args, *, log, **kwargs):
                record = deepcopy(schema['examples']['runtime_record'])
                record.update(source_fingerprint='source-commit',
                              execution_nonce=environment['MODPORT_EVIDENCE_NONCE'])
                for witness in record['runtime_witnesses']:
                    witness['execution_nonce'] = record['execution_nonce']
                path = root / 'baseline' / contract['test_evidence'][record['test_id']]['path']
                atomic_json(path, record)
                stdout = '> Task :test\nMODPORT_RUNTIME_WITNESS ' + record['execution_nonce'] + ' ' + record['test_id']
                log.write_text(stdout)
                return subprocess.CompletedProcess(args, 0, stdout)
            with patch('modport.handlers._forge_baseline_init', return_value='/cache/fixture.init.gradle'), \
                 patch('modport.handlers._sandboxed_build_command', side_effect=sandbox), \
                 patch('modport.handlers._exec', side_effect=execute) as process:
                result = BaselineContractVerificationHandler()(command)
            process.assert_called_once()
            self.assertEqual(result.status, 'completed', result.detail)
            self.assertEqual(result.outputs['record_errors'], [])
