import copy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from modport.contracts import OperationInput, OperationResult
from modport.evidence import atomic_json, file_digest, verified_path
from modport.gap_research import GapResearchHandler, RESEARCH_REPORT, validate_research


class GapResearchTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.workspace = self.root / "baseline"
        self.workspace.mkdir()
        self.blockers = [{"skill": "platform", "index": 0, "kind": "knowledge",
                          "applicable": True, "status": "unresolved"}]
        self.report = {"schema_version": 1, "gap_findings": [
            {"skill": "platform", "index": 0, "status": "unresolved", "evidence": ["exact source unavailable"]}],
            "sources": []}

    def test_research_must_cover_blockers_and_cannot_self_certify_resolution(self):
        self.assertEqual(validate_research(self.report, self.blockers, self.workspace), [])
        for rows in ([], [self.report["gap_findings"][0]] * 2,
                     [{**self.report["gap_findings"][0], "index": 1}],
                     [{**self.report["gap_findings"][0], "status": "resolved"}]):
            with self.subTest(rows=rows), self.assertRaises(ValueError):
                validate_research({**self.report, "gap_findings": rows}, self.blockers, self.workspace)

    def test_sources_need_no_hash_but_must_be_contained(self):
        source = self.workspace / ".modport/gap-research/sources/api.txt"
        source.parent.mkdir(parents=True)
        source.write_text("exact locked source declaration\n")
        entry = {"path": source.relative_to(self.workspace).as_posix(), "origin": "locked source archive/member",
                 "sha256": file_digest(source)}
        self.report["sources"] = [entry]
        self.assertEqual(validate_research(self.report, self.blockers, self.workspace), [entry["path"]])
        del entry["sha256"]
        self.assertEqual(validate_research(self.report, self.blockers, self.workspace), [entry["path"]])
        source.write_text("updated source declaration")
        self.assertEqual(validate_research(self.report, self.blockers, self.workspace), [entry["path"]])
        for field, bad in (("path", ".modport/gap-research/sources/../../escape"),
                           ("path", "/tmp/external"), ("origin", "")):
            report = copy.deepcopy(self.report)
            report["sources"][0][field] = bad
            with self.subTest(field=field), self.assertRaises(ValueError):
                validate_research(report, self.blockers, self.workspace)
        source.unlink()
        outside = self.root / "outside"
        outside.write_text("exact locked source declaration\n")
        source.symlink_to(outside)
        with self.assertRaisesRegex(ValueError, "unsafe"):
            validate_research(self.report, self.blockers, self.workspace)

    def test_handler_preserves_source_and_returns_authenticated_research(self):
        original = self.workspace / "Source.java"
        original.write_text("original\n")
        command = OperationInput("test", "gap_research", "gap_research", "test:research:1", str(self.root),
            payload={"knowledge_gap_context": {"result": {"outputs": {"unresolved_relevant_gaps": self.blockers}}}})

        def write_report(_):
            atomic_json(self.workspace / RESEARCH_REPORT, self.report)
            return OperationResult("completed")

        with patch("modport.handlers.CodexStageHandler") as agent:
            agent.return_value.side_effect = write_report
            result = GapResearchHandler()(command)
            self.assertEqual(result.status, "completed")
            self.assertTrue(verified_path(self.root, result.outputs["artifact_refs"]["gap_research"]).is_file())
            self.assertEqual(original.read_text(), "original\n")

            def tamper(command):
                result = write_report(command)
                original.write_text("changed\n")
                return result

            agent.return_value.side_effect = tamper
            result = GapResearchHandler()(command)
            self.assertEqual(result.status, "completed", result.detail)
            self.assertEqual(original.read_text(), "changed\n")

    def test_v17_research_runs_without_gap_prerequisite_and_retains_freeform_report(self):
        command = OperationInput("test", "gap_research", "gap_research", "test:research:v17", str(self.root),
                                 options={"workflow_version": 17})

        def write_report(_):
            path = self.workspace / RESEARCH_REPORT
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("No current research target was supplied.\n", encoding="utf-8")
            return OperationResult("completed")

        with patch("modport.handlers.CodexStageHandler") as agent:
            agent.return_value.side_effect = write_report
            result = GapResearchHandler()(command)
        self.assertEqual(result.status, "completed", result.detail)
        self.assertEqual(result.outputs["acceptance_status"], "unverified")
        self.assertIn("gap_research", result.outputs["artifact_refs"])


