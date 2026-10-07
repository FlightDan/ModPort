import json
from pathlib import Path
import tempfile
import unittest
from zipfile import ZipFile

from modport.artifact_verification import (ArtifactTestExecuteHandler, _protected_tree,
    extract_binary, install_artifact_input)
from modport.contracts import OperationInput
from modport.evidence import file_digest


class ArtifactInputTests(unittest.TestCase):
    def test_package_receipt_binds_jar_bytes_and_target_commit(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            jar = root / 'artifacts' / 'delivered.jar'
            jar.parent.mkdir()
            jar.write_bytes(b'exact delivered binary')
            receipt = root / 'artifacts' / 'target-package-receipt.json'
            value = {'status':'passed','target_clean':True,'run_id':'old-run',
                     'target_commit':'a' * 40,'artifacts':[{'path':'worktree/build/libs/mod.jar',
                     'sha256':file_digest(jar),'size':jar.stat().st_size}]}
            receipt.write_text(json.dumps(value))
            manifest = {'source':{'run_id':'old-run','target_commit':'a' * 40},
                        'artifacts':[{'source_path':'artifacts/build/target-package-receipt.json'}]}
            refs = {'handoff:artifacts/build/target-package-receipt.json':
                    {'path':receipt.relative_to(root).as_posix(),'sha256':file_digest(receipt)},
                    'handoff:worktree/build/libs/mod.jar':
                    {'path':jar.relative_to(root).as_posix(),'sha256':file_digest(jar)}}
            installed = install_artifact_input(root,manifest,refs)
            identity = json.loads((root / installed['artifact_input']['path']).read_text())
            self.assertEqual(file_digest(jar),identity['jar_ref']['sha256'])
            value['target_commit'] = 'b' * 40
            receipt.write_text(json.dumps(value))
            refs['handoff:artifacts/build/target-package-receipt.json']['sha256'] = file_digest(receipt)
            with self.assertRaisesRegex(ValueError,'delivered candidate'):
                install_artifact_input(root,manifest,refs)

    def test_delivered_archive_cannot_escape_or_shadow_an_entry(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            jar = root / 'bad.jar'
            with ZipFile(jar,'w') as archive:
                archive.writestr('../outside.class',b'bad')
            with self.assertRaisesRegex(ValueError,'unsafe'):
                extract_binary(jar,root/'expanded')
            self.assertFalse((root/'outside.class').exists())

    def test_protocol_edits_and_setup_failure_are_detected_before_execution(self):
        with tempfile.TemporaryDirectory() as raw:
            root=Path(raw)
            workspace=root/'worktree'
            protocol=workspace/'.modport'
            protocol.mkdir(parents=True)
            (protocol/'functional-contract.json').write_text('original contract')
            before=_protected_tree(workspace,include_protocol=True)
            (protocol/'functional-contract.json').write_text('weakened contract')
            self.assertNotEqual(before,_protected_tree(workspace,include_protocol=True))
            harness=protocol/'harness'
            harness.symlink_to(workspace/'src')
            with self.assertRaisesRegex(ValueError,'symlink'):
                _protected_tree(workspace,include_protocol=True)
            harness.unlink()
            command=OperationInput('run','test','artifact_test_execute','run:test:1',str(root))
            result=ArtifactTestExecuteHandler()(command)
            self.assertEqual('failed',result.status)
            self.assertFalse(result.outputs['process_executed'])
            self.assertEqual('unverified',result.outputs['acceptance_status'])
            self.assertIn('artifact_setup_failure',result.outputs['artifact_refs'])


if __name__ == '__main__':
    unittest.main()
