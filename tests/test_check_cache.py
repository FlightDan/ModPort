"""Focused tests for the v20 deterministic static-scan cache."""
from __future__ import annotations

from hashlib import sha256
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from modport.check_cache import (
    CacheUnavailable,
    load_static_scan,
    prepare_static_scan,
    publish_static_scan,
    static_scan_config,
)
from modport.contracts import OperationInput
from modport.skill_runtime import TRUSTED_SCANNER, mod_scan


class CheckCacheTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.workspace = self.root / "worktree"
        self.workspace.mkdir()
        (self.workspace / "Example.java").write_text(
            "class Example { OldApi value; }\n", encoding="utf-8")
        self.rules = self.root / "rules.json"
        self.identities = {"source": {"java": "17"}, "target": {"java": "25"}}
        self.rules.write_text(json.dumps({
            "schema_version": 1,
            **self.identities,
            "rules": [{"id": "old-api", "files": ["*.java"]}],
            "manual_checks": [],
            "known_gaps": [],
        }), encoding="utf-8")
        self.scanner = self.root / "scanner.py"
        self.scanner.write_text("# trusted scanner fixture\n", encoding="utf-8")
        self.store = self.root / "check-cache"
        self.config = static_scan_config()
        self.candidate = {"kind": "git_commit", "value": "a" * 40}

    def key(self, **overrides):
        values = {
            "workspace": self.workspace,
            "kind": "java",
            "candidate_identity": self.candidate,
            "identities": self.identities,
            "bundle_sha256": "b" * 64,
            "rules_path": self.rules,
            "expected_rules_sha256": sha256(self.rules.read_bytes()).hexdigest(),
            "scanner_path": self.scanner,
            "config": self.config,
        }
        values.update(overrides)
        return prepare_static_scan(**values)

    def report(self):
        return {
            "schema_version": 1,
            "source": self.identities["source"],
            "target": self.identities["target"],
            "root": str(self.workspace),
            "rules_sha256": sha256(self.rules.read_bytes()).hexdigest(),
            "scan_complete": True,
            "knowledge_entries": [],
            "known_gaps": [],
            "manual_checks": [],
            "rules": [],
            "findings": [],
            "scanned_files": [{"path": "Example.java", "sha256": sha256(
                (self.workspace / "Example.java").read_bytes()).hexdigest()}],
            "skipped": [],
            "scope": {
                "excluded_directory_names": self.config["excluded_directory_names"],
                "files_without_applicable_rules": 0,
                "excluded_directories": 0,
                "max_file_bytes": self.config["max_file_bytes"],
                "max_findings": self.config["max_findings"],
            },
        }

    def test_complete_report_round_trips_as_reuse_not_execution_or_acceptance(self):
        key = self.key()
        stored = publish_static_scan(self.store, key, self.report(),
                                     workspace=self.workspace, config=self.config)
        loaded = load_static_scan(self.store, key, workspace=self.workspace,
                                  config=self.config)

        self.assertEqual(stored["status"], "stored")
        self.assertIsNotNone(loaded)
        report, observation = loaded
        self.assertEqual(report["root"], str(self.workspace))
        self.assertEqual(observation["status"], "hit")
        self.assertTrue(observation["reused_result"])
        self.assertFalse(observation["scan_executed"])
        self.assertFalse(observation["counts_as_new_execution"])
        self.assertFalse(observation["acceptance_evidence"])

    def test_fingerprint_covers_content_versions_rules_scanner_environment_and_config(self):
        original = self.key().fingerprint
        (self.workspace / "Example.java").write_text("class Example { NewApi value; }\n")
        self.assertNotEqual(original, self.key().fingerprint)
        (self.workspace / "Example.java").write_text("class Example { OldApi value; }\n")

        changed_identity = {"source": {"java": "21"}, "target": {"java": "25"}}
        self.assertNotEqual(original, self.key(identities=changed_identity).fingerprint)
        self.assertNotEqual(original, self.key(bundle_sha256="c" * 64).fingerprint)
        self.scanner.write_text("# changed trusted scanner\n")
        self.assertNotEqual(original, self.key().fingerprint)
        self.scanner.write_text("# trusted scanner fixture\n")
        changed_config = {**self.config, "max_findings": 9999}
        self.assertNotEqual(original, self.key(config=changed_config).fingerprint)
        with patch("modport.check_cache._runtime_identity",
                   return_value={"implementation": "different"}):
            self.assertNotEqual(original, self.key().fingerprint)

    def test_rules_digest_mismatch_and_workspace_symlink_disable_cache(self):
        with self.assertRaisesRegex(CacheUnavailable, "rules_digest_mismatch"):
            self.key(expected_rules_sha256="0" * 64)
        linked = self.workspace / "Linked.java"
        linked.symlink_to(self.workspace / "Example.java")
        with self.assertRaisesRegex(CacheUnavailable, "workspace_symlink_rejected"):
            self.key()

    def test_applicable_file_read_is_bounded(self):
        (self.workspace / "Huge.java").write_bytes(
            b"x" * (self.config["max_file_bytes"] + 1))
        with self.assertRaisesRegex(CacheUnavailable, "file_limit_exceeded"):
            self.key()

    def test_incomplete_report_is_never_published(self):
        report = self.report()
        report["scan_complete"] = False
        report["skipped"] = [{"path": "Example.java", "reason": "incomplete"}]
        with self.assertRaisesRegex(CacheUnavailable, "scan_incomplete"):
            publish_static_scan(self.store, self.key(), report,
                                workspace=self.workspace, config=self.config)
        self.assertFalse(self.store.exists())

    def test_tampered_cached_report_is_rejected(self):
        key = self.key()
        stored = publish_static_scan(self.store, key, self.report(),
                                     workspace=self.workspace, config=self.config)
        object_path = self.store / "static-scan-v1" / "objects" / (
            stored["report_sha256"] + ".json")
        value = json.loads(object_path.read_text())
        value["findings"] = [{"rule_id": "forged"}]
        object_path.write_text(json.dumps(value))

        with self.assertRaisesRegex(CacheUnavailable, "cache_report_digest_mismatch"):
            load_static_scan(self.store, key, workspace=self.workspace,
                             config=self.config)

    def test_digest_matching_non_object_report_is_rejected(self):
        key = self.key()
        publish_static_scan(self.store, key, self.report(),
                            workspace=self.workspace, config=self.config)
        cache_root = self.store / "static-scan-v1"
        manifest_path = cache_root / "keys" / f"java-{key.fingerprint}.json"
        manifest = json.loads(manifest_path.read_text())
        report_raw = b"[]"
        report_digest = sha256(report_raw).hexdigest()
        (cache_root / "objects" / f"{report_digest}.json").write_bytes(report_raw)
        manifest["report_sha256"] = report_digest
        manifest["report_size"] = len(report_raw)
        manifest_path.write_text(json.dumps(manifest))

        with self.assertRaisesRegex(CacheUnavailable, "cache_report_invalid"):
            load_static_scan(self.store, key, workspace=self.workspace,
                             config=self.config)


class SkillRuntimeCacheIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.run = self.root / "run"
        self.worktree = self.run / "worktree"
        self.rules_dir = self.run / "artifacts" / "skills" / "java"
        self.worktree.mkdir(parents=True)
        self.rules_dir.mkdir(parents=True)
        (self.worktree / "Example.java").write_text(
            "class Example { OldApi value; }\n", encoding="utf-8")
        subprocess.run(["git", "init", "-q"], cwd=self.worktree, check=True)
        subprocess.run(["git", "add", "Example.java"], cwd=self.worktree, check=True)
        subprocess.run(["git", "-c", "user.name=Test", "-c",
                        "user.email=test@example.invalid", "commit", "-qm", "candidate"],
                       cwd=self.worktree, check=True)
        self.identities = {"source": {"java": "17"}, "target": {"java": "25"}}
        rule = {
            "id": "old-api", "category": "api", "summary": "old",
            "recommendation": "inspect", "verification": "compile",
            "evidence": [{"source": "https://example.invalid", "locator": "v1",
                          "supports": "fixture"}],
            "files": ["*.java"], "pattern": "OldApi", "flags": [],
            "examples": {"match": ["OldApi"], "no_match": ["NewApi"]},
        }
        self.rules = self.rules_dir / "rules.json"
        self.rules.write_text(json.dumps({"schema_version": 1, **self.identities,
            "rules": [rule], "manual_checks": [], "known_gaps": []}), encoding="utf-8")
        self.reference = {
            "path": "artifacts/skills/java", "bundle_sha256": "b" * 64,
            "files": {"rules.json": sha256(self.rules.read_bytes()).hexdigest()},
        }
        self.inputs = {"kinds": ["java"], "identities": {"java": self.identities},
                       "revisions": {"java": None}, "store": str(self.root / "skills")}

    def command(self, identifier, version):
        return OperationInput("run", "mod_scan", "mod_scan", identifier, str(self.run),
            payload={"request": {"workflow_mode": "skill_generation", "skill_kind": "java",
                                 "source_java": "17", "target_java": "25"}},
            options={"workflow_version": version},
            artifact_refs={"skill_references": {"path": "unused", "sha256": "0" * 64}})

    @staticmethod
    def execute(args, **kwargs):
        return subprocess.run(args, cwd=kwargs["cwd"], capture_output=True, text=True,
                              timeout=10, env=kwargs.get("env"))

    def invoke(self, identifier, version, execute):
        with patch("modport.skill_runtime._read_references",
                   return_value={"java": self.reference}), patch(
                   "modport.skill_runtime.resolve_skill_inputs", return_value=self.inputs), patch(
                   "modport.handlers._exec", side_effect=execute):
            return mod_scan(self.command(identifier, version))

    def test_v20_second_scan_hits_cache_and_rebinds_current_candidate(self):
        first = self.invoke("scan-1", 20, self.execute)
        self.assertEqual(first.status, "completed", first.detail)
        self.assertEqual(first.outputs["check_cache"]["entries"]["java"]["status"], "stored")

        def no_scanner(args, **kwargs):
            if len(args) > 2 and Path(args[2]) == TRUSTED_SCANNER:
                raise AssertionError("scanner ran on cache hit")
            return self.execute(args, **kwargs)

        second = self.invoke("scan-2", 20, no_scanner)
        self.assertEqual(second.status, "completed", second.detail)
        entry = second.outputs["check_cache"]["entries"]["java"]
        self.assertEqual(entry["status"], "hit")
        self.assertFalse(entry["counts_as_new_execution"])
        report = json.loads((self.run / "artifacts/mod-scan-report.json").read_text())
        self.assertEqual(report["candidate_identity"], second.outputs["candidate_identity"])
        self.assertEqual(report["skills"]["java"]["report"]["root"], str(self.worktree))
        self.assertFalse(report["check_cache"]["acceptance_evidence"])

    def test_v19_keeps_executing_and_emits_no_cache_contract(self):
        calls = []

        def execute(args, **kwargs):
            if len(args) > 2 and Path(args[2]) == TRUSTED_SCANNER:
                calls.append(args)
            return self.execute(args, **kwargs)

        first = self.invoke("legacy-1", 19, execute)
        second = self.invoke("legacy-2", 19, execute)

        self.assertEqual(first.status, "completed", first.detail)
        self.assertEqual(second.status, "completed", second.detail)
        self.assertEqual(len(calls), 2)
        self.assertNotIn("check_cache", first.outputs)
        report = json.loads((self.run / "artifacts/mod-scan-report.json").read_text())
        self.assertNotIn("check_cache", report)

    def test_v20_tampered_cache_falls_back_to_a_fresh_scan(self):
        first = self.invoke("tamper-1", 20, self.execute)
        entry = first.outputs["check_cache"]["entries"]["java"]
        object_path = self.root / "check-cache/static-scan-v1/objects" / (
            entry["report_sha256"] + ".json")
        object_path.write_text("{}\n")
        scanner_calls = []

        def execute(args, **kwargs):
            if len(args) > 2 and Path(args[2]) == TRUSTED_SCANNER:
                scanner_calls.append(args)
            return self.execute(args, **kwargs)

        second = self.invoke("tamper-2", 20, execute)

        self.assertEqual(second.status, "completed", second.detail)
        self.assertEqual(len(scanner_calls), 1)
        self.assertEqual(second.outputs["check_cache"]["entries"]["java"]["status"],
                         "stored")

    def test_v20_incomplete_scan_is_not_cached(self):
        (self.worktree / "Example.java").write_bytes(b"OldApi\x00")
        scanner_calls = []

        def execute(args, **kwargs):
            if len(args) > 2 and Path(args[2]) == TRUSTED_SCANNER:
                scanner_calls.append(args)
            return self.execute(args, **kwargs)

        first = self.invoke("incomplete-1", 20, execute)
        second = self.invoke("incomplete-2", 20, execute)

        self.assertEqual(first.status, "completed", first.detail)
        self.assertEqual(second.status, "completed", second.detail)
        self.assertFalse(first.outputs["scan_complete"])
        self.assertEqual(len(scanner_calls), 2)
        self.assertEqual(first.outputs["check_cache"]["entries"]["java"]["status"],
                         "not_stored")
        keys = self.root / "check-cache/static-scan-v1/keys"
        self.assertEqual(list(keys.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
