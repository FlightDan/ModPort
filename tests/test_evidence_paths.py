import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import test_handlers
from modport.handlers import BaselineContractVerificationHandler, _test_evidence_declarations
from modport.prompts import STAGE_PROMPTS, build_prompt
from modport.rubric import acceptance_rubric


class EvidencePathTests(unittest.TestCase):
    def test_invalid_declaration_fails_before_source_snapshot_or_execution(self):
        for path in ('build/modport/evidence/game_test.behavior_1.json',
                     '.modport/evidence/../outside.json',
                     '/tmp/evidence.json', '.modport/evidence/',
                     '.modport/evidence//result.json'):
            with self.subTest(path=path), tempfile.TemporaryDirectory() as raw:
                root = Path(raw)
                source, contract = test_handlers.HandlerTests._write_contract_fixture(root)
                contract['baseline_evidence_files'] = [path]
                contract['test_evidence']['game_test.behavior_1']['path'] = path
                source.write_text(json.dumps(contract))
                command = test_handlers.HandlerTests._command(root, 'contract_verify')
                with patch('modport.handlers._runtime_executor_provenance') as provenance, \
                     patch('modport.handlers._exec') as execute:
                    result = BaselineContractVerificationHandler()(command)
                self.assertEqual(result.status, 'failed')
                self.assertEqual(result.error_code, 'baseline_evidence_invalid')
                self.assertIn(path, result.detail)
                self.assertIn('.modport/evidence/game_test.behavior_1.json', result.detail)
                self.assertIn('baseline_evidence_files, test_evidence and the harness evidence producer', result.detail)
                provenance.assert_not_called()
                execute.assert_not_called()

    def test_new_and_existing_valid_declared_paths_are_preserved(self):
        for path in ('.modport/evidence/game_test.behavior_1.json',
                     '.modport/evidence/test-results.json',
                     '.modport/evidence/group/result.json'):
            with self.subTest(path=path), tempfile.TemporaryDirectory() as raw:
                _, contract = test_handlers.HandlerTests._write_contract_fixture(Path(raw))
                contract['baseline_evidence_files'] = [path]
                contract['test_evidence']['game_test.behavior_1']['path'] = path
                actual = _test_evidence_declarations(contract, acceptance_rubric())
                self.assertEqual(actual['game_test.behavior_1']['path'], path)

    def test_harness_prompts_explain_declaration_and_producer_consistency(self):
        for stage in ('contract_draft', 'contract_revise', 'implementation', 'target_revise'):
            with self.subTest(stage=stage), tempfile.TemporaryDirectory() as raw:
                root = Path(raw)
                command = test_handlers.HandlerTests._command(root, stage)
                prompt = build_prompt(STAGE_PROMPTS[stage], command, root, {}, acceptance_rubric())
                self.assertIn('.modport/evidence/<test_id>.json', prompt)
                self.assertIn('baseline_evidence_files', prompt)
                self.assertIn('test_evidence[test_id].path', prompt)
                self.assertIn('actual harness evidence producer', prompt)
                self.assertIn('path relative to the project root', prompt)
                self.assertIn('update both declarations and the producer together', prompt)
                self.assertIn('generated classes, jars and Gradle reports', prompt)
                self.assertIn('Preserve already frozen valid evidence paths', prompt)

    def test_protocol_is_packaged_with_explicit_paths(self):
        root = Path(__file__).resolve().parents[1]
        protocol = (root / 'docs/EVIDENCE_PROTOCOL.md').read_text()
        self.assertEqual(protocol, (root / 'src/modport/rules/EVIDENCE_PROTOCOL.md').read_text())
        for requirement in ('.modport/evidence/<test_id>.json',
                            'baseline_evidence_files', 'test_evidence[test_id].path',
                            'actual harness evidence producer', 'build/modport/evidence/'):
            self.assertIn(requirement, protocol)
