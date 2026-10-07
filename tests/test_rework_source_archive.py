"""Author tools survive repeated continuations and legacy catalog gaps."""
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from modport.continuation import _publish_rework_sources
from modport.contracts import OperationInput, OperationResult
from modport.evidence import atomic_json, file_digest, read_json
from modport.rework_source_archive import inherited_rework_sources, SOURCE_KEY
from modport.rework_tools import prepare_session, rework_targets, opencode_tool_config


class ReworkSourceArchiveTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.options = {'workflow_version': 17, 'business_gates_disabled': True}

    def operation(self, task, stage, *, refs=None, payload=None):
        command = OperationInput('logical', task, stage, task + ':1', str(self.root),
            options=self.options, artifact_refs=refs or {}, payload=payload or {})
        result = OperationResult('failed', 'logical', task, stage, command.command_id,
                                 error_code='observed_failure')
        return command, result

    def state(self, segment, *pairs):
        return {'run_id': segment, 'revision': 7, 'generation': 0, 'tasks': {
            command.task_id: {'attempts': [{'state': 'succeeded',
                'command': {'execution_id': command.command_id, 'payload': command.to_dict()},
                'result': {'value': result.to_dict()}}]}
            for command, result in pairs}}

    def header(self, segment, previous=None, ref=None):
        header = {'run_id': segment, 'logical_run_id': 'logical'}
        if ref is not None:
            header['continuation'] = {'previous_run_id': previous,
                                      'support_refs': {SOURCE_KEY: ref}}
        return header

    def publish(self, state, next_segment, pairs, *, header=None):
        effective = {command.task_id: result.to_dict() for command, result in pairs}
        app = {'effective': effective,
               'continuation_feedback': {'gate_failure': pairs[-1][1].to_dict()}}
        return _publish_rework_sources(self.root, state, app, next_segment, source_header=header)

    def first_catalog(self):
        coder = self.operation('coder.one', 'coder', payload={
            'development_task': {'id': 'one', 'objective': 'Repair conflicting changes'}})
        ref = self.publish(self.state('one', coder), 'two', [coder])
        return coder, ref

    def test_repeated_continuation_exposes_real_coder_tool_and_keeps_failure(self):
        coder, first_ref = self.first_catalog()
        build = self.operation('target_build', 'target_build', refs={SOURCE_KEY: first_ref})
        header = self.header('two', 'one', first_ref)
        next_ref = self.publish(self.state('two', build), 'three', [coder, build], header=header)
        next_header = self.header('three', 'two', next_ref)
        atomic_json(self.root / 'run.json', next_header)
        review = OperationInput('logical', 'code_review', 'code_review', 'review:1', str(self.root),
            options=self.options, artifact_refs={SOURCE_KEY: next_ref},
            upstream_results={c.task_id: r.to_dict() for c, r in (coder, build)})
        targets = rework_targets({'run_id': 'three', 'tasks': {}}, review)
        self.assertEqual(['coder.one'], [row['target_agent'] for row in targets])
        self.assertEqual('failed', review.upstream_results['coder.one']['status'])
        workspace = self.root / 'worktree'
        workspace.mkdir()
        session = prepare_session(replace(review, payload={'review_rework_targets': targets}), workspace, 30)
        self.assertEqual(targets, read_json(session)['targets'])
        config = opencode_tool_config(session, 30)['modport_rework']
        self.assertEqual('local', config['type'])
        self.assertTrue(config['enabled'])
        self.assertEqual(30000, config['timeout'])
        self.assertIn('modport.rework_mcp', ' '.join(config['command']))

    def test_legacy_dropped_authors_are_recovered_only_through_sealed_links(self):
        coder, first_ref = self.first_catalog()
        build = self.operation('target_build', 'target_build', refs={SOURCE_KEY: first_ref})
        # Reproduce the old publisher: it only carried its local SDK attempt.
        old_ref = self.publish(self.state('two', build), 'three', [coder, build])
        old_digest = file_digest(self.root / old_ref['path'])
        self.assertEqual(['target_build'], [r['stage_id'] for r in read_json(self.root / old_ref['path'])['sources']])
        check = self.operation('target_build_again', 'target_build', refs={SOURCE_KEY: old_ref})
        new_ref = self.publish(self.state('three', check), 'four', [coder, build, check],
                               header=self.header('three', 'two', old_ref))
        rows = read_json(self.root / new_ref['path'])['sources']
        self.assertEqual({c.command_id for c, _ in (coder, build, check)},
                         {r['execution_id'] for r in rows})
        self.assertEqual(old_digest, file_digest(self.root / old_ref['path']))

    def test_unneeded_historical_descriptors_are_not_copied(self):
        coder = self.operation('coder.one', 'coder')
        unused = self.operation('coder.unused', 'coder')
        ref = self.publish(self.state('one', coder, unused), 'two', [coder, unused])
        next_ref = self.publish(self.state('two'), 'three', [coder], header=self.header('two', 'one', ref))
        self.assertEqual([coder[0].command_id],
            [row['execution_id'] for row in read_json(self.root / next_ref['path'])['sources']])

    def test_tampered_ancestor_is_unavailable_and_diagnostic(self):
        coder, ref = self.first_catalog()
        (self.root / ref['path']).write_text('{}')
        next_ref = self.publish(self.state('two'), 'three', [coder], header=self.header('two', 'one', ref))
        document = read_json(self.root / next_ref['path'])
        self.assertEqual([], document['sources'])
        self.assertIn('digest differs', document['source_diagnostics'][0]['detail'])

    def test_foreign_logical_author_is_not_rebound_to_current_run(self):
        coder, ref = self.first_catalog()
        document = read_json(self.root / ref['path'])
        row = document['sources'][0]
        row['operation']['run_id'] = row['result_identity']['run_id'] = 'foreign'
        atomic_json(self.root / ref['path'], document)
        ref = {**ref, 'sha256': file_digest(self.root / ref['path'])}
        sources, diagnostics = inherited_rework_sources(self.root, self.state('two'),
            self.header('two', 'one', ref), wanted={coder[0].command_id})
        self.assertEqual({}, sources)
        self.assertIn('operation identity differs', diagnostics[0]['detail'])

    def test_conflicting_descriptors_are_not_selected_arbitrarily(self):
        coder, ref = self.first_catalog()
        changed = (replace(coder[0], payload={'different': True}), coder[1])
        bridge = self.operation('bridge', 'target_build', refs={SOURCE_KEY: ref})
        newest = self.publish(self.state('two', changed, bridge), 'three', [changed, bridge])
        sources, diagnostics = inherited_rework_sources(self.root, self.state('three'),
            self.header('three', 'two', newest), wanted={coder[0].command_id, 'not-present'})
        self.assertNotIn(coder[0].command_id, sources)
        self.assertTrue(any('conflicting' in row['detail'] for row in diagnostics))

    def test_rework_child_retains_original_author_across_segments(self):
        coder, ref = self.first_catalog()
        child = self.operation('agent_rework.one', 'coder', refs={SOURCE_KEY: ref}, payload={
            'reviewer_rework': {'source_execution_id': coder[0].command_id}})
        next_ref = self.publish(self.state('two', child), 'three', [child],
                               header=self.header('two', 'one', ref))
        self.assertEqual({coder[0].command_id, child[0].command_id},
                         {r['execution_id'] for r in read_json(self.root / next_ref['path'])['sources']})

    def test_orphaned_child_does_not_hide_other_available_authors(self):
        coder = self.operation('coder.one', 'coder')
        orphan = self.operation('orphan', 'coder', payload={
            'reviewer_rework': {'source_execution_id': 'absent'}})
        ref = self.publish(self.state('one', coder, orphan), 'two', [coder, orphan])
        document = read_json(self.root / ref['path'])
        self.assertEqual([coder[0].command_id], [r['execution_id'] for r in document['sources']])
        self.assertIn('original rework author', document['source_diagnostics'][0]['detail'])

    def test_snapshot_and_header_must_refer_to_same_segment(self):
        with self.assertRaisesRegex(ValueError, 'does not match'):
            inherited_rework_sources(self.root, self.state('one'), self.header('two'))


if __name__ == '__main__':
    unittest.main()
