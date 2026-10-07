"""Generated target evidence is outside the delivered-product boundary."""
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from modport.artifact_verification import ArtifactTestExecuteHandler, _protected_tree
from modport.contracts import OperationInput, OperationResult
from modport.evidence import atomic_json
from modport.workflow import WORKFLOW_VERSION


class ArtifactRuntimeOutputsTests(unittest.TestCase):
    def test_fresh_outputs_and_cleaned_old_evidence_do_not_change_product_inputs(self):
        with TemporaryDirectory() as folder:
            root = Path(folder)
            workspace = root / 'worktree'
            product = workspace / 'src/Product.java'
            product.parent.mkdir(parents=True)
            product.write_text('delivered product')
            old = workspace / '.modport/evidence/old.json'
            old.parent.mkdir(parents=True)
            old.write_text('prior runtime result')
            # Use raw fixture bytes in place of existing host bookkeeping;
            # this regression must not calculate additional hashes.
            with patch('modport.artifact_verification.file_digest',
                       side_effect=lambda path: Path(path).read_text()):
                protected = _protected_tree(workspace, include_protocol=True,
                    mutable_paths=('.modport/functional-contract.json',), mutable_directories=('tests',))
                self.assertEqual({'src/Product.java': 'delivered product'}, protected)
                prior_snapshot = {**protected, '.modport/evidence/old.json': 'old-host-metadata',
                    '.modport/run-client/old/characterization.log': 'old-host-metadata'}
                atomic_json(root / 'artifacts/product.json', prior_snapshot)
                old.unlink()
                fresh = workspace / '.modport/run-server/fresh/world/state.dat'
                fresh.parent.mkdir(parents=True)
                fresh.write_text('new game output')
                command = OperationInput('run', 'verify', 'artifact_test_execute', 'execution', str(root),
                    options={'workflow_version': WORKFLOW_VERSION}, artifact_refs={
                        'artifact_product_snapshot': {'path': 'artifacts/product.json'}})
                with patch('modport.artifact_verification.prepare_artifact_runtime',
                           return_value=({}, root / 'classes', 'harness', command)) as prepare:
                    result = ArtifactTestExecuteHandler()._prepare(command)
                    self.assertIsInstance(result, tuple)
                    prepare.assert_called_once()
                    product.write_text('changed product')
                    changed = ArtifactTestExecuteHandler()._prepare(command)
                    self.assertIsInstance(changed, OperationResult)
                    self.assertEqual('artifact_candidate_changed', changed.error_code)
                    self.assertFalse(changed.outputs['process_executed'])
                    self.assertEqual(1, prepare.call_count)
                self.assertEqual(prior_snapshot, json.loads((root / 'artifacts/product.json').read_text()))
