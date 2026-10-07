"""Exercise authenticated harness capture, transfer, and fresh child restore."""
from dataclasses import replace
from hashlib import sha256
import json
from pathlib import Path
import tempfile
import unittest

from modport.contracts import OperationInput
from modport.evidence import digest, file_digest, seal_ref, verified_path
from modport.harness_snapshot import capture_harness, RestoreHarnessHandler
from modport.retry_policy import build_harness_snapshot


class HarnessSnapshotTests(unittest.TestCase):
    SOURCE_COMMIT = 'a' * 40
    SOURCES = {
        '.modport/functional-contract.json': b'{"test_evidence": {}}',
        '.modport/init.gradle': b'// baseline build setup',
        '.modport/characterization/src/main/java/p/build/Test.java': b'class Test {}',
        '.modport/fixtures/input.json': b'{"fixture": "original"}',
    }
    OUTPUTS = {
        '.modport/evidence/passed.json': b'{"passed": true}',
        '.modport/characterization/run/saves/world/level.dat': b'world',
        '.modport/client/build/classes/Test.class': b'compiled',
        '.modport/characterization/logs/latest.log': b'log',
        '.modport/background.md': b'old host background',
    }

    def write_ref(self, root, relative, data, execution='host'):
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return seal_ref(root, {'path': relative, 'sha256': file_digest(path)}, execution_id=execution)

    def command(self, root, refs, stage='contract_revise'):
        return OperationInput('run', 'task', stage, 'execution', str(root), artifact_refs=refs)

    def parent(self, root):
        source = self.write_ref(root, 'artifacts/source.json', json.dumps({'source_commit': self.SOURCE_COMMIT}).encode())
        for relative, data in {**self.SOURCES, **self.OUTPUTS}.items():
            path = root / 'baseline' / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        command = self.command(root, {'source_evidence': source})
        return command, {**command.artifact_refs, **capture_harness(command)}

    def child(self, parent, child):
        _, parent_refs = self.parent(parent)
        snapshot = build_harness_snapshot(parent, parent_refs, parent_run_id='parent', source_commit=self.SOURCE_COMMIT)
        source = self.write_ref(child, 'artifacts/source.json', json.dumps({'source_commit': self.SOURCE_COMMIT}).encode())
        refs = {'source_evidence': source}
        for index, entry in enumerate(snapshot['files']):
            ref = self.write_ref(child, f'artifacts/transfer/file-{index}', verified_path(parent, entry['ref']).read_bytes())
            entry['ref'] = ref
            refs['inherited_harness:' + entry['path']] = ref
        snapshot['provenance_ref'] = self.write_ref(child, 'artifacts/transfer/provenance.json',
            verified_path(parent, snapshot['provenance_ref']).read_bytes())
        self.set_snapshot(child, refs, snapshot)
        return self.command(child, refs, 'harness_restore'), snapshot

    def set_snapshot(self, root, refs, snapshot):
        snapshot.pop('snapshot_sha256', None)
        snapshot['snapshot_sha256'] = digest(snapshot)
        refs['inherited_harness'] = self.write_ref(root, 'artifacts/inherited.json', json.dumps(snapshot).encode())

    def test_capture_preserves_history_while_retry_inherits_current_sources(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            command, refs = self.parent(root)
            snapshot = json.loads(verified_path(root, refs['baseline_harness_snapshot']).read_text())
            self.assertEqual(set(snapshot['files']), set(self.SOURCES))
            self.assertEqual(snapshot['source_commit'], self.SOURCE_COMMIT)
            self.assertEqual(snapshot['candidate_sha256'], sha256(self.SOURCES['.modport/functional-contract.json']).hexdigest())
            for relative, ref in snapshot['files'].items():
                self.assertEqual(ref['metadata']['execution_id'], command.command_id)
                self.assertEqual(ref['metadata']['source_path'], 'baseline/' + relative)
                self.assertEqual(verified_path(root, ref).read_bytes(), self.SOURCES[relative])
            original = snapshot['files']['.modport/init.gradle']
            (root / 'baseline/.modport/init.gradle').write_text('mutable edit after capture')
            self.assertEqual(verified_path(root, original).read_bytes(), self.SOURCES['.modport/init.gradle'])
            transferred = build_harness_snapshot(root, refs, parent_run_id='parent', source_commit=self.SOURCE_COMMIT)
            entry = next(entry for entry in transferred['files'] if entry['path'] == '.modport/init.gradle')
            self.assertEqual(verified_path(root, entry['ref']).read_bytes(), b'mutable edit after capture')

    def test_failed_revision_cannot_remove_captured_contract_evidence(self):
        from modport.repair_evidence import snapshot_repair_evidence
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _, refs = self.parent(root)
            # The author clears this required output, then fails to replace it.
            (root / 'baseline/.modport/functional-contract.json').unlink()
            saved = snapshot_repair_evidence(root, {'artifact_refs': refs})
            contract = saved['artifact_refs']['baseline_harness_source:.modport/functional-contract.json']
            self.assertEqual(verified_path(root, contract).read_bytes(),
                             self.SOURCES['.modport/functional-contract.json'])
            manifest = json.loads(verified_path(root, refs['baseline_harness_snapshot']).read_text())
            self.assertEqual(verified_path(root, manifest['files']['.modport/functional-contract.json']).read_bytes(),
                             self.SOURCES['.modport/functional-contract.json'])

    def test_restore_uses_child_authenticated_sources_and_requires_fresh_verification(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            command, _ = self.child(root / 'parent', root / 'child')
            result = RestoreHarnessHandler()(command)
            self.assertEqual(result.status, 'completed', result.detail)
            self.assertTrue(result.outputs['requires_fresh_verification'])
            self.assertEqual(result.outputs['inherited_from'], 'parent')
            baseline = root / 'child/baseline'
            actual = {p.relative_to(baseline).as_posix(): p.read_bytes() for p in baseline.rglob('*') if p.is_file()}
            self.assertEqual(actual, self.SOURCES)
            self.assertNotIn('baseline_contract_tests', result.outputs['artifact_refs'])
            self.assertIn('baseline_harness_snapshot', result.outputs['artifact_refs'])
            # Repeated recovery with identical bytes is safe and idempotent.
            self.assertEqual(RestoreHarnessHandler()(command).status, 'completed')

    def test_capture_rejects_symlink_and_accepts_source_evidence_updates(self):
        for defect in ('symlink', 'source_tamper'):
            with self.subTest(defect=defect), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                command, _ = self.parent(root)
                if defect == 'symlink':
                    source = root / 'baseline/.modport/init.gradle'
                    source.unlink()
                    source.symlink_to(root / 'artifacts/source.json')
                else:
                    verified_path(root, command.artifact_refs['source_evidence']).write_text('{"source_commit":"tampered"}')
                if defect == 'symlink':
                    with self.assertRaises(ValueError):
                        capture_harness(command)
                else:
                    refs = capture_harness(command)
                    manifest = json.loads(verified_path(root, refs['baseline_harness_snapshot']).read_text())
                    self.assertEqual(manifest['source_commit'], 'tampered')

    def test_restore_accepts_updates_but_rejects_invalid_identity_or_paths_before_writes(self):
        for defect in ('bytes', 'source_identity', 'missing_authority', 'snapshot_digest', 'conflict', 'destination_symlink'):
            with self.subTest(defect=defect), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                command, snapshot = self.child(root / 'parent', root / 'child')
                refs = dict(command.artifact_refs)
                child = root / 'child'
                if defect == 'bytes':
                    verified_path(child, snapshot['files'][-1]['ref']).write_text('corrupt')
                elif defect == 'source_identity':
                    refs['source_evidence'] = self.write_ref(child, 'artifacts/other-source.json', json.dumps({'source_commit': 'b' * 40}).encode())
                elif defect == 'missing_authority':
                    del refs['inherited_harness:' + snapshot['files'][-1]['path']]
                elif defect == 'snapshot_digest':
                    snapshot['parent_run_id'] = 'forged-parent'
                    refs['inherited_harness'] = self.write_ref(child, 'artifacts/forged.json', json.dumps(snapshot).encode())
                elif defect == 'conflict':
                    path = child / 'baseline/.modport/init.gradle'
                    path.parent.mkdir(parents=True)
                    path.write_text('different checkout bytes')
                else:
                    outside = child / 'outside'
                    outside.mkdir()
                    (child / 'baseline').mkdir()
                    (child / 'baseline/.modport').symlink_to(outside, target_is_directory=True)
                result = RestoreHarnessHandler()(replace(command, artifact_refs=refs))
                if defect in ('bytes', 'snapshot_digest', 'conflict'):
                    self.assertEqual(result.status, 'completed', result.detail)
                    self.assertTrue(result.outputs['requires_fresh_verification'])
                    if defect == 'bytes':
                        self.assertEqual((child / 'baseline' / snapshot['files'][-1]['path']).read_text(), 'corrupt')
                    elif defect == 'snapshot_digest':
                        self.assertEqual(result.outputs['inherited_from'], 'forged-parent')
                    else:
                        self.assertEqual((child / 'baseline/.modport/init.gradle').read_bytes(), self.SOURCES['.modport/init.gradle'])
                else:
                    self.assertEqual(result.status, 'blocked', result.detail)
                    self.assertEqual(result.error_code, 'inherited_harness_invalid')
                    self.assertFalse((child / 'baseline/.modport/functional-contract.json').exists())

    def test_restore_rejects_authenticated_manifest_with_runtime_or_duplicate_path(self):
        for relative in ('.modport/client/build/generated/Test.java', '.modport/characterization/run/Test.groovy',
                         '.modport/evidence/passed.json', '../outside', '.modport/.git/config', '.modport/init.gradle'):
            with self.subTest(relative=relative), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                command, snapshot = self.child(root / 'parent', root / 'child')
                refs = dict(command.artifact_refs)
                child = root / 'child'
                ref = (refs['inherited_harness:' + relative] if relative == '.modport/init.gradle'
                       else self.write_ref(child, 'artifacts/extra-file', b'not inherited source'))
                snapshot['files'].append({'path': relative, 'parent_ref_key': 'extra', 'ref': ref})
                refs['inherited_harness:' + relative] = ref
                self.set_snapshot(child, refs, snapshot)
                result = RestoreHarnessHandler()(replace(command, artifact_refs=refs))
                self.assertEqual(result.status, 'blocked', result.detail)
                self.assertFalse((child / 'baseline/.modport/functional-contract.json').exists())

    def test_restore_prevalidates_later_destination_before_writing_first_file(self):
        for defect in ('symlink', 'file_parent'):
            with self.subTest(defect=defect), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                command, snapshot = self.child(root / 'parent', root / 'child')
                child = root / 'child'
                refs = dict(command.artifact_refs)
                # Put the safe contract first to expose incremental-write bugs.
                snapshot['files'].sort(key=lambda entry: entry['path'] != '.modport/functional-contract.json')
                self.set_snapshot(child, refs, snapshot)
                parent = child / 'baseline/.modport/fixtures'
                parent.parent.mkdir(parents=True)
                outside = child / 'outside'
                outside.mkdir()
                if defect == 'symlink':
                    parent.symlink_to(outside, target_is_directory=True)
                else:
                    parent.write_text('regular file blocks directory creation')
                result = RestoreHarnessHandler()(replace(command, artifact_refs=refs))
                self.assertEqual(result.status, 'blocked', result.detail)
                self.assertFalse((child / 'baseline/.modport/functional-contract.json').exists())
                self.assertEqual(list(outside.iterdir()), [])

    def test_restore_prevalidates_manifest_identity_and_destination_collisions(self):
        for defect in ('missing_parent', 'blank_parent', 'ancestor_conflict'):
            with self.subTest(defect=defect), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                child = root / 'child'
                command, snapshot = self.child(root / 'parent', child)
                refs = dict(command.artifact_refs)
                if defect == 'missing_parent':
                    snapshot.pop('parent_run_id')
                elif defect == 'blank_parent':
                    snapshot['parent_run_id'] = ' '
                else:
                    for index, relative in enumerate(('.modport/newfile', '.modport/newfile/child')):
                        ref = self.write_ref(child, f'artifacts/collision-{index}', b'harness source')
                        snapshot['files'].append({'path': relative, 'ref': ref})
                        refs['inherited_harness:' + relative] = ref
                self.set_snapshot(child, refs, snapshot)
                result = RestoreHarnessHandler()(replace(command, artifact_refs=refs))
                self.assertEqual(result.status, 'blocked', result.detail)
                self.assertFalse((child / 'baseline/.modport/functional-contract.json').exists())
                self.assertFalse((child / 'baseline/.modport/newfile').exists())

    def test_restore_rejects_boolean_schema_before_writes(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            command, snapshot = self.child(root / 'parent', root / 'child')
            refs = dict(command.artifact_refs)
            snapshot['schema_version'] = True
            self.set_snapshot(root / 'child', refs, snapshot)
            result = RestoreHarnessHandler()(replace(command, artifact_refs=refs))
            self.assertEqual(result.status, 'blocked', result.detail)
            self.assertFalse((root / 'child/baseline').exists())
