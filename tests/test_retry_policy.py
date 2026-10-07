from dataclasses import replace
import json
from pathlib import Path
import tempfile
import unittest

from modport.evidence import atomic_json, seal_ref, file_digest
from modport.models import MigrationRequest
from modport.retry_policy import apply_budget_overrides, validate_retry_request, build_harness_snapshot, is_harness_source, is_harness_runtime_output


class RetryPolicyTests(unittest.TestCase):
    def setUp(self):
        self.parent = MigrationRequest('example', 'https://example.invalid/mod', '1.20.1', '1.21.1', source_revision='a' * 40)

    def test_default_preserves_every_input(self):
        self.assertEqual(apply_budget_overrides(self.parent), self.parent)
        self.assertEqual(validate_retry_request(self.parent.to_dict(), self.parent)['changes'], {})

    def test_explicit_dependency_cache_override_is_audited_without_source_changes(self):
        child = replace(self.parent, dependency_cache="/tmp/verified-dependencies")
        with self.assertRaises(ValueError):
            validate_retry_request(self.parent, child)
        audit = validate_retry_request(self.parent, child, dependency_cache=child.dependency_cache)
        self.assertEqual(audit["dependency_cache_override"], {"before": None, "after": child.dependency_cache})
        self.assertIsNone(self.parent.dependency_cache)
        with self.assertRaises(ValueError):
            validate_retry_request(self.parent, replace(child, source_revision="b" * 40),
                                   dependency_cache=child.dependency_cache)

    def test_override_audit_and_parent_immutable(self):
        child = apply_budget_overrides(self.parent, {'max_seconds': None, 'max_agent_assignments': 100}, 'More time')
        audit = validate_retry_request(self.parent, child, {'max_seconds': None, 'max_agent_assignments': 100}, 'More time')
        self.assertEqual(audit['changes']['max_seconds'], {'before': 43200, 'after': None})
        self.assertEqual(self.parent.budget.max_agent_assignments, 40)

    def test_rejects_types_unknown_fields_and_missing_reason(self):
        for overrides in ({'max_seconds': True}, {'max_seconds': '40'}, {'max_seconds': 1.0},
                          {'max_seconds': -1}, {'execution_max_attempts': 0}, {'max_rework_rounds': None},
                          {'max_parallel_coders': 10}, {'max_agent_assignments': 41}):
            with self.subTest(overrides=overrides), self.assertRaises(ValueError):
                apply_budget_overrides(self.parent, overrides)
        for reason in ('', '  ', 1):
            with self.assertRaises(ValueError):
                apply_budget_overrides(self.parent, {'max_seconds': 50}, reason)

    def test_rejects_nonbudget_changes_and_mutable_revision(self):
        for name, value in [('source_revision', 'b' * 40), ('target_minecraft', '1.21.2'), ('max_parallel_coders', 2)]:
            with self.assertRaises(ValueError):
                validate_retry_request(self.parent, replace(self.parent, **{name: value}))
        with self.assertRaises(ValueError):
            apply_budget_overrides(replace(self.parent, source_revision='main'))
        with self.assertRaises(ValueError):
            validate_retry_request(self.parent, {**self.parent.to_dict(), 'contract': 'changed'})

    def fixture(self, root):
        files = {}
        for name, data in {'.modport/functional-contract.json': '{}', '.modport/characterization/src/Test.java': 'class Test {}', '.modport/init.gradle': '// init'}.items():
            path = root / 'baseline' / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(data)
            files[name] = seal_ref(root, {'path': 'baseline/' + name, 'sha256': file_digest(path)}, execution_id='draft')
        manifest = {'schema_version': 1, 'source_commit': 'a' * 40, 'candidate_sha256': files['.modport/functional-contract.json']['sha256'], 'files': files}
        path = root / 'artifacts' / 'snapshot.json'
        atomic_json(path, manifest)
        return {'baseline_harness_snapshot': seal_ref(root, {'path': 'artifacts/snapshot.json', 'sha256': file_digest(path)}, execution_id='draft')}

    def test_complete_snapshot_does_not_require_passed_validation(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            refs = self.fixture(root)
            output = root / 'baseline/.modport/run-client/worlds/data'
            output.parent.mkdir(parents=True)
            output.write_text('runtime')
            snapshot = build_harness_snapshot(root, refs, parent_run_id='parent', source_commit='a' * 40)
            self.assertEqual(len(snapshot['files']), 3)
            self.assertTrue(snapshot['requires_fresh_verification'])
            self.assertTrue(all(item['ref']['path'].startswith('baseline/') for item in snapshot['files']))

    def test_current_harness_changes_are_inherited_without_archive_comparison(self):
        for change in ('stale', 'missing', 'extra'):
            with self.subTest(change=change), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                refs = self.fixture(root)
                source = root / 'baseline/.modport/init.gradle'
                if change == 'stale':
                    source.write_text('modified')
                elif change == 'missing':
                    source.unlink()
                elif change == 'extra':
                    (source.parent / 'unarchived.gradle').write_text('unarchived')
                snapshot = build_harness_snapshot(root, refs, parent_run_id='parent', source_commit='a' * 40)
                paths = {item['path'] for item in snapshot['files']}
                self.assertIn('.modport/functional-contract.json', paths)
                if change == 'extra':
                    self.assertIn('.modport/unarchived.gradle', paths)
                if change == 'missing':
                    self.assertNotIn('.modport/init.gradle', paths)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            refs = self.fixture(root)
            source = root / 'baseline/.modport/init.gradle'
            source.unlink()
            source.symlink_to(root / 'artifacts/snapshot.json')
            with self.assertRaises(ValueError):
                build_harness_snapshot(root, refs, parent_run_id='parent', source_commit='a' * 40)

    def test_output_exclusions_preserve_source_package_named_build(self):
        self.assertFalse(is_harness_source('.modport/characterization/build/classes/A.class'))
        self.assertFalse(is_harness_source('.modport/run-server/world/level.dat'))
        self.assertTrue(is_harness_source('.modport/characterization/src/main/java/example/build/A.java'))

    def test_old_snapshot_metadata_and_source_revision_do_not_block_current_harness(self):
        for relative in ('../outside', '.modport/../outside', '/tmp/escape', '.modport/.git/config', '.modport/evidence/pass.json'):
            with self.subTest(relative=relative), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                refs = self.fixture(root)
                manifest = json.loads((root / 'artifacts/snapshot.json').read_text())
                item = dict(manifest['files']['.modport/init.gradle'])
                item['metadata'] = {**item['metadata'], 'source_path': 'baseline/' + relative}
                manifest['files'][relative] = item
                path = root / 'artifacts/unsafe.json'
                atomic_json(path, manifest)
                refs['baseline_harness_snapshot'] = seal_ref(root, {'path': 'artifacts/unsafe.json', 'sha256': file_digest(path)}, execution_id='draft')
                snapshot = build_harness_snapshot(root, refs, parent_run_id='parent', source_commit='a' * 40)
                self.assertEqual(len(snapshot['files']), 3)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            refs = self.fixture(root)
            snapshot = build_harness_snapshot(root, refs, parent_run_id='parent', source_commit='b' * 40)
            self.assertEqual(snapshot['source_commit'], 'b' * 40)


    def test_nested_runtime_output_and_executable_suffixes_are_excluded(self):
        outputs = (
            '.modport/characterization/run/saves/world/level.dat',
            '.modport/client/build/classes/Test.class',
            '.modport/characterization/logs/latest.log',
            '.modport/nested/client/run-client/worlds/data',
            '.modport/client/build/generated/Test.java',
            '.modport/characterization/run/Test.groovy',
            '.modport/client/.gradle/state.bin',
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            refs = self.fixture(root)
            for relative in outputs:
                with self.subTest(relative=relative):
                    self.assertTrue(is_harness_runtime_output(relative))
                    self.assertFalse(is_harness_source(relative))
                path = root / 'baseline' / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text('generated')
            snapshot = build_harness_snapshot(root, refs, parent_run_id='parent', source_commit='a' * 40)
            self.assertEqual(len(snapshot['files']), 3)

    def test_runtime_filter_preserves_host_documents_evidence_and_source_packages(self):
        for relative in (
            '.modport/characterization/src/main/java/p/build/Test.java',
            '.modport/client/src/test/groovy/p/run/Test.groovy',
            '.modport/characterization/src/main/java/p/logs/Test.java',
        ):
            with self.subTest(relative=relative):
                self.assertTrue(is_harness_source(relative))
                self.assertFalse(is_harness_runtime_output(relative))
        for relative in ('.modport/background.md', '.modport/gap-research/report.json',
                         '.modport/evidence/Test.java', '.modport/evidence/pass.json'):
            with self.subTest(relative=relative):
                self.assertFalse(is_harness_source(relative))
                self.assertFalse(is_harness_runtime_output(relative))
        self.assertFalse(is_harness_runtime_output('src/main/java/p/build/Test.java'))

    def test_manifest_runtime_java_sources_are_still_excluded_from_current_harness(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            refs = self.fixture(root)
            manifest = json.loads((root / 'artifacts/snapshot.json').read_text())
            relative = '.modport/client/build/generated/Test.java'
            path = root / 'baseline' / relative
            path.parent.mkdir(parents=True)
            path.write_text('class Test {}')
            manifest['files'][relative] = seal_ref(root, {'path': 'baseline/' + relative,
                'sha256': file_digest(path)}, execution_id='draft')
            manifest_path = root / 'artifacts/runtime-snapshot.json'
            atomic_json(manifest_path, manifest)
            refs['baseline_harness_snapshot'] = seal_ref(root, {'path': 'artifacts/runtime-snapshot.json',
                'sha256': file_digest(manifest_path)}, execution_id='draft')
            snapshot = build_harness_snapshot(root, refs, parent_run_id='parent', source_commit='a' * 40)
            self.assertNotIn(relative, {item['path'] for item in snapshot['files']})
