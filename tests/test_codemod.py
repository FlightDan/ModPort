import json
from dataclasses import replace
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from modport.codemod import (
    CodemodConflictError,
    CodemodRule,
    CodemodSecurityError,
    FORGE_1_20_1,
    NEOFORGE_26_1_2,
    RuleEvidence,
    StaleCodemodPlanError,
    VersionIdentity,
    apply_codemod,
    eventbus_import_rules,
    plan_codemod,
)


FIXTURES = Path(__file__).parent / "fixtures" / "codemod"


class CodemodTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name) / "candidate"
        shutil.copytree(FIXTURES / "multimodule", self.root)

    def tearDown(self):
        self.temporary.cleanup()

    @staticmethod
    def evidence():
        return (
            RuleEvidence(
                source="https://example.invalid/pinned-evidence",
                locator="exact fixture",
                supports="the fixture mapping only",
            ),
        )

    def test_builtin_rules_default_to_detect_only_and_report_evidence(self):
        source = self.root / "module-a/src/main/java/example/Primary.java"
        before = source.read_bytes()

        plan = plan_codemod(
            self.root,
            source_identity=FORGE_1_20_1,
            target_identity=NEOFORGE_26_1_2,
        )
        report = plan.to_dict()

        self.assertEqual([], report["changes"])
        self.assertEqual(3, len(report["rules"]))
        self.assertTrue(all(rule["mode"] == "detect-only" for rule in report["rules"]))
        self.assertTrue(all(rule["evidence"] for rule in report["rules"]))
        self.assertEqual(3, sum(item["occurrences"] for item in report["skipped"]
                                if item["reason"] == "detect_only"))
        result = apply_codemod(self.root, plan)
        self.assertEqual(0, result.applied_changes)
        self.assertEqual(before, source.read_bytes())

    def test_v20_audited_imports_preserve_comments_and_are_idempotent(self):
        from modport.codemod import audited_import_rules
        rules = audited_import_rules(mode='transform')
        self.assertEqual(8, len(rules))
        path = self.root / 'Audited.java'
        path.write_text('\n'.join('import ' + rule.source_value + ';' for rule in rules)
                        + '\n// import net.minecraftforge.fml.common.Mod;\n')
        plan = plan_codemod(self.root, source_identity=FORGE_1_20_1,
                            target_identity=NEOFORGE_26_1_2, rules=rules)
        apply_codemod(self.root, plan)
        for rule in rules:
            self.assertIn('import ' + rule.target_value + ';', path.read_text())
        self.assertIn('// import net.minecraftforge.fml.common.Mod;', path.read_text())
        again = plan_codemod(self.root, source_identity=FORGE_1_20_1,
                             target_identity=NEOFORGE_26_1_2, rules=rules)
        self.assertEqual((), again.changes)

    def test_explicit_eventbus_transform_is_exact_multimodule_and_idempotent(self):
        plan = plan_codemod(
            self.root,
            source_identity=FORGE_1_20_1,
            target_identity=NEOFORGE_26_1_2,
            rules=eventbus_import_rules(mode="transform"),
        )

        self.assertEqual(2, len(plan.changes))
        self.assertEqual(64, len(plan.input_sha256))
        self.assertIn("net.neoforged.bus.api.SubscribeEvent", plan.patch)
        self.assertIn("net.neoforged.bus.api.IEventBus", plan.patch)
        self.assertTrue(all(change.before_sha256 != change.after_sha256
                            for change in plan.changes))

        result = apply_codemod(self.root, plan)
        self.assertEqual(2, result.applied_changes)
        primary = (self.root / "module-a/src/main/java/example/Primary.java").read_text()
        secondary = (self.root / "module-b/src/main/java/example/Secondary.java").read_text()
        self.assertIn("import net.neoforged.bus.api.SubscribeEvent;", primary)
        self.assertIn("import net.neoforged.bus.api.EventPriority;", primary)
        self.assertIn("import net.neoforged.bus.api.IEventBus;", secondary)
        self.assertIn("// import net.minecraftforge.eventbus.api.IEventBus;", primary)
        self.assertIn('"import net.minecraftforge.eventbus.api.SubscribeEvent;"', primary)
        self.assertIn("import net.minecraftforge.eventbus.api.EventPriority;\n            \"\"\"", primary)
        self.assertIn("/* import net.minecraftforge.eventbus.api.SubscribeEvent; */", secondary)
        self.assertIn('"net.minecraftforge.eventbus.api.IEventBus"', secondary)

        second = plan_codemod(
            self.root,
            source_identity=FORGE_1_20_1,
            target_identity=NEOFORGE_26_1_2,
            rules=eventbus_import_rules(mode="transform"),
        )
        self.assertEqual((), second.changes)
        self.assertTrue(all(item.reason == "source_not_found" for item in second.skipped))

    def test_identity_mismatch_never_writes(self):
        plan = plan_codemod(
            self.root,
            source_identity=VersionIdentity("1.20.2", "forge", "48.0.0"),
            target_identity=NEOFORGE_26_1_2,
            rules=eventbus_import_rules(mode="transform"),
        )

        self.assertEqual((), plan.changes)
        self.assertEqual(3, len(plan.skipped))
        self.assertTrue(all(item.reason == "identity_mismatch" for item in plan.skipped))

    def test_version_identity_rejects_ranges_wildcards_and_aliases(self):
        invalid_versions = (
            "47.+",
            "[47.0.1,48.0.0)",
            "(47.0.1,48.0.0]",
            "47.*",
            "latest",
        )
        for version in invalid_versions:
            with self.subTest(version=version):
                with self.assertRaisesRegex(ValueError, "exact|range"):
                    VersionIdentity("1.20.1", "forge", version)

    def test_explicit_resource_rename_rejects_conflicts_and_is_idempotent(self):
        source = "module-a/src/main/resources/data/forge/tags/blocks/example.json"
        target = "module-a/src/main/resources/data/c/tags/blocks/example.json"
        rule = CodemodRule(
            rule_id="rename-one-resource",
            kind="path_rename",
            source_identity=FORGE_1_20_1,
            target_identity=NEOFORGE_26_1_2,
            source_value=source,
            target_value=target,
            evidence=self.evidence(),
            mode="transform",
        )
        plan = plan_codemod(
            self.root,
            source_identity=FORGE_1_20_1,
            target_identity=NEOFORGE_26_1_2,
            rules=(rule,),
        )
        self.assertEqual("rename", plan.changes[0].kind)
        self.assertIn("rename from " + source, plan.patch)
        apply_codemod(self.root, plan)
        self.assertFalse((self.root / source).exists())
        self.assertTrue((self.root / target).is_file())

        repeated = plan_codemod(
            self.root,
            source_identity=FORGE_1_20_1,
            target_identity=NEOFORGE_26_1_2,
            rules=(rule,),
        )
        self.assertEqual((), repeated.changes)
        self.assertEqual("already_applied", repeated.skipped[0].reason)

        (self.root / source).parent.mkdir(parents=True, exist_ok=True)
        (self.root / source).write_text("{}\n", encoding="utf-8")
        with self.assertRaises(CodemodConflictError):
            plan_codemod(
                self.root,
                source_identity=FORGE_1_20_1,
                target_identity=NEOFORGE_26_1_2,
                rules=(rule,),
            )

    def test_json_field_mapping_is_explicit_and_conflict_safe(self):
        metadata = self.root / "metadata.json"
        shutil.copy2(FIXTURES / "json-field.json", metadata)
        rule = CodemodRule(
            rule_id="metadata-field",
            kind="json_field",
            source_identity=FORGE_1_20_1,
            target_identity=NEOFORGE_26_1_2,
            source_value="oldField",
            target_value="newField",
            evidence=self.evidence(),
            mode="transform",
            files=("metadata.json",),
            object_path=("loader",),
        )
        plan = plan_codemod(
            self.root,
            source_identity=FORGE_1_20_1,
            target_identity=NEOFORGE_26_1_2,
            rules=(rule,),
        )
        apply_codemod(self.root, plan)
        transformed = json.loads(metadata.read_text(encoding="utf-8"))
        self.assertEqual("value", transformed["loader"]["newField"])
        self.assertNotIn("oldField", transformed["loader"])
        self.assertTrue(transformed["loader"]["untouched"])

        metadata.write_text(
            '{"loader":{"oldField":1,"newField":2}}\n', encoding="utf-8"
        )
        with self.assertRaises(CodemodConflictError):
            plan_codemod(
                self.root,
                source_identity=FORGE_1_20_1,
                target_identity=NEOFORGE_26_1_2,
                rules=(rule,),
            )

    def test_apply_rejects_candidate_changes_after_plan(self):
        plan = plan_codemod(
            self.root,
            source_identity=FORGE_1_20_1,
            target_identity=NEOFORGE_26_1_2,
            rules=eventbus_import_rules(mode="transform"),
        )
        changed = self.root / "module-a/src/main/java/example/Primary.java"
        changed.write_text(changed.read_text() + "// concurrent edit\n", encoding="utf-8")

        with self.assertRaises(StaleCodemodPlanError):
            apply_codemod(self.root, plan)
        self.assertIn("net.minecraftforge.eventbus.api.SubscribeEvent", changed.read_text())

    def test_apply_rolls_back_prior_file_when_a_later_write_fails(self):
        import modport.codemod as codemod

        plan = plan_codemod(
            self.root,
            source_identity=FORGE_1_20_1,
            target_identity=NEOFORGE_26_1_2,
            rules=eventbus_import_rules(mode="transform"),
        )
        paths = [self.root / change.path for change in plan.changes]
        before = [path.read_bytes() for path in paths]
        original = codemod._atomic_write
        calls = 0

        def fail_second(path, data, mode):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError("fixture write failure")
            return original(path, data, mode)

        with patch("modport.codemod._atomic_write", side_effect=fail_second):
            with self.assertRaisesRegex(OSError, "fixture write failure"):
                apply_codemod(self.root, plan)
        self.assertEqual(before, [path.read_bytes() for path in paths])

    def test_apply_uses_recomputed_bytes_not_private_plan_payload(self):
        plan = plan_codemod(
            self.root,
            source_identity=FORGE_1_20_1,
            target_identity=NEOFORGE_26_1_2,
            rules=eventbus_import_rules(mode="transform"),
        )
        poisoned_change = replace(plan.changes[0], _after=b"malicious replacement\n")
        poisoned = replace(plan, changes=(poisoned_change, *plan.changes[1:]))

        apply_codemod(self.root, poisoned)
        transformed = (self.root / poisoned_change.path).read_text(encoding="utf-8")
        self.assertNotIn("malicious replacement", transformed)
        self.assertIn("net.neoforged.bus.api", transformed)

    def test_rejects_symlinks_and_unsafe_paths(self):
        outside = Path(self.temporary.name) / "outside.java"
        outside.write_text("class Outside {}\n", encoding="utf-8")
        (self.root / "linked.java").symlink_to(outside)
        with self.assertRaises(CodemodSecurityError):
            plan_codemod(
                self.root,
                source_identity=FORGE_1_20_1,
                target_identity=NEOFORGE_26_1_2,
                rules=eventbus_import_rules(),
            )
        with self.assertRaises(CodemodSecurityError):
            CodemodRule(
                rule_id="unsafe",
                kind="path_rename",
                source_identity=FORGE_1_20_1,
                target_identity=NEOFORGE_26_1_2,
                source_value="../outside.java",
                target_value="inside.java",
            )

    def test_transform_requires_evidence_and_existing_target_import_is_skipped(self):
        with self.assertRaisesRegex(ValueError, "require explicit evidence"):
            CodemodRule(
                rule_id="unsupported-transform",
                kind="java_import",
                source_identity=FORGE_1_20_1,
                target_identity=NEOFORGE_26_1_2,
                source_value="a.b.Source",
                target_value="a.b.Target",
                mode="transform",
            )

        source = self.root / "Duplicate.java"
        source.write_text(
            "import net.minecraftforge.eventbus.api.IEventBus;\n"
            "import net.neoforged.bus.api.IEventBus;\n",
            encoding="utf-8",
        )
        rule = next(rule for rule in eventbus_import_rules(mode="transform")
                    if rule.source_value.endswith("IEventBus"))
        rule = replace(rule, files=("Duplicate.java",))
        plan = plan_codemod(
            self.root,
            source_identity=FORGE_1_20_1,
            target_identity=NEOFORGE_26_1_2,
            rules=(rule,),
        )
        self.assertEqual((), plan.changes)
        self.assertEqual("target_import_present", plan.skipped[0].reason)

    def test_git_candidates_must_be_clean_and_plan_records_head(self):
        subprocess.run(["git", "init", "-q", str(self.root)], check=True)
        subprocess.run(["git", "-C", str(self.root), "add", "."], check=True)
        subprocess.run(
            [
                "git", "-C", str(self.root),
                "-c", "user.name=Codemod Test",
                "-c", "user.email=codemod@example.invalid",
                "commit", "-qm", "fixture",
            ],
            check=True,
        )
        plan = plan_codemod(
            self.root,
            source_identity=FORGE_1_20_1,
            target_identity=NEOFORGE_26_1_2,
            rules=eventbus_import_rules(),
        )
        self.assertRegex(plan.candidate_revision or "", r"^[0-9a-f]{40}$")

        (self.root / "untracked.txt").write_text("dirty\n", encoding="utf-8")
        with self.assertRaisesRegex(CodemodConflictError, "candidate is dirty"):
            plan_codemod(
                self.root,
                source_identity=FORGE_1_20_1,
                target_identity=NEOFORGE_26_1_2,
                rules=eventbus_import_rules(),
            )


if __name__ == "__main__":
    unittest.main()