class ResearchReviewTests(unittest.TestCase):
    def test_review_uses_scoped_unresolved_gaps_over_project_catalog(self):
        """A review covers only eligible rows selected for this research pass."""
        from modport.gap_research import ResearchReviewHandler
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            atomic_json(root / 'artifacts/research.json', {'author_id': 'research-author'})
            eligible = {'gap_id': 'knowledge:platform-current', 'kind': 'knowledge',
                        'skill': 'platform', 'applicable': True, 'project_status': 'unresolved'}
            command = OperationInput(
                'run', 'research_review', 'research_review', 'run:research-review:scoped',
                str(root),
                payload={
                    'project_research_gaps': [
                        eligible,
                        {'gap_id': 'knowledge:catalog-only', 'kind': 'knowledge',
                         'skill': 'platform', 'applicable': True, 'project_status': 'unresolved'},
                    ],
                    'research_kinds': ['platform'],
                    'knowledge_gap_context': {'result': {'outputs': {
                        'unresolved_relevant_gaps': [
                            eligible,
                            {'gap_id': 'knowledge:java-current', 'kind': 'knowledge',
                             'skill': 'java', 'applicable': True, 'project_status': 'unresolved'},
                            {'gap_id': 'knowledge:platform-resolved', 'kind': 'knowledge',
                             'skill': 'platform', 'applicable': True, 'project_status': 'resolved'},
                            {'gap_id': 'knowledge:platform-inapplicable', 'kind': 'knowledge',
                             'skill': 'platform', 'applicable': False, 'project_status': 'unresolved'},
                            {'gap_id': 'knowledge:platform-license', 'kind': 'knowledge',
                             'skill': 'platform', 'applicable': True, 'project_status': 'unresolved',
                             'issue_type': 'legacy_license_header'},
                        ],
                    }}},
                },
                artifact_refs={'gap_research': {'path': 'artifacts/research.json'}},
            )
            _, _, known, requirement_known, _, _ = ResearchReviewHandler()._current_context(
                command, root
            )
        self.assertEqual(known, {'knowledge:platform-current'})
        self.assertEqual(requirement_known, {'knowledge:platform-current'})

    def test_malformed_research_context_falls_back_to_filtered_project_catalog(self):
        from modport.gap_research import ResearchReviewHandler
        eligible = {'gap_id': 'knowledge:platform-current', 'kind': 'knowledge',
                    'skill': 'platform', 'applicable': True, 'project_status': 'unresolved'}
        for context in (None, [], {'result': None}, {'result': {'outputs': []}},
                        {'result': {'outputs': {'unresolved_relevant_gaps': 'invalid'}}}):
            with self.subTest(context=context), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                atomic_json(root / 'artifacts/research.json', {'author_id': 'research-author'})
                command = OperationInput(
                    'run', 'research_review', 'research_review', 'run:research-review:fallback',
                    str(root), payload={
                        'project_research_gaps': [eligible],
                        'research_kinds': ['platform'],
                        'knowledge_gap_context': context,
                    }, artifact_refs={'gap_research': {'path': 'artifacts/research.json'}})
                _, _, known, _, _, _ = ResearchReviewHandler()._current_context(command, root)
                self.assertEqual(known, {'knowledge:platform-current'})

    def test_admin_review_with_none_context_uses_project_catalog(self):
        from modport.gap_research import ResearchReviewHandler
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            ref = {'path': 'artifacts/submission.json'}
            atomic_json(root / ref['path'], {'submission_id': 'submission-1'})
            command = OperationInput(
                'run', 'admin_review', 'admin_review', 'run:admin:fallback', str(root),
                payload={'admin_submission_ref': ref, 'admin_submission_id': 'submission-1',
                         'knowledge_gap_context': None,
                         'project_research_gaps': [{'gap_id': 'knowledge:catalog'}]},
                artifact_refs={'submission': ref})
            _, _, known, requirement_known, _, _ = ResearchReviewHandler(
                'admin_review')._current_context(command, root)
        self.assertEqual(known, {'knowledge:catalog'})
        self.assertEqual(requirement_known, {'knowledge:catalog'})

    def _run_research_review(self, report):
        from modport.gap_research import ResearchReviewHandler
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            atomic_json(root / 'artifacts/research.json', {'author_id': 'research-author'})
            ref = {'path': 'artifacts/research.json'}
            command = OperationInput('run', 'research_review', 'research_review', 'run:research-review:1', str(root),
                                     payload={'project_research_gaps': [
                                         {'gap_id': 'knowledge:gap.api', 'kind': 'knowledge',
                                          'skill': 'platform', 'applicable': True,
                                          'project_status': 'unresolved'}]},
                                     artifact_refs={'gap_research': ref})

            def agent(_):
                atomic_json(root / 'baseline/.modport/research-review.json', report)
                return OperationResult('completed')

            with patch('modport.handlers.CodexStageHandler') as handler:
                handler.return_value.side_effect = agent
                return ResearchReviewHandler()(command)

    def test_approved_review_allows_explicit_nonblocking_info_findings(self):
        findings = [{'id': 'research-review.note', 'severity': 'info', 'blocking': False,
                     'summary': 'The review records a limitation for a later stage.'}]
        report = {'schema_version': 1, 'reviewer_id': 'research-review-agent', 'review_id': 'review-1',
                  'verdict': 'approved', 'findings': findings,
                  'approved_generic_knowledge_entries': [], 'approved_gap_resolutions': [],
                  'verification_requirements': []}
        result = self._run_research_review(report)
        self.assertEqual(result.status, 'completed', result.detail)
        self.assertEqual(result.outputs['prior_findings'], findings)

    def test_review_requirements_can_reference_host_verification_obligations(self):
        report = {'schema_version': 1, 'reviewer_id': 'research-review-agent', 'review_id': 'review-1',
                  'verdict': 'approved', 'findings': [],
                  'approved_generic_knowledge_entries': [], 'approved_gap_resolutions': [],
                  'verification_requirements': [
                      {'gap_id': 'verification:gap.behavior',
                       'research_gap_id': 'verification:gap.behavior',
                       'due_stage': 'test_execute',
                       'closure_criteria': ['Run the host-owned behavior witness.']}]
                 }
        from modport.gap_research import ResearchReviewHandler
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            atomic_json(root / 'artifacts/research.json', {'author_id': 'research-author'})
            ref = {'path': 'artifacts/research.json'}
            command = OperationInput('run', 'research_review', 'research_review', 'run:research-review:1', str(root),
                                     payload={'project_research_gaps': [{'gap_id': 'knowledge:gap.api'}],
                                              'gap_obligations': [{'gap_id': 'verification:gap.behavior',
                                                                   'resolution_stage': 'test_execute'}]},
                                     artifact_refs={'gap_research': ref})
            def agent(_):
                atomic_json(root / 'baseline/.modport/research-review.json', report)
                return OperationResult('completed')
            with patch('modport.handlers.CodexStageHandler') as handler:
                handler.return_value.side_effect = agent
                result = ResearchReviewHandler()(command)
        self.assertEqual(result.status, 'completed', result.detail)
        self.assertEqual(result.outputs['verification_requirements'][0]['research_gap_id'],
                         'verification:gap.behavior')

    def test_review_rejects_conflicting_or_unchecked_stage_and_unknown_parent(self):
        base = {'gap_id': 'verification:new', 'research_gap_id': 'knowledge:gap.api',
                'due_stage': 'test_execute', 'closure_criteria': ['Exercise real behavior.']}
        for changes in ({'resolution_stage': 'target_build'}, {'resolution_stage': 'delivery'},
                        {'due_stage': 'delivery'}, {'research_gap_id': 'verification:unknown'},
                        {'gap_id': ''}):
            report = {'schema_version': 1, 'reviewer_id': 'research-review-agent', 'review_id': 'review-1',
                      'verdict': 'approved', 'findings': [], 'approved_generic_knowledge_entries': [],
                      'approved_gap_resolutions': [], 'verification_requirements': [{**base, **changes}]}
            with self.subTest(changes=changes):
                self.assertEqual(self._run_research_review(report).error_code, 'research_review_invalid')
        report['verification_requirements'] = [{**base, 'resolution_stage': 'test_execute'}]
        self.assertEqual(self._run_research_review(report).status, 'completed')
        report['verification_requirements'] = [base, base]
        self.assertEqual(self._run_research_review(report).error_code, 'research_review_invalid')

    def test_approved_review_retains_freeform_findings_without_schema_gating(self):
        for finding in (
                {'id': 'blocking', 'severity': 'warning', 'blocking': True, 'summary': 'Unresolved issue.'},
                {'id': 'missing-blocking', 'severity': 'info', 'summary': 'Classification omitted.'},
                {'id': 'non-info', 'severity': 'warning', 'blocking': False, 'summary': 'Not informational.'},
                {'id': 'non-boolean', 'severity': 'info', 'blocking': 0, 'summary': 'Not a JSON boolean.'}):
            with self.subTest(finding=finding):
                report = {'schema_version': 1, 'reviewer_id': 'research-review-agent', 'review_id': 'review-1',
                          'verdict': 'approved', 'findings': [finding],
                          'approved_generic_knowledge_entries': [], 'approved_gap_resolutions': [],
                          'verification_requirements': []}
                result = self._run_research_review(report)
                self.assertEqual(result.status, 'completed', result.detail)
                self.assertEqual(result.outputs['prior_findings'], [finding])
                self.assertEqual(result.outputs['reviewer_id'], 'research-review-agent')
                self.assertEqual(result.outputs['review_id'], 'run:research-review:1')

    def test_host_binds_review_identity_and_admin_submission_while_state_updates_stay_strict(self):
        from modport.gap_research import ResearchReviewHandler
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            atomic_json(root / 'artifacts/submission.json', {'submission_id': 'submission-1', 'author_id': 'research-author'})
            ref = {'path': 'artifacts/submission.json'}
            command = OperationInput('run', 'admin_review', 'admin_review', 'run:admin:1', str(root),
                payload={'admin_submission_ref': ref, 'admin_submission_id': 'submission-1',
                         'project_research_gaps': [{'gap_id': 'knowledge:gap.api'}]},
                artifact_refs={'submission': ref})
            report = {'schema_version': 1, 'reviewer_id': 'admin-review-agent', 'review_id': 'review-1',
                      'submission_id': 'submission-1', 'verdict': 'approved', 'findings': [],
                      'approved_generic_knowledge_entries': [], 'approved_gap_resolutions': [
                          {'gap_id': 'knowledge:gap.api', 'project_status': 'unresolved',
                           'evidence_artifact_ids': ['submission']}], 'verification_requirements': []}
            def agent(_):
                atomic_json(root / 'baseline/.modport/admin-review.json', report)
                return OperationResult('completed')
            with patch('modport.handlers.CodexStageHandler') as handler:
                handler.return_value.side_effect = agent
                result = ResearchReviewHandler('admin_review')(command)
                self.assertEqual(result.status, 'completed', result.detail)
                self.assertEqual(result.outputs['submission_id'], 'submission-1')
                for changes in ({'submission_id': 'other'}, {'reviewer_id': 'research-author'}):
                    original = copy.deepcopy(report)
                    report.update(changes)
                    result = ResearchReviewHandler('admin_review')(command)
                    self.assertEqual(result.status, 'completed', result.detail)
                    self.assertEqual(result.outputs['submission_id'], 'submission-1')
                    self.assertEqual(result.outputs['reviewer_id'], 'admin-review-agent')
                    self.assertEqual(result.outputs['review_id'], command.command_id)
                    report.clear()
                    report.update(original)
                for changes in (
                        {'approved_gap_resolutions': [
                            {'gap_id': 'unknown', 'project_status': 'resolved',
                             'evidence_artifact_ids': ['submission']}]},
                        {'approved_gap_resolutions': [
                            {'gap_id': 'knowledge:gap.api', 'project_status': 'passed',
                             'evidence_artifact_ids': ['submission']}]},
                        {'approved_gap_resolutions': [
                            {'gap_id': 'knowledge:gap.api', 'project_status': 'resolved',
                             'evidence_artifact_ids': ['invented']}]}):
                    original = copy.deepcopy(report)
                    report.update(changes)
                    self.assertEqual(
                        ResearchReviewHandler('admin_review')(command).error_code,
                        'research_review_invalid')
                    report.clear()
                    report.update(original)

    def test_research_scope_is_host_owned_and_freeform_report_never_resolves_project(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            rows = [{'gap_id': 'knowledge:api', 'kind': 'knowledge', 'applicable': True, 'project_status': 'unresolved'},
                    {'gap_id': 'verification:visual', 'kind': 'verification', 'applicable': True, 'project_status': 'unresolved'}]
            command = OperationInput('run', 'gap_research', 'gap_research', 'run:research:1', str(root),
                                     payload={'project_research_gaps': rows, 'research_kinds': ['verification']})
            report = {'schema_version': 1, 'gap_findings': [{'gap_id': 'verification:visual', 'status': 'evidence_added',
                                                           'evidence': ['Located documented rendering assertions']}],
                      'sources': [], 'generic_knowledge_entries': []}
            def agent(_):
                atomic_json(root / 'baseline' / RESEARCH_REPORT, report)
                return OperationResult('completed')
            with patch('modport.handlers.CodexStageHandler') as handler:
                handler.return_value.side_effect = agent
                result = GapResearchHandler()(command)
                self.assertEqual(result.status, 'completed', result.detail)
                self.assertEqual(result.outputs['research_kinds'], ['verification'])
                self.assertNotIn('approved_gap_resolutions', result.outputs)
                self.assertNotIn('project_research_gaps', result.outputs)
                report['gap_findings'].append({'gap_id': 'knowledge:api', 'status': 'evidence_added', 'evidence': ['extra']})
                result = GapResearchHandler()(command)
                self.assertEqual(result.status, 'completed', result.detail)
                self.assertEqual(result.outputs['research_kinds'], ['verification'])
                saved = verified_path(root, result.outputs['artifact_refs']['gap_research'])
                self.assertEqual(saved.read_text(encoding='utf-8'),
                                 (root / 'baseline' / RESEARCH_REPORT).read_text(encoding='utf-8'))
                self.assertNotIn('approved_gap_resolutions', result.outputs)
