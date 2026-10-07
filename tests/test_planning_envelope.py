"""Bounded planning envelopes retain complete host-authenticated reference scope."""
from dataclasses import replace
import json
import unittest
from unittest.mock import patch

from modport import handlers
from modport.planning import PlanningHandler, _context, _upstream
from modport.prompt_compressor import prompt_regions
import test_planning
from test_development import git


class PlanningEnvelopeTests(unittest.TestCase):
    def test_explicit_rework_keeps_original_manifest_and_seals_new_snapshot(self):
        from modport.planning import _store_manifest
        command = self.fixture.command('parallel_review', identifier='rework-manifest')
        original = _store_manifest(command, b'{"snapshot": "before"}')
        refreshed = _store_manifest(command, b'{"snapshot": "after"}', after_rework=True)
        self.assertNotEqual(original['path'], refreshed['path'])
        self.assertEqual(b'{"snapshot": "before"}', (self.fixture.root / original['path']).read_bytes())
        self.assertEqual(refreshed, _store_manifest(command, b'{"snapshot": "after"}', after_rework=True))

    def setUp(self):
        self.fixture = test_planning.PlanningTests('runTest')
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)

    def run_inventory(self, *, mutate=None, selected=None, omit=True, identifier='inventory'):
        fixture = self.fixture
        command = fixture.command('migration_inventory', identifier=identifier)
        def fake(handler, cmd):
            self.prompt = handler.prompt
            expected, _, _ = _context(cmd, fixture.work)
            doc = {**expected, 'base_commit': git(fixture.work, 'rev-parse', 'HEAD'),
                   **fixture.body('migration_inventory')}
            if omit:
                doc.pop('input_refs')
            else:
                doc['input_refs'] = selected
            if mutate:
                mutate(doc, cmd)
            path = fixture.root / 'logs' / (cmd.command_id + '.txt')
            path.write_text(json.dumps(doc))
            return handlers._result(cmd, 'completed', outputs={'last_message': str(path)})
        with patch('modport.handlers.CodexStageHandler.__call__', fake):
            result = PlanningHandler()(command)
        if result.status == 'completed':
            fixture.refs.update(result.outputs['artifact_refs'])
        return result

    def manifest(self, identifier='inventory'):
        return self.fixture.root / 'artifacts' / 'executions' / identifier / 'planning-input-manifest.json'

    def test_thousands_of_parent_aliases_do_not_enter_exact_envelope(self):
        fixture = self.fixture
        for index in range(2300):
            alias = 'parent:' + ('previous-segment:' * 8) + str(index)
            fixture.refs[alias] = dict(fixture.refs['source_evidence'])
        result = self.run_inventory()
        self.assertEqual('completed', result.status, result.detail)
        _, _, protected = prompt_regions(self.prompt)
        self.assertLess(len(protected.encode()), 15000)
        self.assertNotIn('previous-segment:', protected)
        self.assertIn('Planning input manifest', protected)
        self.assertIn('no JSON schema or identity echo is required', protected)
        sealed = json.loads((fixture.root / fixture.refs['migration_inventory']['path']).read_text())
        manifest = json.loads(self.manifest().read_text())
        self.assertEqual(2302, len(manifest['input_bindings']))
        self.assertEqual(set(sealed['input_refs']), set(manifest['input_bindings']))
        self.assertEqual('source_evidence', sealed['input_refs']['source_evidence'])
        contract_ref = manifest['input_bindings']['functional_contract_lock']
        contract = json.loads((fixture.root / contract_ref['path']).read_text())
        self.assertEqual('behavior', contract['contract']['behaviors'][0]['id'])
        _upstream(fixture.command('migration_plan'), 'migration_inventory')

    def test_selected_aliases_are_optional_and_host_seals_complete_scope(self):
        for index, selected in enumerate(([], {}, ['source_evidence'], {'source_evidence': 'source_evidence'})):
            with self.subTest(selected=selected):
                result = self.run_inventory(omit=False, selected=selected, identifier='selected-' + str(index))
                self.assertEqual('completed', result.status, result.detail)
                sealed = json.loads((self.fixture.root / self.fixture.refs['migration_inventory']['path']).read_text())
                self.assertIn('functional_contract_lock', sealed['input_refs'])

    def test_model_selected_references_are_raw_text_and_host_seals_scope(self):
        for index, selected in enumerate((['unknown'], {'source_evidence': 'unknown'},
                                          ['source_evidence', 'source_evidence'], 'source_evidence')):
            result = self.run_inventory(omit=False, selected=selected, identifier='invalid-' + str(index))
            self.assertEqual('completed', result.status, result.detail)
            sealed = json.loads((self.fixture.root / result.outputs['artifact_refs']['migration_inventory']['path']).read_text())
            manifest = json.loads((self.fixture.root / sealed['input_manifest']['path']).read_text())
            self.assertEqual(set(manifest['input_bindings']), set(sealed['input_refs']))
            self.assertIn('source_evidence', sealed['input_refs'])
            self.assertIn('functional_contract_lock', sealed['input_refs'])
            self.assertEqual(selected, json.loads(sealed['raw_report'])['input_refs'])
        result = self.run_inventory(mutate=lambda doc, cmd: doc['issues'][0].update(evidence_refs=['unknown']))
        self.assertEqual('completed', result.status, result.detail)
        sealed = json.loads((self.fixture.root / result.outputs['artifact_refs']['migration_inventory']['path']).read_text())
        self.assertEqual(['unknown'], json.loads(sealed['raw_report'])['issues'][0]['evidence_refs'])
        self.assertNotIn('unknown', sealed['input_refs'])

    def test_tampered_manifest_is_rejected_before_response_sealing(self):
        result = self.run_inventory(mutate=lambda doc, cmd: self.manifest().write_text('{}'))
        self.assertEqual('planning_artifact_invalid', result.error_code, result.detail)
        self.assertIn('manifest changed', result.detail)
        self.assertFalse((self.manifest().parent / 'migration_inventory.json').exists())

    def test_model_manifest_field_is_ignored_and_host_seals_current_manifest(self):
        result = self.run_inventory(identifier='previous')
        self.assertEqual('completed', result.status, result.detail)
        prior = json.loads((self.fixture.root / self.fixture.refs['migration_inventory']['path']).read_text())
        result = self.run_inventory(mutate=lambda doc, cmd: doc.update(input_manifest=prior['input_manifest']))
        self.assertEqual('completed', result.status, result.detail)
        sealed = json.loads((self.fixture.root / result.outputs['artifact_refs']['migration_inventory']['path']).read_text())
        self.assertEqual(str(self.manifest().relative_to(self.fixture.root)),
                         sealed['input_manifest']['path'])
        self.assertEqual(prior['input_manifest'], json.loads(sealed['raw_report'])['input_manifest'])

    def test_upstream_manifest_tamper_is_rejected(self):
        result = self.run_inventory()
        self.assertEqual('completed', result.status, result.detail)
        self.manifest().write_text('{}')
        with self.assertRaisesRegex(ValueError, 'manifest changed'):
            _upstream(self.fixture.command('migration_plan'), 'migration_inventory')

    def test_upstream_rejects_manifest_from_another_execution(self):
        self.assertEqual('completed', self.run_inventory(identifier='previous').status)
        prior = json.loads((self.fixture.root / self.fixture.refs['migration_inventory']['path']).read_text())
        self.assertEqual('completed', self.run_inventory().status)
        path = self.fixture.root / self.fixture.refs['migration_inventory']['path']
        doc = json.loads(path.read_text())
        doc['input_manifest'] = prior['input_manifest']
        path.write_text(json.dumps(doc))
        with self.assertRaisesRegex(ValueError, 'development artifact digest mismatch'):
            _upstream(self.fixture.command('migration_plan'), 'migration_inventory')

    def test_reference_scope_snapshot_is_stable_during_execution(self):
        original = dict(self.fixture.refs['source_evidence'])
        def mutate(doc, cmd):
            cmd.artifact_refs['source_evidence'] = dict(cmd.artifact_refs['source_evidence'], metadata={'changed': True})
        result = self.run_inventory(mutate=mutate)
        self.assertEqual('completed', result.status, result.detail)
        manifest = json.loads(self.manifest().read_text())
        self.assertEqual(original, manifest['input_bindings']['source_evidence'])
        sealed = json.loads((self.fixture.root / result.outputs['artifact_refs']['migration_inventory']['path']).read_text())
        self.assertEqual('source_evidence', sealed['input_refs']['source_evidence'])

    def test_repair_catalog_projection_preserves_complete_manifest_and_reasoning(self):
        fixture = self.fixture
        fixture.repair_payload('target_diagnose')
        aliases = {'parent:history:' + str(index): dict(fixture.refs['source_evidence'])
                   for index in range(64)}
        fixture.refs.update(aliases)
        fixture.repair_context['artifact_refs'].update(aliases)
        fixture.repair_context['prior_attempts'].append({'artifact_refs': aliases,
            'objective': 'Keep cache invalidation on reload', 'acceptance': ['Pass frozen characterization']})
        result = fixture.repair_round('target_diagnose', mutation=lambda doc: doc.pop('input_refs'))
        self.assertEqual('completed', result.status, result.detail)
        _, historical, protected = prompt_regions(fixture.repair_prompts['target_diagnose'])
        self.assertNotIn('parent:history:', historical + protected)
        self.assertIn('Keep cache invalidation on reload', historical)
        self.assertIn('Pass frozen characterization', historical)
        manifest = json.loads(self.manifest('target_diagnose').read_text())
        self.assertEqual(fixture.repair_context, manifest['repair_context'])
        sealed = json.loads((fixture.root / fixture.refs['target_diagnose']['path']).read_text())
        self.assertEqual(manifest['input_bindings'], sealed['input_bindings'])
        command = replace(fixture.command('target_repair_plan'), payload=fixture.repair_payload('target_repair_plan'))
        _upstream(command, 'target_diagnose')
