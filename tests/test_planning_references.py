"""Authenticated history references remain usable without inline expansion."""
import copy
from hashlib import sha256
import json
from pathlib import Path
import tempfile
import unittest

from modport.operations import MigrationOperations
from modport.planning_references import archive_context, context_closure, read_context, reference_history


class PlanningReferenceTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def test_manifest_delivers_semantic_feedback_separately_from_original_failure(self):
        from unittest.mock import patch
        from modport.contracts import OperationInput
        from modport.planning import _manifest_document
        original = {'failure_execution_id': 'build-failed', 'current_failure': {'detail': 'build error'}}
        feedback = [{'execution_id': 'rejected-tasks', 'result': {
            'error_code': 'planning_handoff_conflict', 'outputs': {
                'handoff_conflict': {'explanation': 'Host prerequisite is not a coder task'}}}}]
        for version in (11, 12):
            command = OperationInput('run', 'task', 'target_repair_plan', 'plan', str(self.root),
                payload={'repair_feedback': feedback}, options={'workflow_version': version})
            with patch('modport.planning._repair_round', return_value=True), \
                 patch('modport.planning._repair_context', return_value=original):
                manifest = _manifest_document(command, {'input_refs': {}}, [])
            if version == 12:
                self.assertEqual(feedback, read_context(self.root, manifest['repair_feedback']))
                self.assertEqual(original, read_context(self.root, manifest['repair_context']))
                self.assertIn(manifest['repair_feedback']['source_ref'], context_closure(self.root, manifest))
            else:
                self.assertEqual(feedback, manifest['repair_feedback'])
                self.assertEqual(original, manifest['repair_context'])

    def test_archive_reuses_canonical_bytes_and_deduplicates_transitive_dependencies(self):
        evidence = self.root / 'evidence.txt'
        evidence.write_text('original failure')
        ref = {'path': 'evidence.txt', 'sha256': sha256(evidence.read_bytes()).hexdigest()}
        nested = archive_context(self.root, {'first': ref, 'second': ref})
        binding = archive_context(self.root, {'nested': nested, 'same_evidence': ref})
        before = (self.root / binding['source_ref']['path']).stat().st_mtime_ns
        reused = archive_context(self.root, {'same_evidence': ref, 'nested': nested})
        self.assertEqual(binding, reused)
        self.assertEqual(before, (self.root / binding['source_ref']['path']).stat().st_mtime_ns)
        self.assertEqual({ref['path'], nested['source_ref']['path']},
                         {item['path'] for item in binding['referenced_artifacts']})
        self.assertEqual(2, len(binding['referenced_artifacts']))
        self.assertEqual({'first': ref, 'second': ref}, read_context(self.root, nested))

    def test_duplicate_reference_metadata_selection_is_canonical(self):
        first = {'path': 'evidence.txt', 'sha256': 'a' * 64, 'metadata': {'alias': 'first'}}
        second = {**first, 'metadata': {'alias': 'second'}}
        binding = archive_context(self.root, {'first': first, 'second': second})
        reordered = archive_context(self.root, {'second': second, 'first': first})
        self.assertEqual(binding, reordered)
        self.assertEqual(1, len(binding['referenced_artifacts']))

    def test_archive_tampering_and_invalid_bindings_are_rejected(self):
        binding = archive_context(self.root, {'detail': 'authenticated'})
        for mutate in (
            lambda value: value.update(json_pointer='/detail'),
            lambda value: value.pop('referenced_artifacts'),
            lambda value: value['source_ref']['metadata'].clear(),
        ):
            invalid = copy.deepcopy(binding)
            mutate(invalid)
            with self.assertRaises(ValueError):
                read_context(self.root, invalid)
        (self.root / binding['source_ref']['path']).write_text('{"detail":"changed"}')
        with self.assertRaises(ValueError):
            read_context(self.root, binding)
        with self.assertRaises(ValueError):
            archive_context(self.root, {'detail': 'authenticated'})

    def test_history_index_preserves_current_failure_and_complete_original_history(self):
        context = {'current_failure': {'execution_id': 'current', 'result': {'detail': 'current error'}},
                   'prior_attempts': [{'run_id': 'parent', 'failure_execution_id': 'old',
                                       'result': {'detail': 'old detail ' * 10000}}],
                   'prior_findings': [{'detail': 'old finding'}], 'failure_input': {'payload': 'input'},
                   'upstream_results': {'build': {'status': 'failed'}}, 'parent_context': {'run_id': 'parent'},
                   'artifact_refs': {}, 'request': {'mod_id': 'example'}}
        original = copy.deepcopy(context)
        compact = reference_history(self.root, context)
        self.assertEqual(original, context)
        self.assertEqual(context['current_failure'], compact['current_failure'])
        self.assertEqual('/prior_attempts/0', compact['prior_attempts'][0]['history_pointer'])
        self.assertNotIn('old detail', json.dumps(compact))
        archived = read_context(self.root, compact['history_source'])
        for key in ('prior_attempts', 'prior_findings', 'failure_input', 'upstream_results', 'parent_context'):
            self.assertEqual(original[key], archived[key])

    def test_parent_remapping_preserves_archive_bytes_and_nested_evidence_closure(self):
        evidence = self.root / 'original.txt'
        evidence.write_text('original evidence')
        ref = {'path': evidence.name, 'sha256': sha256(evidence.read_bytes()).hexdigest()}
        history = archive_context(self.root, {'old_result': {'evidence': ref}})
        context = archive_context(self.root, {'history_source': history})
        sources = {str(index): value for index, value in enumerate(context_closure(self.root, context))}
        child = self.root / 'child'
        child.mkdir()
        copied = {}
        for key, value in sources.items():
            target = child / 'artifacts/parent-evidence' / value['path']
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes((self.root / value['path']).read_bytes())
            copied[key] = {**value, 'path': target.relative_to(child).as_posix()}
        packet = {'source_refs': sources, 'copied_evidence': copied, 'repair_context': context}
        packet_path = child / 'packet.json'
        packet_path.write_text(json.dumps(packet))
        parent = MigrationOperations._parent_repair_context({
            'parent_run_id': 'parent', 'run_dir': str(child), 'definition': {'workflow_version': 12},
            'initial_refs': {'failure_packet': {'path': 'packet.json'}}})
        parent_data = read_context(child, parent)
        restored = read_context(child, parent_data['repair_context'])
        paths = parent_data['evidence_path_map']
        nested = copy.deepcopy(restored['history_source'])
        nested['source_ref']['path'] = paths[nested['source_ref']['path']]
        restored_history = read_context(child, nested)
        old_ref = restored_history['old_result']['evidence']
        self.assertEqual(b'original evidence', (child / paths[old_ref['path']]).read_bytes())
        self.assertEqual((self.root / history['source_ref']['path']).read_bytes(),
                         (child / nested['source_ref']['path']).read_bytes())
        self.assertTrue(set(paths.values()).issubset({value['path'] for value in context_closure(child, parent)}))
