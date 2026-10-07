import json
from pathlib import Path
import shutil
import unittest
from unittest.mock import patch

from test_skill_runtime import SkillRuntimeTests as _RuntimeFixtures
from modport.contracts import OperationInput
from modport.knowledge_library import project_entries
from modport.skill_runtime import skill_lookup, skill_publish, publish_supplement
from modport.skill_tools.scan import knowledge_entries
from modport.skill_tools import bundle


class KnowledgeProjectionTests(unittest.TestCase):
    def test_legacy_composite_gap_has_three_stable_ids(self):
        items = knowledge_entries({'known_gaps': ['menu/input/chat APIs need inspection']})
        self.assertEqual({item['id'] for item in items}, {'gap.menu', 'gap.input', 'gap.chat'})

    def test_project_fields_are_rejected(self):
        with self.assertRaisesRegex(ValueError, 'project fields'):
            project_entries([{'id': 'api.test', 'run_id': 'private'}])


class KnowledgeRuntimeTests(unittest.TestCase):
    setUp = _RuntimeFixtures.setUp
    command = _RuntimeFixtures.command
    write = _RuntimeFixtures.write
    candidate = _RuntimeFixtures.candidate
    publish = _RuntimeFixtures.publish
    java_only = _RuntimeFixtures.java_only

    def test_partial_material_is_existing_and_reviewable(self):
        self.java_only()
        candidate = self.candidate()
        (candidate / 'coverage.json').unlink()
        self.store.mkdir()
        shutil.copytree(candidate, self.store / 'partial')
        result = skill_lookup(self.command())
        self.assertEqual(result.status, 'completed', result.detail)
        self.assertEqual(result.outputs['missing_kinds'], [])
        self.assertEqual(result.outputs['research_origins'], {'java': 'existing'})
        self.assertEqual(result.outputs['needs_review_kinds'], ['java'])
        workspace = self.run / 'workspaces/skills/java'
        self.assertTrue((workspace / 'existing-material/rules.json').exists())
        self.assertTrue((workspace / 'coverage.json').exists())

    def test_partial_envelope_preserves_valid_entries_and_gap_ids(self):
        self.java_only()
        candidate = self.candidate()
        rules = json.loads((candidate / 'rules.json').read_text())
        rules['knowledge_entries'] = [{'id': 'api.preserved', 'summary': 'Existing generic knowledge'}]
        rules['known_gaps'] = [{'id': 'gap.chat', 'summary': 'Unknown chat migration'}]
        self.write(candidate / 'rules.json', rules)
        (candidate / 'coverage.json').unlink()
        self.store.mkdir()
        shutil.copytree(candidate, self.store / 'partial')
        result = skill_lookup(self.command())
        self.assertEqual(result.status, 'completed', result.detail)
        current = json.loads((self.run / 'workspaces/skills/java/rules.json').read_text())
        self.assertEqual(current, rules)
        self.assertEqual({entry['id'] for entry in knowledge_entries(current)},
                         {'api.preserved', 'gap.chat', 'old-api'})

    def test_repeated_partial_materialization_keeps_original_archive(self):
        self.java_only()
        candidate = self.candidate()
        (candidate / 'coverage.json').unlink()
        workspace = self.run / 'workspaces/skills/java'
        workspace.parent.mkdir(parents=True)
        shutil.copytree(candidate, workspace)
        first = skill_lookup(self.command())
        self.assertEqual(first.status, 'completed', first.detail)
        archived = {path.relative_to(workspace / 'existing-material'): path.read_bytes()
                    for path in (workspace / 'existing-material').rglob('*') if path.is_file()}
        (workspace / 'coverage.json').unlink()
        second = skill_lookup(self.command())
        self.assertEqual(second.status, 'completed', second.detail)
        self.assertEqual(second.outputs['needs_review_kinds'], ['java'])
        self.assertTrue((workspace / 'coverage.json').is_file())
        self.assertFalse((workspace / 'existing-material/existing-material').exists())
        for relative, original in archived.items():
            self.assertEqual((workspace / 'existing-material' / relative).read_bytes(), original)

    def test_rules_only_exact_material_is_reused(self):
        self.java_only()
        candidate = self.candidate()
        self.store.mkdir()
        partial = self.store / 'rules-only'
        partial.mkdir()
        shutil.copyfile(candidate / 'rules.json', partial / 'rules.json')
        result = skill_lookup(self.command())
        self.assertEqual(result.status, 'completed', result.detail)
        self.assertEqual(result.outputs['research_origins'], {'java': 'existing'})

    def test_frozen_revision_does_not_follow_new_publications(self):
        self.java_only()
        self.publish()
        first = skill_lookup(self.command())
        self.publish(suffix='newer')
        second = skill_lookup(self.command())
        self.assertEqual(first.outputs['knowledge_revisions'], second.outputs['knowledge_revisions'])

    def test_pair_indexes_isolate_versions_without_inspecting_other_pair(self):
        self.java_only()
        first = self.publish()
        first_identity = {'source': {'java': '17'}, 'target': {'java': '25'}}
        self.request['source_java'] = '21'
        second = self.publish(suffix='second-pair')
        second_identity = {'source': {'java': '21'}, 'target': {'java': '25'}}
        self.assertEqual(bundle.indexed_paths(self.store, 'java', first_identity), [first])
        self.assertEqual(bundle.indexed_paths(self.store, 'java', second_identity), [second])
        inspect = bundle.inspect
        def check(directory, **kwargs):
            self.assertNotEqual(Path(directory), first)
            return inspect(directory, **kwargs)
        with patch('modport.skill_tools.bundle.inspect', side_effect=check):
            result = skill_lookup(self.command())
        self.assertEqual(result.status, 'completed', result.detail)
        self.assertEqual(result.outputs['knowledge_revisions']['java']['source'], {'java': '21'})

    def test_unindexed_legacy_path_remains_selectable(self):
        self.java_only()
        self.publish(suffix='indexed')
        candidate = self.candidate()
        shutil.copytree(candidate, self.store / 'legacy')
        self.request['java_skill_revision'] = 'legacy'
        result = skill_lookup(self.command())
        self.assertEqual(result.status, 'completed', result.detail)
        self.assertEqual(result.outputs['research_origins'], {'java': 'existing'})

    def test_skill_publish_returns_frozen_revision_identity(self):
        self.java_only()
        self.publish()
        found = skill_lookup(self.command())
        result = skill_publish(self.command('skill_publish'))
        self.assertEqual(result.status, 'completed', result.detail)
        self.assertEqual(result.outputs['knowledge_revisions'], found.outputs['knowledge_revisions'])

    def test_exhausted_research_material_requires_host_flag_and_full_budget(self):
        self.java_only()
        def lookup(dispatched):
            return skill_lookup(OperationInput('run', 'task', 'skill_lookup', 'lookup-exhausted',
                str(self.run), payload={'request': self.request, 'allow_empty_research_material': True,
                    'research_budget': {'java': {'limit': 2, 'dispatched': dispatched}}}))
        self.assertEqual(lookup(1).outputs['missing_kinds'], ['java'])
        result = lookup(2)
        self.assertEqual(result.status, 'completed', result.detail)
        self.assertEqual(result.outputs['missing_kinds'], [])
        self.assertEqual(result.outputs['needs_review_kinds'], ['java'])
        self.assertEqual(result.outputs['cached_references'], {})
        workspace = self.run / 'workspaces/skills/java'
        rules = json.loads((workspace / 'rules.json').read_text())
        self.assertEqual(rules['rules'], [])
        self.assertEqual(rules['manual_checks'], [])
        self.assertTrue(rules['known_gaps'])
        self.assertFalse((workspace / 'review.json').exists())

    def test_exhausted_fallback_covers_both_kinds_in_one_lookup(self):
        result = skill_lookup(OperationInput('run', 'task', 'skill_lookup', 'lookup-both',
            str(self.run), payload={'request': self.request, 'allow_empty_research_material': True,
                'research_budget': {kind: {'limit': 2, 'dispatched': 2}
                                    for kind in ('platform', 'java')}}))
        self.assertEqual(result.status, 'completed', result.detail)
        self.assertEqual(result.outputs['missing_kinds'], [])
        self.assertEqual(result.outputs['needs_review_kinds'], ['platform', 'java'])
        for kind in ('platform', 'java'):
            rules = json.loads((self.run / 'workspaces/skills' / kind / 'rules.json').read_text())
            self.assertEqual(rules['known_gaps'][0]['status'], 'unknown')
            self.assertEqual(rules['rules'], [])

    def supplement(self, entries, **kwargs):
        return OperationInput('run', 'task', 'knowledge_publish', 'knowledge-publish-1', str(self.run),
            payload={'request': self.request, 'generic_knowledge_entries': entries}, **kwargs)

    def test_publication_requires_exact_host_review_and_creates_revision(self):
        self.java_only()
        old = self.publish()
        skill_lookup(self.command())
        entries = {'java': [{'id': 'api.generic', 'category': 'api', 'summary': 'API changed',
            'applicability': 'Uses API', 'migration': 'Use replacement', 'compat': 'adapter',
            'verification': 'Inspect locked JDK source', 'evidence': [{'source': 'https://openjdk.org/',
                'locator': 'JDK 25', 'supports': 'API change'}]}]}
        blocked = publish_supplement(self.supplement(entries))
        self.assertEqual(blocked.status, 'blocked')
        review = {'status': 'completed', 'stage_id': 'research_review', 'command_id': 'review-1',
                  'outputs': {'verdict': 'approved', 'reviewer_id': 'independent',
                              'approved_generic_knowledge_entries': entries}}
        result = publish_supplement(self.supplement(entries, upstream_results={'research_review': review}))
        self.assertEqual(result.status, 'completed', result.detail)
        self.assertEqual(result.outputs['published_kinds'], ['java'])
        self.assertTrue((old / 'rules.json').exists())
        self.assertNotIn('knowledge_entries', json.loads((old / 'rules.json').read_text()))
        frozen = json.loads((self.run / 'artifacts/skills/java/rules.json').read_text())
        self.assertEqual(frozen['knowledge_entries'], entries['java'])

    def test_empty_supplement_is_noop(self):
        result = publish_supplement(self.supplement({}))
        self.assertEqual(result.status, 'completed')
        self.assertTrue(result.outputs['noop'])

# unittest must not collect the imported fixture class again.
del _RuntimeFixtures
