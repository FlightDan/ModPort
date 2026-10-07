"""Behavioral tests for exact, frozen skill inputs and trusted scanning."""
from hashlib import sha256
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from modport.contracts import OperationInput
from modport.evidence import seal_ref
from modport.opencode_runtime import OpenCodeCleanupError
from modport.skill_runtime import (TRUSTED_SCANNER, build_skill_registry,
                                  _scan_workspace, mod_scan, resolve_skill_inputs, skill_lookup,
                                  skill_publish, skill_resolve,
                                  validate_requested_skills)
from modport.skill_tools import bundle


class SkillRuntimeTests(unittest.TestCase):
    def test_v25_skill_dialogue_requests_shared_session_budget(self):
        self.java_only()
        captured = {}

        def observe(**kwargs):
            captured.update(kwargs)
            result = subprocess.CompletedProcess(['opencode'], 1, stdout='')
            result.dialogue_metadata = {'thread_id': 'synthetic-session', 'turns': 2}
            return result

        command = self.command('java_diff', options={
            'workflow_version': 25,
            'agent_dialogue_policy': {'version': 1, 'turns': ['plan', 'execute']},
        })
        with patch('modport.opencode_agent.run_agent', side_effect=observe):
            result = build_skill_registry()['java_diff'](command)
        self.assertEqual('failed', result.status)
        self.assertTrue(captured['auto_context_budget'])
        self.assertIn('planning_prompt', captured)

    def test_agent_failures_preserve_prompt_and_process_evidence(self):
        self.java_only()
        for error, code in ((subprocess.TimeoutExpired('opencode', .1), 'agent_timeout'),
                            (OSError(7, 'Argument list too long'), 'agent_launch_failed'),
                            (None, 'skill_agent_failed')):
            with self.subTest(code=code):
                def execute(**kwargs):
                    log = kwargs['log']
                    log.parent.mkdir(parents=True, exist_ok=True)
                    log.write_text('partial output')
                    Path(str(log) + '.stdin.txt').write_text(kwargs['prompt'])
                    if error is not None:
                        raise error
                    return subprocess.CompletedProcess(['opencode'], 1)
                with patch('modport.opencode_agent.run_agent', side_effect=execute):
                    result = build_skill_registry()['java_diff'](self.command('java_diff'))
                self.assertEqual('failed', result.status)
                self.assertEqual(code, result.error_code)
                self.assertEqual('partial output', (self.run / result.outputs['log']).read_text())
                self.assertIn('agent_prompt', result.outputs['artifact_refs'])

    def test_skill_startup_cleanup_failure_retains_safe_diagnostic(self):
        self.java_only()
        command = self.command('java_diff')
        failure = OpenCodeCleanupError({
            'cleanup_confirmed': False, 'target_pid': 900006,
            'detail': 'private provider message'})
        with patch('modport.opencode_agent.run_agent', side_effect=failure):
            result = build_skill_registry()['java_diff'](command)
        self.assertEqual('failed', result.status)
        self.assertEqual('opencode_cleanup_unconfirmed', result.error_code)
        ref = result.outputs['artifact_refs']['opencode_cleanup']
        record = (self.run / ref['path']).read_text()
        self.assertEqual(900006, json.loads(record)['target_pid'])
        self.assertNotIn('private provider message', record)

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.store = self.root / "store"
        self.run = self.root / "run"
        self.run.mkdir()
        self.request = {"source_minecraft": "1.20.1", "target_minecraft": "26.1.2",
                        "source_loader_version": "47.0.1", "target_loader_version": "26.1.2.106",
                        "source_java": "17", "target_java": "25", "skill_store": str(self.store)}

    def command(self, stage="skill_lookup", **kwargs):
        return OperationInput("run", "task", stage, stage + "-1", str(self.run),
                              payload={"request": self.request}, **kwargs)

    def write(self, path, value):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value), encoding="utf-8")

    def candidate(self, kind="java", suffix="one", *, approved=True):
        directory = self.root / (kind + "-" + suffix)
        directory.mkdir()
        identity = resolve_skill_inputs(self.command())["identities"][kind]
        metadata = {"schema_version": 1, "kind": kind, "skill_id": kind + "-fixture", **identity,
                    "generator_id": "generator"}
        rule = {"id": "old-api", "category": "api", "summary": "old", "recommendation": "inspect",
                "verification": "test manually", "evidence": [{"source": "https://example.org/source",
                "locator": "v1", "supports": "change"}], "files": ["*.java"],
                "pattern": "OldApi", "flags": [], "examples": {"match": ["OldApi"], "no_match": ["NewApi"]}}
        self.write(directory / "metadata.json", metadata)
        self.write(directory / "rules.json", {"schema_version": 1, **identity, "rules": [rule],
                                              "manual_checks": [], "known_gaps": [suffix]})
        self.write(directory / "coverage.json", {"areas": [{"id": "api", "status": "verified", "rule_ids": ["old-api"]}]})
        self.write(directory / "evidence.json", {})
        (directory / "SKILL.md").write_text("---\nname: fixture\ndescription: migration\n---\nInspect candidates.")
        (directory / "scripts").mkdir()
        shutil.copyfile(TRUSTED_SCANNER, directory / "scripts" / "scan.py")
        manifest = bundle.build_manifest(directory)
        self.write(directory / "manifest.json", manifest)
        self.write(directory / "review.json", {"verdict": "approved" if approved else "rejected",
                    "bundle_sha256": manifest["bundle_sha256"], "reviewer_id": "reviewer"})
        return directory

    def publish(self, kind="java", suffix="one"):
        return bundle.publish(self.candidate(kind, suffix), self.store)

    def java_only(self):
        self.request.update(workflow_mode="skill_generation", skill_kind="java")

    def test_submission_preflight_rejects_unavailable_selector_without_writes(self):
        self.request["platform_skill_revision"] = "54b425f7a129c2347c4d4fc0522ce45b5384e74e2587edaaa5dbb9b"
        destination = self.root / "uncreated-run"
        with self.assertRaisesRegex(ValueError, "requested platform revision .* unavailable"):
            validate_requested_skills(self.request, destination)
        self.assertFalse(destination.exists())

    def test_submission_preflight_accepts_exact_published_revision(self):
        path = self.publish("platform")
        self.request["platform_skill_revision"] = path.name
        destination = self.root / "uncreated-run"
        validate_requested_skills(self.request, destination)
        self.assertFalse(destination.exists())

    def test_submission_preflight_defers_undiscovered_versions(self):
        self.request["platform_skill_revision"] = "explicit-selector"
        del self.request["source_loader_version"]
        validate_requested_skills(self.request, self.root / "uncreated-run")

    def test_lookup_missing_exact_versions_blocks_instead_of_guessing(self):
        del self.request["source_loader_version"]
        result = skill_lookup(self.command())
        self.assertEqual(result.status, "blocked")
        self.assertIn("source_loader_version", result.detail)

    def test_skill_resolve_is_registered_and_does_not_start_research(self):
        self.java_only()
        self.assertIs(build_skill_registry()["skill_resolve"], skill_resolve)

        result = skill_resolve(self.command("skill_resolve"))

        self.assertEqual(result.status, "completed", result.detail)
        self.assertEqual(result.outputs["skill_references"], {})
        self.assertEqual(result.outputs["resolved_kinds"], [])
        self.assertIn("java: exact reusable skill unavailable", result.outputs["diagnostics"])
        self.assertFalse((self.run / "workspaces/skills/java").exists())

    def test_skill_resolve_returns_authenticated_frozen_references(self):
        self.java_only()
        self.publish()

        result = skill_resolve(self.command("skill_resolve"))

        self.assertEqual(result.status, "completed", result.detail)
        self.assertEqual(result.outputs["resolved_kinds"], ["java"])
        self.assertEqual(result.outputs["diagnostics"], [])
        reference = result.outputs["skill_references"]["java"]
        self.assertEqual(reference["path"], "artifacts/skills/java")
        self.assertEqual(reference["bundle_sha256"],
            bundle.verify(self.run / reference["path"], approved=True)["bundle_sha256"])
        index_ref = result.outputs["artifact_refs"]["skill_references"]
        self.assertEqual(index_ref["sha256"],
            sha256((self.run / index_ref["path"]).read_bytes()).hexdigest())

    def test_skill_resolve_reports_unapproved_cache_without_materializing_research(self):
        self.java_only()
        cached = self.publish()
        (cached / "review.json").unlink()

        result = skill_resolve(self.command("skill_resolve"))

        self.assertEqual(result.status, "completed", result.detail)
        self.assertEqual(result.outputs["skill_references"], {})
        self.assertEqual(result.outputs["needs_review_kinds"], ["java"])
        self.assertIn("java: independent review still required", result.outputs["diagnostics"])
        self.assertFalse((self.run / "workspaces/skills/java").exists())

    def test_reads_source_preparation_and_locked_target_versions(self):
        for key in ("source_loader_version", "source_java", "target_loader_version", "target_java"):
            del self.request[key]
        self.write(self.run / "artifacts" / "preparation.json", {"source_loader_version": "47.0.1", "source_java": "17"})
        self.write(self.run / "artifacts" / "locked-manifest.json", {"neoforge_version": "26.1.2.106", "java_version": 25})
        inputs = resolve_skill_inputs(self.command())
        self.assertEqual(inputs["identities"]["java"]["target"], {"java": "25"})
        self.assertEqual(inputs["identities"]["platform"]["source"]["loader_version"], "47.0.1")

    def test_accepts_host_sealed_content_addressed_references(self):
        self.java_only()
        self.publish()
        found = skill_lookup(self.command())
        reference = seal_ref(self.run, found.outputs["artifact_refs"]["skill_references"],
                             execution_id="lookup-execution")
        self.assertEqual(reference["path"], "artifacts/skill-references.json")
        result = skill_publish(self.command("skill_publish", artifact_refs={"skill_references": reference}))
        self.assertEqual(result.status, "completed", result.detail)
        path = self.run / "artifacts" / "skill-references.json"
        path.write_text("{}")
        result = skill_publish(self.command("skill_publish", artifact_refs={"skill_references": reference}))
        self.assertEqual(result.status, "blocked")

    def test_request_cannot_override_locked_version(self):
        self.write(self.run / "artifacts" / "locked-manifest.json", {"java_version": 21})
        self.assertEqual(skill_lookup(self.command()).status, "blocked")

    def test_java_standalone_does_not_require_platform_versions(self):
        self.java_only()
        del self.request["source_loader_version"]
        self.assertEqual(skill_lookup(self.command()).outputs["missing_kinds"], ["java"])

    def test_cache_frozen_copy_survives_store_removal(self):
        self.java_only()
        self.publish()
        found = skill_lookup(self.command())
        self.assertEqual(found.status, "completed", found.detail)
        self.assertEqual(found.outputs["missing_kinds"], [])
        shutil.rmtree(self.store)
        result = skill_publish(self.command("skill_publish", artifact_refs=found.outputs["artifact_refs"]))
        self.assertEqual(result.status, "completed", result.detail)
        frozen = self.run / "artifacts" / "skills" / "java"
        self.assertEqual(bundle.verify(frozen, approved=True)["source"], {"java": "17"})

    def test_v17_publish_preserves_approved_lookup_without_workspace(self):
        self.java_only()
        self.publish()
        found = skill_lookup(self.command())
        self.assertEqual(found.status, "completed", found.detail)
        self.assertFalse((self.run / "workspaces/skills/java").exists())

        published = skill_publish(self.command("skill_publish",
            options={"workflow_version": 17}, artifact_refs=found.outputs["artifact_refs"]))

        self.assertEqual(published.status, "completed", published.detail)
        self.assertIn("java", published.outputs["skill_references"])
        references = json.loads((self.run / "artifacts/skill-references.json").read_text())
        self.assertEqual(set(references["skills"]), {"java"})

    def test_skill_reference_with_wrong_hash_is_not_trusted(self):
        self.java_only()
        self.publish()
        found = skill_lookup(self.command())
        bad_ref = dict(found.outputs["artifact_refs"]["skill_references"], sha256="0" * 64)

        result = skill_publish(self.command("skill_publish",
            artifact_refs={"skill_references": bad_ref}))

        self.assertEqual(result.status, "blocked")
        self.assertIn("digest", result.detail)

    def test_explicit_revision_missing_or_wrong_versions_never_regenerates(self):
        self.java_only()
        path = self.publish()
        self.request["java_skill_revision"] = path.name
        self.request["source_java"] = "21"
        result = skill_lookup(self.command())
        self.assertEqual(result.status, "blocked")
        self.assertIn("unavailable", result.detail)

    def test_multiple_revisions_select_the_newest_approved_payload(self):
        self.java_only()
        self.publish()
        self.publish(suffix="two")
        result = skill_lookup(self.command())
        self.assertEqual(result.status, "completed", result.detail)
        self.assertEqual(result.outputs["missing_kinds"], [])
        self.assertEqual(result.outputs["needs_review_kinds"], [])
        frozen_rules = json.loads((self.run / "artifacts/skills/java/rules.json").read_text())
        self.assertEqual(frozen_rules["known_gaps"], ["two"])

    def test_explicit_revision_selector_remains_honored_after_freezing(self):
        self.java_only()
        first = self.publish()
        second = self.publish(suffix="two")
        self.request["java_skill_revision"] = first.name
        self.assertEqual(skill_lookup(self.command()).status, "completed")
        self.request["java_skill_revision"] = second.name
        self.assertEqual(skill_lookup(self.command()).status, "completed")
        frozen_rules = json.loads((self.run / "artifacts/skills/java/rules.json").read_text())
        self.assertEqual(frozen_rules["known_gaps"], ["two"])

    def test_unreviewed_cache_is_materialized_and_only_review_is_needed(self):
        self.java_only()
        cached = self.publish()
        (cached / "review.json").unlink()
        result = skill_lookup(self.command())
        self.assertEqual(result.status, "completed", result.detail)
        self.assertEqual(result.outputs["missing_kinds"], [])
        self.assertEqual(result.outputs["needs_review_kinds"], ["java"])
        self.assertEqual(result.outputs["reusable_kinds"], ["java"])
        self.assertEqual(result.outputs["cached_references"], {})
        workspace = self.run / "workspaces/skills/java"
        self.assertTrue((workspace / "rules.json").is_file())
        self.assertFalse((self.run / "artifacts/skills/java").exists())

    def test_existing_unreviewed_workspace_is_found_by_lookup(self):
        self.java_only()
        workspace = self.run / "workspaces/skills/java"
        workspace.parent.mkdir(parents=True)
        shutil.copytree(self.candidate(), workspace)
        (workspace / "review.json").unlink()
        result = skill_lookup(self.command())
        self.assertEqual(result.status, "completed", result.detail)
        self.assertEqual(result.outputs["missing_kinds"], [])
        self.assertEqual(result.outputs["needs_review_kinds"], ["java"])

    def test_payload_change_with_stale_manifest_reuses_cached_skill(self):
        self.java_only()
        path = self.publish()
        (path / "SKILL.md").write_text("---\nname: bad\ndescription: changed\n---\n")
        result = skill_lookup(self.command())
        self.assertEqual(result.status, "completed", result.detail)

    def test_self_approval_and_wrong_hash_rejected(self):
        directory = self.candidate()
        review = json.loads((directory / "review.json").read_text())
        review["reviewer_id"] = "generator"
        self.write(directory / "review.json", review)
        with self.assertRaises(ValueError):
            bundle.publish(directory, self.store)
        review.update(reviewer_id="reviewer", bundle_sha256="0" * 64)
        self.write(directory / "review.json", review)
        bundle.publish(directory, self.store)

    def test_portable_bundle_scan_uses_host_scanner_not_payload(self):
        # An approved package may contain arbitrary scripts. None may be executed.
        self.java_only()
        candidate = self.candidate()
        (candidate / "scripts" / "scan.py").write_text("raise RuntimeError('untrusted payload executed')")
        manifest = bundle.build_manifest(candidate)
        self.write(candidate / "manifest.json", manifest)
        self.write(candidate / "review.json", {"verdict": "approved", "reviewer_id": "reviewer",
                                                "bundle_sha256": manifest["bundle_sha256"]})
        bundle.publish(candidate, self.store)
        found = skill_lookup(self.command())
        baseline = self.run / "baseline"
        baseline.mkdir()
        (baseline / "Example.java").write_text("class Example { OldApi a; }")
        (baseline / "gradlew").write_text("exit 99")
        before = {p.name: p.read_bytes() for p in baseline.iterdir()}
        def execute(args, **kwargs):
            self.assertEqual(Path(args[2]), TRUSTED_SCANNER)
            return subprocess.run(args, cwd=kwargs["cwd"], capture_output=True, text=True, timeout=10)
        with patch("modport.handlers._exec", side_effect=execute):
            result = mod_scan(self.command("mod_scan", artifact_refs=found.outputs["artifact_refs"]))
        self.assertEqual(result.status, "completed", result.detail)
        report = json.loads((self.run / "artifacts" / "mod-scan-report.json").read_text())
        self.assertFalse(report["compatibility_verified"])
        self.assertEqual(len(report["skills"]["java"]["report"]["findings"]), 1)
        self.assertEqual(before, {p.name: p.read_bytes() for p in baseline.iterdir()})

    def test_scan_excludes_generated_harness_and_modport_sources(self):
        self.java_only()
        self.publish()
        found = skill_lookup(self.command())
        baseline = self.run / "baseline"
        for relative in ("Source.java", ".modport/independent-tests/Generated.java",
                          "src/generated/Generated.java", "harness/Harness.java",
                          "build/generated/BuildGenerated.java",
                          "src/main/java/example/harness/SourceHarness.java",
                          "src/main/java/example/generated/SourceGenerated.java"):
            path = baseline / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("class Candidate { OldApi value; }\n")
        def execute(args, **kwargs):
            return subprocess.run(args, cwd=kwargs["cwd"], capture_output=True, text=True, timeout=10)
        with patch("modport.handlers._exec", side_effect=execute):
            result = mod_scan(self.command("mod_scan", artifact_refs=found.outputs["artifact_refs"]))
        self.assertEqual(result.status, "completed", result.detail)
        report = json.loads((self.run / "artifacts" / "mod-scan-report.json").read_text())
        scanned = {item["path"] for item in report["skills"]["java"]["report"]["scanned_files"]}
        self.assertEqual(scanned, {"Source.java", "src/main/java/example/harness/SourceHarness.java",
                                   "src/main/java/example/generated/SourceGenerated.java"})
        self.assertEqual(len(report["skills"]["java"]["report"]["findings"]), 3)

    def test_v19_scan_uses_bound_worktree_instead_of_baseline(self):
        self.java_only()
        self.publish()
        found = skill_resolve(self.command("skill_resolve"))
        baseline = self.run / "baseline"
        baseline.mkdir()
        (baseline / "Baseline.java").write_text("class Baseline { OldApi old; }\n")
        worktree = self.run / "worktree"
        worktree.mkdir()
        (worktree / "Candidate.java").write_text("class Candidate { OldApi current; }\n")
        subprocess.run(["git", "init", "-q"], cwd=worktree, check=True)
        subprocess.run(["git", "add", "Candidate.java"], cwd=worktree, check=True)
        subprocess.run(["git", "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
                        "commit", "-qm", "candidate"], cwd=worktree, check=True)

        def execute(args, **kwargs):
            return subprocess.run(args, cwd=kwargs["cwd"], capture_output=True, text=True, timeout=10,
                                  env=kwargs.get("env"))

        with patch("modport.handlers._exec", side_effect=execute):
            result = mod_scan(self.command("mod_scan", options={"workflow_version": 19},
                artifact_refs=found.outputs["artifact_refs"]))

        self.assertEqual(result.status, "completed", result.detail)
        self.assertEqual(result.outputs["workspace"], "worktree")
        self.assertEqual(result.outputs["candidate_identity"]["kind"], "git_commit")
        report = json.loads((self.run / "artifacts/mod-scan-report.json").read_text())
        self.assertEqual(report["workspace"], "worktree")
        self.assertEqual(report["candidate_identity"], result.outputs["candidate_identity"])
        scanned = {row["path"] for row in report["skills"]["java"]["report"]["scanned_files"]}
        self.assertEqual(scanned, {"Candidate.java"})

    def test_scan_workspace_is_versioned_and_honors_v19_override(self):
        for name in ("baseline", "worktree", "candidate"):
            (self.run / name).mkdir()
        legacy = self.command("mod_scan", options={"workflow_version": 18, "workspace": "candidate"})
        current = self.command("mod_scan", options={"workflow_version": 19})
        overridden = self.command("mod_scan",
            options={"workflow_version": 19, "workspace": "candidate"})

        self.assertEqual(_scan_workspace(legacy), (self.run / "baseline", "baseline"))
        self.assertEqual(_scan_workspace(current), (self.run / "worktree", "worktree"))
        self.assertEqual(_scan_workspace(overridden), (self.run / "candidate", "candidate"))

    def test_frozen_review_tamper_is_detected(self):
        self.java_only()
        self.publish()
        found = skill_lookup(self.command())
        path = self.run / "artifacts" / "skills" / "java" / "review.json"
        value = json.loads(path.read_text())
        value["reviewer_id"] = "different-reviewer"
        self.write(path, value)
        result = skill_publish(self.command("skill_publish", artifact_refs=found.outputs["artifact_refs"]))
        self.assertEqual(result.status, "completed", result.detail)

    def test_generation_review_and_publication_require_matching_effective_outcomes(self):
        self.java_only()
        candidate = self.candidate(approved=False)
        workspace = self.run / "workspaces" / "skills" / "java"
        registry = build_skill_registry()
        def generate(**kwargs):
            self.assertEqual(kwargs['model'], 'gpt-6-luna')
            self.assertEqual(kwargs['variant'], 'max')
            self.assertIn('Assignment:', kwargs['prompt'])
            kwargs['log'].parent.mkdir(parents=True, exist_ok=True)
            kwargs['log'].write_text('generation output')
            Path(str(kwargs['log']) + '.stdin.txt').write_text(kwargs['prompt'])
            shutil.copytree(candidate, workspace, dirs_exist_ok=True)
            metadata = json.loads((workspace / "metadata.json").read_text())
            metadata["generator_id"] = "java-diff-java_diff-1"
            self.write(workspace / "metadata.json", metadata)
            return subprocess.CompletedProcess(['opencode'], 0)
        with patch("modport.opencode_agent.run_agent", side_effect=generate):
            generation = registry["java_diff"](self.command("java_diff"))
        self.assertEqual(generation.status, "completed", generation.detail)
        self.assertIn('agent_prompt', generation.outputs['artifact_refs'])
        # Generation is complete, but publication still needs a usable review.
        blocked = skill_publish(self.command("skill_publish"))
        self.assertEqual(blocked.status, "blocked")
        def review(**kwargs):
            self.assertEqual(kwargs['model'], 'gpt-6-luna')
            self.assertEqual(kwargs['variant'], 'max')
            self.assertIn('Independently audit', kwargs['prompt'])
            self.assertFalse((workspace / "review.json").exists())
            self.write(workspace / "review.json", {"verdict": "approved", "findings": [],
                "reviewer_id": "java-review-java_skill_review-1",
                "bundle_sha256": generation.outputs["bundle_sha256"]})
            return subprocess.CompletedProcess(['opencode'], 0)
        with patch("modport.opencode_agent.run_agent", side_effect=review):
            reviewed = registry["java_skill_review"](self.command("java_skill_review"))
        self.assertEqual(reviewed.status, "completed", reviewed.detail)
        self.assertEqual(reviewed.outputs["verdict"], "approved")
        published_without_history = skill_publish(self.command("skill_publish", upstream_results={
            "java_diff": generation.to_dict()}))
        self.assertEqual(published_without_history.status, "completed", published_without_history.detail)
        published = skill_publish(self.command("skill_publish", upstream_results={
            "java_diff": generation.to_dict(), "java_skill_review": reviewed.to_dict()}))
        self.assertEqual(published.status, "completed", published.detail)

    def test_scanner_incomplete_exit_is_blocked_with_report(self):
        self.java_only()
        self.publish()
        found = skill_lookup(self.command())
        baseline = self.run / "baseline"
        baseline.mkdir()
        (baseline / "Incomplete.java").write_bytes(b"OldApi\x00")
        def execute(args, **kwargs):
            return subprocess.run(args, cwd=kwargs["cwd"], capture_output=True, text=True, timeout=10)
        with patch("modport.handlers._exec", side_effect=execute):
            result = mod_scan(self.command("mod_scan", artifact_refs=found.outputs["artifact_refs"]))
        self.assertEqual(result.status, "blocked", result.detail)
        self.assertEqual(result.error_code, "skill_scan_incomplete")
        self.assertIn("mod_scan_report", result.outputs["artifact_refs"])

    def test_v17_unapproved_skill_is_snapshotted_and_incomplete_scan_is_diagnostic(self):
        self.java_only()
        workspace = self.run / "workspaces" / "skills" / "java"
        workspace.parent.mkdir(parents=True)
        shutil.copytree(self.candidate(approved=False), workspace)
        options = {"workflow_version": 17}
        published = skill_publish(self.command("skill_publish", options=options))
        self.assertEqual(published.status, "completed", published.detail)
        self.assertEqual(published.outputs["acceptance_status"], "unverified")
        self.assertIn("java", published.outputs["skill_references"])
        baseline = self.run / "baseline"
        baseline.mkdir()
        (baseline / "Incomplete.java").write_bytes(b"OldApi\x00")

        def execute(args, **kwargs):
            return subprocess.run(args, cwd=kwargs["cwd"], capture_output=True, text=True, timeout=10)

        with patch("modport.handlers._exec", side_effect=execute):
            scanned = mod_scan(self.command("mod_scan", options=options,
                artifact_refs=published.outputs["artifact_refs"]))
        self.assertEqual(scanned.status, "completed", scanned.detail)
        self.assertIsNone(scanned.error_code)
        self.assertEqual(scanned.outputs["acceptance_status"], "unverified")
        self.assertFalse(scanned.outputs["scan_complete"])

    def test_reviewer_cannot_change_payload(self):
        self.java_only()
        candidate = self.candidate(approved=False)
        workspace = self.run / "workspaces" / "skills" / "java"
        workspace.parent.mkdir(parents=True)
        shutil.copytree(candidate, workspace)
        def execute(**kwargs):
            self.assertEqual(kwargs['variant'], 'max')
            self.assertEqual(kwargs["cwd"], workspace)
            (workspace / "SKILL.md").write_text("---\nname: edited\ndescription: edited\n---\n")
            return subprocess.CompletedProcess(['opencode'], 0)
        with patch("modport.opencode_agent.run_agent", side_effect=execute):
            result = build_skill_registry()["java_skill_review"](self.command("java_skill_review"))
        self.assertEqual(result.status, "blocked")

    def test_complete_workspace_without_seal_is_reused(self):
        self.java_only()
        workspace = self.run / "workspaces" / "skills" / "java"
        workspace.parent.mkdir(parents=True)
        shutil.copytree(self.candidate(), workspace)
        (workspace / "manifest.json").unlink()
        (workspace / "review.json").unlink()
        with patch("modport.handlers._exec", side_effect=AssertionError("complete skill must be reused")):
            result = build_skill_registry()["java_diff"](self.command("java_diff"))
        self.assertEqual(result.status, "completed", result.detail)
        self.assertTrue(result.outputs["reused"])
        self.assertTrue(result.outputs["needs_review"])

    def test_stale_manifest_and_review_digest_do_not_invalidate_cached_skill(self):
        self.java_only()
        path = self.publish()
        # Keep a valid payload and an independently approved review while
        # leaving both derived digests at their old values.
        (path / "SKILL.md").write_text("---\nname: changed\ndescription: changed\n---\n")
        result = skill_lookup(self.command())
        self.assertEqual(result.status, "completed", result.detail)
        self.assertFalse(result.outputs["cached_references"]["java"]["bundle_sha256"] ==
                         json.loads((path / "manifest.json").read_text())["bundle_sha256"])


if __name__ == "__main__":
    unittest.main()
