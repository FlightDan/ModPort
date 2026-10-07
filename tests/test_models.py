import unittest

from modport import (
    Budget,
    LockedManifest,
    MigrationRequest,
    canonical_json,
    manifest_sha256,
    select_neoforge_candidate,
    select_neoforge_version,
    validate_manifest,
)


class ModelsTests(unittest.TestCase):
    def setUp(self):
        self.request = MigrationRequest(
            mod_id="examplemod",
            source_repository="https://example.invalid/mod.git",
            source_minecraft="1.20.1",
            target_minecraft="1.21.1",
            budget=Budget(max_seconds=20, max_agent_assignments=10),
        )

    def test_request_and_budget_validate(self):
        self.request.validate()
        with self.assertRaises(ValueError):
            Budget(max_seconds=-1).validate()

    def test_local_snapshot_semantics_survive_request_serialization(self):
        from dataclasses import replace
        request = replace(self.request, source_repository='file:///tmp/local-source', source_snapshot=True)
        restored = MigrationRequest.from_mapping(request.to_dict())
        restored.validate()
        self.assertIs(restored.source_snapshot, True)
        self.assertNotIn('source_snapshot', self.request.to_dict())
        with self.assertRaisesRegex(ValueError, 'local file'):
            replace(self.request, source_snapshot=True).validate()
        with self.assertRaisesRegex(ValueError, 'boolean'):
            replace(request, source_snapshot='true').validate()

    def test_validation_scope_round_trips_and_is_migration_only(self):
        from dataclasses import replace
        request = replace(self.request, validation_scope='compile_package')
        self.assertEqual(request, MigrationRequest.from_mapping(request.to_dict()))
        with self.assertRaisesRegex(ValueError, 'validation_scope'):
            replace(request, workflow_mode='skill_generation', skill_kind='java').validate()

    def test_exact_skill_inputs_survive_request_and_manifest_round_trips(self):
        from dataclasses import replace
        request = replace(self.request, source_loader_version="47.0.1", target_loader_version="21.1.77",
            source_java="17", target_java="21", mdk_revision="a" * 40, skill_store="/tmp/skills",
            platform_skill_revision="b" * 64, java_skill_revision="c" * 64, max_parallel_coders=2)
        self.assertEqual(MigrationRequest.from_mapping(request.to_dict()), request)
        manifest = LockedManifest(request, "21.1.77", java_version="21")
        self.assertEqual(LockedManifest.from_mapping(manifest.to_dict()).request, request)
        for value in (0, True, 33):
            with self.assertRaises(ValueError):
                replace(request, max_parallel_coders=value).validate()
        with self.assertRaises(ValueError):
            replace(request, platform_skill_revision="latest").validate()

    def test_budget_uses_independent_rework_and_execution_limits(self):
        budget = Budget()
        self.assertEqual(budget.max_seconds, 43_200)
        self.assertEqual(budget.max_agent_assignments, 40)
        self.assertEqual(budget.max_rework_rounds, 10)
        self.assertEqual(budget.execution_max_attempts, 3)
        self.assertEqual(
            budget.to_dict(),
            {
                "max_seconds": 43_200,
                "max_agent_assignments": 40,
                "max_rework_rounds": 10,
                "execution_max_attempts": 3,
                "max_tokens": None,
            },
        )
        for value in (-1, True, 1.5, None):
            with self.subTest(max_rework_rounds=value), self.assertRaises(ValueError):
                Budget(max_rework_rounds=value).validate()
        for value in (0, -1, True, 1.5, None):
            with self.subTest(execution_max_attempts=value), self.assertRaises(ValueError):
                Budget(execution_max_attempts=value).validate()

    def test_legacy_budget_field_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "max_fixes_per_signature"):
            MigrationRequest.from_mapping(
                {
                    "mod_id": "examplemod",
                    "source_repository": "https://example.invalid/mod.git",
                    "source_minecraft": "1.20.1",
                    "target_minecraft": "1.21.1",
                    "budget": {"max_fixes_per_signature": 2},
                }
            )

    def test_manifest_is_deeply_immutable(self):
        manifest = LockedManifest(
            request=self.request,
            neoforge_version="21.1.77",
            dependencies=({"id": "minecraft", "version": "1.21.1"},),
            files=("src/main/java/Example.java",),
        )
        with self.assertRaises(TypeError):
            manifest.dependencies[0]["id"] = "changed"
        with self.assertRaises(Exception):
            manifest.files += ("other",)

    def test_skill_reference_is_immutable_and_accepts_stale_digest_metadata(self):
        from modport import SkillReference
        from dataclasses import replace
        reference = SkillReference("java", "java-17-to-25", {"java": "17"}, {"java": "25"},
            "a" * 64, "artifacts/skills/java", {"rules.json": "b" * 64},
            {"verdict": "approved", "bundle_sha256": "a" * 64, "reviewer_id": "independent"})
        self.assertEqual(SkillReference.from_mapping(reference.to_dict()), reference)
        with self.assertRaises(TypeError):
            reference.source["java"] = "21"
        # Skill digests are diagnostic metadata; an isolated run may continue
        # after the referenced payload advances.
        replace(reference, bundle_sha256="c" * 64).validate()

    def test_canonical_json_is_order_independent(self):
        self.assertEqual(canonical_json({"b": 2, "a": 1}), '{"a":1,"b":2}')
        first = LockedManifest(self.request, "21.1.77")
        second = LockedManifest(self.request, "21.1.77")
        self.assertEqual(manifest_sha256(first), manifest_sha256(second))

    def test_manifest_hash_is_optional_metadata_not_a_workflow_gate(self):
        base = LockedManifest(self.request, "21.1.77")
        locked = LockedManifest(self.request, "21.1.77", manifest_sha256=manifest_sha256(base))
        validate_manifest(locked)
        restored = LockedManifest.from_dict(locked.to_dict(include_hash=True))
        validate_manifest(restored)
        self.assertEqual(restored, locked)
        updated = LockedManifest(self.request, "21.1.78", manifest_sha256=locked.manifest_sha256)
        validate_manifest(updated)
        self.assertEqual(updated.neoforge_version, "21.1.78")

    def test_manifest_uses_sdk_schema_and_rejects_old_schema(self):
        from modport.sdk_compat import SDK_VERSION
        from modport.workflow import WORKFLOW_VERSION
        manifest = LockedManifest(self.request, "21.1.77", workflow_version=WORKFLOW_VERSION)
        serialized = manifest.to_dict()
        self.assertEqual(manifest.sdk_version, SDK_VERSION)
        self.assertEqual(manifest.workflow_version, WORKFLOW_VERSION)
        self.assertEqual(manifest.schema_version, 2)
        self.assertIn("sdk_version", serialized)
        self.assertNotIn("kernel_version", serialized)
        self.assertEqual(LockedManifest.from_mapping(serialized), manifest)
        historical = dict(serialized, sdk_version="0.5.1")
        self.assertEqual(LockedManifest.from_mapping(historical).sdk_version, "0.5.1")
        unspecified = dict(serialized)
        unspecified.pop("sdk_version")
        with self.assertRaisesRegex(ValueError, "must declare sdk_version"):
            LockedManifest.from_mapping(unspecified)

        old_schema = dict(serialized, schema_version=1)
        with self.assertRaisesRegex(ValueError, "not migrated"):
            LockedManifest.from_mapping(old_schema)
        old_field = dict(serialized)
        old_field.pop("sdk_version")
        old_field["kernel_version"] = "0.2.0"
        with self.assertRaisesRegex(ValueError, "kernel_version"):
            LockedManifest.from_mapping(old_field)

    def test_stable_is_preferred_over_beta(self):
        candidates = [
            {"version": "21.1.90", "channel": "beta"},
            {"version": "21.1.77", "channel": "stable"},
            {"version": "21.1.76", "channel": "stable"},
        ]
        self.assertEqual(select_neoforge_version(candidates), "21.1.77")
        selected = select_neoforge_candidate(candidates)
        self.assertEqual(selected.channel, "stable")

    def test_beta_is_fallback(self):
        candidates = ["21.1.2-beta", "21.1.10-beta"]
        self.assertEqual(select_neoforge_version(candidates), "21.1.10-beta")


if __name__ == "__main__":
    unittest.main()
