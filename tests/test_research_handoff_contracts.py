"""Prompt-derived artifacts exercise the research producer/consumer boundary."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from modport.analysis_contract import GENERIC_ENTRIES_SCHEMA, HOST_GAP_FIELDS
from modport.analysis_stages import AnalysisStageHandler, validate_analysis
from modport.contracts import OperationInput, OperationResult
from modport.evidence import atomic_json
from modport.gap_research import ResearchReviewHandler, GapResearchHandler
from modport.gap_review import GAP_REVIEW_PROMPT, _analysis_gaps, _validate_report
from modport.project_gaps import project_gap_rows
from modport.research_orchestration import ResearchOrchestration


class ResearchHandoffTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.baseline = self.root / 'baseline'
        self.baseline.mkdir()
        (self.baseline / 'Source.java').write_text('Input event;\n')
        self.row = {'entry_id': 'gap.api', 'gap_id': 'knowledge:gap.api', 'skill': 'platform', 'index': 0,
                    'kind': 'knowledge', 'status': 'unresolved', 'applicable': True,
                    'resolution_stage': 'mod_analysis', 'closure_criteria': ['Read declaration'],
                    'affected_tasks': ['port'], 'question': 'Which declaration?',
                    'existing_answer': 'Source Input', 'missing_information': 'Target Input',
                    'evidence': ['Source.java:1'], 'usage_locations': [{'path': 'Source.java', 'line': 1}]}
        self.scan = {'skills': {skill: {'report': {'findings': [], 'known_gaps': [{'id': 'gap.api'}]}}
                                for skill in ('platform', 'java')}}
        self.logs = {'log_path': 'logs/agent.log', 'last_message_path': 'logs/last.txt',
                     'artifact_refs': {'raw_report': {'path': 'artifacts/raw.json'}}}
        atomic_json(self.root / 'artifacts/raw.json', {'author_id': 'author'})

    def command(self, stage, **kwargs):
        return OperationInput('run', stage, stage, 'run:' + stage + ':1', str(self.root),
                              options={'workflow_version': 13}, **kwargs)

    def analysis(self, rows):
        return {'schema_version': 2, 'candidates': [], 'gap_assessments': rows}

    def test_actual_analysis_prompt_catalog_produces_both_colliding_rows(self):
        atomic_json(self.root / 'artifacts/scan.json', self.scan)
        command = self.command('mod_analysis', artifact_refs={'mod_scan_report': {'path': 'artifacts/scan.json'}})
        def factory(prompt, **kwargs):
            catalog = json.loads(prompt.split('Exact catalog identities (choose knowledge or verification gap_id): ')[1])
            self.assertEqual({row['skill'] for row in catalog}, {'platform', 'java'})
            rows = [{**self.row, 'skill': item['skill'], 'index': item['index'],
                     'entry_id': item['entry_id'], 'gap_id': item['gap_ids'][0]} for item in catalog]
            def invoke(command):
                atomic_json(self.baseline / '.modport/mod-analysis.json', self.analysis(rows))
                return OperationResult('completed', outputs=self.logs)
            return invoke
        with patch('modport.handlers.CodexStageHandler', side_effect=factory):
            result = AnalysisStageHandler('mod_analysis', ('.modport/mod-analysis.json',))(command)
        self.assertEqual(result.error_code, 'relevant_skill_gap', result.detail)
        rows = result.outputs['project_research_gaps']
        self.assertEqual({row['gap_id'] for row in rows}, {'knowledge:platform:gap.api', 'knowledge:java:gap.api'})
        bad = deepcopy(rows)
        for row in bad:
            for field in HOST_GAP_FIELDS:
                row.pop(field, None)
            row['gap_id'] = 'knowledge:gap.api'
        with self.assertRaisesRegex(ValueError, 'colliding catalog'):
            validate_analysis(self.analysis(bad), self.scan, self.baseline, strict=True)

    def test_host_fields_rejected_and_direct_ingestion_cannot_forge_review(self):
        scan = {'skills': {'platform': self.scan['skills']['platform']}}
        for field in HOST_GAP_FIELDS:
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, 'host-owned'):
                validate_analysis(self.analysis([{**self.row, field: 'forged'}]), scan, self.baseline, strict=True)
        forged = {**self.row, 'project_status': 'bypassed', 'review_execution_id': 'forged',
                  'resolution': {'project_status': 'resolved'}}
        app = {}
        host = ResearchOrchestration()
        host._ingest_analysis(app, {'project_research_gaps': [forged]})
        saved = app['project_research_gaps'][self.row['gap_id']]
        self.assertEqual(saved['project_status'], 'unresolved')
        self.assertNotIn('review_execution_id', saved)
        host._ingest_analysis(app, {'project_research_gaps': []})
        self.assertEqual(len(app['unresolved_knowledge_gaps']), 1)

    def run_review(self, document, *, payload=None):
        command = self.command('research_review', payload=payload or {'project_research_gaps': [self.row]},
                               artifact_refs={'gap_research': {'path': 'artifacts/raw.json'}})
        def factory(prompt, **kwargs):
            self.assertIn(GENERIC_ENTRIES_SCHEMA, prompt)
            self.assertIn('report is free-form text', prompt)
            self.assertIn('fenced JSON control block', prompt)
            self.assertIn('Do not echo reviewer IDs, submission IDs, schema versions or hashes', prompt)
            self.assertIn('approved_gap_resolutions', prompt)
            def invoke(command):
                path = self.baseline / '.modport/research-review.json'
                path.parent.mkdir(parents=True, exist_ok=True)
                if isinstance(document, str):
                    path.write_text(document, encoding='utf-8')
                else:
                    atomic_json(path, document)
                return OperationResult('completed', outputs=self.logs)
            return invoke
        with patch('modport.handlers.CodexStageHandler', side_effect=factory):
            return ResearchReviewHandler()(command)

    def review_document(self):
        return {'schema_version': 1, 'reviewer_id': 'research-review-agent', 'review_id': 'r1',
                'verdict': 'approved', 'findings': [], 'approved_generic_knowledge_entries': {},
                'approved_gap_resolutions': [{'gap_id': self.row['gap_id'], 'project_status': 'resolved',
                                              'evidence_artifact_ids': ['gap_research']}],
                'verification_requirements': [{'gap_id': 'verification:gap.api', 'research_gap_id': self.row['gap_id'],
                                               'due_stage': 'test_execute', 'closure_criteria': ['Exercise Input behavior']}]}

    def test_review_to_host_to_reanalysis_transfers_knowledge_without_losing_obligation(self):
        host, app = ResearchOrchestration(), {}
        host._ingest_analysis(app, {'project_research_gaps': [self.row]})
        review = self.run_review(self.review_document())
        self.assertEqual(review.status, 'completed', review.detail)
        host._apply_gap_resolutions(app, review.outputs['approved_gap_resolutions'], 'review:1')
        host._ingest_reviewed_requirements(app, review, 'review:1')
        verification = {**self.row, 'kind': 'verification', 'gap_id': 'verification:gap.api',
                        'resolution_stage': 'test_execute'}
        host._ingest_analysis(app, {'project_research_gaps': [], 'project_verification_gaps': [verification]})
        self.assertEqual(app['unresolved_knowledge_gaps'], [])
        self.assertEqual(app['project_research_gaps'][self.row['gap_id']]['review_execution_id'], 'review:1')
        obligation = app['project_verification_gaps']['verification:gap.api']
        self.assertEqual(obligation['verification_status'], 'pending')
        self.assertEqual(obligation['research_gap_id'], self.row['gap_id'])
        self.assertIn('Exercise Input behavior', obligation['closure_criteria'])
        for status in ('unresolved', 'not_applicable'):
            bad = self.review_document()
            bad['approved_gap_resolutions'][0]['project_status'] = status
            result = self.run_review(bad)
            self.assertEqual(result.error_code, 'research_review_invalid')
            self.assertEqual(result.outputs, self.logs)
        bad = self.review_document()
        bad['approved_gap_resolutions'] = []
        self.assertEqual(self.run_review(bad).error_code, 'research_review_invalid')

    def test_rejected_freeform_review_retains_reasoning_without_findings_schema(self):
        document = (
            'The target declaration is not present in the supplied evidence.\n'
            'Cite the exact target declaration before approval.\n'
            'MODPORT_DECISION: rejected\n'
        )
        outcome = self.run_review(document)
        self.assertEqual(outcome.status, 'completed', outcome.detail)
        self.assertEqual(outcome.outputs['verdict'], 'rejected')
        self.assertEqual(outcome.outputs['approved_gap_resolutions'], [])
        self.assertEqual(outcome.outputs['verification_requirements'], [])
        self.assertEqual(outcome.outputs['prior_findings'], [{'report': document}])
        self.assertEqual((self.baseline / '.modport/research-review.json').read_text(), document)
        self.assertEqual(outcome.outputs['artifact_refs'], self.logs['artifact_refs'])

    def test_freeform_research_keeps_inner_logs_and_raw_report_ref(self):
        command = self.command('gap_research', payload={'project_research_gaps': [self.row], 'research_kinds': ['platform']})
        report = 'Target API remains unknown; exact locked declaration was unavailable.\n'
        def agent(command):
            path = self.baseline / '.modport/gap-research/report.md'
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(report, encoding='utf-8')
            return OperationResult('completed', outputs=self.logs)
        with patch('modport.handlers.CodexStageHandler') as factory:
            factory.return_value.side_effect = agent
            outcome = GapResearchHandler()(command)
        self.assertEqual(outcome.status, 'completed', outcome.detail)
        self.assertEqual(outcome.outputs['log_path'], self.logs['log_path'])
        self.assertEqual(outcome.outputs['last_message_path'], self.logs['last_message_path'])
        ref = outcome.outputs['artifact_refs']['gap_research']
        self.assertEqual((self.root / ref['path']).read_text(encoding='utf-8'), report)
        self.assertNotIn('approved_gap_resolutions', outcome.outputs)

    def test_qualified_reanalysis_retains_short_host_identity_with_explicit_alias(self):
        host, app = ResearchOrchestration(), {}
        host._ingest_analysis(app, {'project_research_gaps': [self.row]})
        qualified = {**self.row, 'gap_id': 'knowledge:platform:gap.api'}
        other = {**self.row, 'skill': 'java', 'gap_id': 'knowledge:java:gap.api'}
        host._ingest_analysis(app, {'project_research_gaps': [qualified, other]})
        self.assertEqual(set(app['project_research_gaps']), {'knowledge:gap.api', 'knowledge:java:gap.api'})
        self.assertEqual(app['gap_identity_aliases'], {'knowledge:platform:gap.api': 'knowledge:gap.api'})
        host._apply_gap_resolutions(app, [{'gap_id': qualified['gap_id'], 'project_status': 'resolved'}], 'review:1')
        self.assertEqual(app['project_research_gaps']['knowledge:gap.api']['project_status'], 'resolved')
        self.assertEqual(app['project_research_gaps']['knowledge:java:gap.api']['project_status'], 'unresolved')
        self.assertIn('exact current gap_id', GAP_REVIEW_PROMPT)
        report = {'schema_version': 1, 'reviewer_id': 'gap-review-agent', 'review_id': 'final',
                  'rubric_id': 'rubric', 'rubric_version': 1, 'verdict': 'approved', 'findings': [],
                  'gap_resolutions': [{'gap_id': row['gap_id'], 'status': 'resolved',
                                       'evidence': ['Checked declaration'], 'artifact_ids': ['source']}
                                      for row in (qualified, other)]}
        command = self.command('gap_review', payload={'gap_identity_aliases': app['gap_identity_aliases']},
                               artifact_refs={'source': {'path': 'artifacts/raw.json'}})
        self.assertEqual(_validate_report(report, _analysis_gaps(self.analysis([qualified, other])), command,
                                         self.root, {'rubric_id': 'rubric', 'rubric_version': 1}), [])

    def test_plan_and_final_review_postvalidation_keep_inner_outputs(self):
        from dataclasses import replace
        from modport.gap_planning import GapPlanHandler, GapPlanReviewHandler
        from modport.gap_review import GapReviewHandler
        from test_gap_planning import GapPlanTests
        from test_gap_review import GapReviewTests
        fixture = GapPlanTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        plan = fixture.execute(fixture.command)
        for stage, handler in (('gap_plan', GapPlanHandler()), ('gap_plan_review', GapPlanReviewHandler())):
            command = replace(fixture.command, stage_id=stage, command_id=stage + ':new',
                              artifact_refs={**fixture.command.artifact_refs, **plan.outputs['artifact_refs']})
            def agent(command):
                path = fixture.root / ('worktree/.modport/' + stage.replace('_', '-') + '.json')
                atomic_json(path, {'schema_version': 0})
                return OperationResult('completed', outputs=self.logs)
            with patch('modport.handlers.CodexStageHandler') as factory:
                factory.return_value.side_effect = agent
                outcome = handler(command)
            if stage == 'gap_plan':
                self.assertEqual(outcome.status, 'completed', outcome.detail)
                raw_ref = outcome.outputs['artifact_refs']['gap_plan']
                self.assertEqual(json.loads((fixture.root / raw_ref['path']).read_text()),
                                 {'schema_version': 0})
                self.assertNotIn('approved_gap_resolutions', outcome.outputs)
            else:
                self.assertEqual(outcome.error_code, stage + '_invalid')
                self.assertEqual(outcome.outputs, self.logs)
        final = GapReviewTests()
        final.setUp()
        self.addCleanup(final.doCleanups)
        raw = {**final.report, 'schema_version': 0, 'reviewer_id': 'spoofed',
               'review_id': 'spoofed', 'rubric_id': 'spoofed', 'rubric_version': 999}
        def agent(command):
            atomic_json(final.worktree / '.modport/gap-review.json', raw)
            snapshot = final.artifact('raw-gap-review.json', raw)
            return OperationResult('completed', outputs={**self.logs, 'artifact_refs': {
                **self.logs['artifact_refs'],
                'stage_output:gap_review:.modport/gap-review.json': snapshot}})
        with patch('modport.handlers.CodexStageHandler') as factory:
            factory.return_value.side_effect = agent
            outcome = GapReviewHandler()(final.command)
        self.assertEqual(outcome.status, 'completed', outcome.detail)
        self.assertEqual(outcome.outputs['log_path'], self.logs['log_path'])
        decision = json.loads((final.root / outcome.outputs['artifact_refs']['gap_review']['path']).read_text())
        self.assertEqual(decision['schema_version'], 1)
        self.assertEqual(decision['reviewer_id'], 'gap-review-agent')
        self.assertEqual(decision['review_id'], final.command.command_id)
        self.assertEqual(json.loads(decision['raw_report']), raw)

    def test_verification_qualification_retains_reviewed_scope_and_host_key(self):
        host, app = ResearchOrchestration(), {}
        row = {**self.row, 'kind': 'verification', 'gap_id': 'verification:gap.api',
               'resolution_stage': 'test_execute'}
        host._ingest_analysis(app, {'project_verification_gaps': [row]})
        app['project_verification_gaps'][row['gap_id']]['requirement_reviews'] = [{'execution_id': 'review:1'}]
        qualified = {**row, 'gap_id': 'verification:platform:gap.api'}
        java = {**row, 'gap_id': 'verification:java:gap.api', 'skill': 'java'}
        host._ingest_analysis(app, {'project_verification_gaps': [qualified, java]})
        self.assertEqual(set(app['project_verification_gaps']), {'verification:gap.api', 'verification:java:gap.api'})
        self.assertEqual(app['project_verification_gaps'][row['gap_id']]['requirement_reviews'],
                         [{'execution_id': 'review:1'}])

    def test_short_and_qualified_spellings_cannot_duplicate_one_catalog_gap(self):
        scan = {'skills': {'platform': self.scan['skills']['platform']}}
        with self.assertRaisesRegex(ValueError, 'duplicate gap kind'):
            validate_analysis(self.analysis([self.row, {**self.row, 'gap_id': 'knowledge:platform:gap.api'}]),
                              scan, self.baseline, strict=True)

    def test_collision_then_unique_shorthand_never_reassigns_historical_owner(self):
        host, app = ResearchOrchestration(), {}
        host._ingest_analysis(app, {'project_research_gaps': [self.row]})
        host._apply_gap_resolutions(app, [{'gap_id': self.row['gap_id'], 'project_status': 'resolved'}], 'platform-review')
        platform = {**self.row, 'gap_id': 'knowledge:platform:gap.api'}
        java = {**self.row, 'skill': 'java', 'gap_id': 'knowledge:java:gap.api'}
        host._ingest_analysis(app, {'project_research_gaps': [platform, java]})
        host._apply_gap_resolutions(app, [{'gap_id': java['gap_id'], 'project_status': 'resolved'}], 'java-review')
        before = deepcopy(app['project_research_gaps'])
        with self.assertRaisesRegex(ValueError, 'historical skill entry'):
            host._ingest_analysis(app, {'project_research_gaps': [{**java, 'gap_id': self.row['gap_id']}]})
        self.assertEqual(app['project_research_gaps'], before)
        host._ingest_analysis(app, {'project_research_gaps': [java]})
        self.assertEqual(app['project_research_gaps'], before)
        self.assertEqual(app['gap_identity_aliases'], {platform['gap_id']: self.row['gap_id']})

    def test_colon_entry_catalog_has_no_cross_skill_generated_id_collision(self):
        from modport.analysis_stages import analysis_catalog
        self.scan['skills']['platform']['report']['known_gaps'].append({'id': 'java:gap.api'})
        catalog = analysis_catalog(self.scan)
        ids = [identity for row in catalog for identity in row['gap_ids']]
        self.assertEqual(len(ids), len(set(ids)))
        rows = [{**self.row, 'skill': row['skill'], 'index': row['index'], 'entry_id': row['entry_id'],
                 'gap_id': row['gap_ids'][0]} for row in catalog]
        self.assertEqual(len(validate_analysis(self.analysis(rows), self.scan, self.baseline, strict=True)), 3)

    def test_research_disposition_extras_cannot_enter_gap_plan_host_path(self):
        document = self.review_document()
        document['verification_requirements'] = []
        document['approved_gap_resolutions'][0].update(action='compatibility_layer',
            verification_requirements=[{'id': 'injected', 'closure_criteria': [], 'resolution_stage': 'invalid'}])
        outcome = self.run_review(document)
        self.assertEqual(outcome.error_code, 'research_review_invalid')
        self.assertEqual(outcome.outputs, self.logs)
        host, app = ResearchOrchestration(), {}
        host._ingest_analysis(app, {'project_research_gaps': [self.row]})
        before = deepcopy(app)
        with self.assertRaisesRegex(ValueError, 'cannot authorize gap-plan'):
            host._apply_gap_resolutions(app, document['approved_gap_resolutions'], 'review:forged')
        self.assertEqual(app, before)

    def test_research_and_plan_nonobject_json_fail_with_inner_evidence(self):
        from dataclasses import replace
        from modport.gap_planning import GapPlanHandler, GapPlanReviewHandler
        from test_gap_planning import GapPlanTests
        fixture = GapPlanTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        plan = fixture.execute(fixture.command)
        for invalid in ([], None, 42, 'not an object'):
            self.assertEqual(self.run_review(invalid).outputs, self.logs)
            for stage, handler in (('gap_plan', GapPlanHandler()), ('gap_plan_review', GapPlanReviewHandler())):
                command = replace(fixture.command, stage_id=stage, command_id=stage + ':invalid',
                                  artifact_refs={**fixture.command.artifact_refs, **plan.outputs['artifact_refs']})
                def agent(command):
                    path = fixture.root / ('worktree/.modport/' + stage.replace('_', '-') + '.json')
                    atomic_json(path, invalid)
                    return OperationResult('completed', outputs=self.logs)
                with patch('modport.handlers.CodexStageHandler') as factory:
                    factory.return_value.side_effect = agent
                    outcome = handler(command)
                if stage == 'gap_plan':
                    self.assertEqual(outcome.status, 'completed', outcome.detail)
                    raw_ref = outcome.outputs['artifact_refs']['gap_plan']
                    self.assertEqual(json.loads((fixture.root / raw_ref['path']).read_text()), invalid)
                    self.assertNotIn('approved_gap_resolutions', outcome.outputs)
                else:
                    self.assertEqual(outcome.error_code, stage + '_invalid')
                    self.assertEqual(outcome.outputs, self.logs)

    def test_historical_colon_short_id_cannot_collide_with_new_qualified_identity(self):
        from modport.analysis_stages import analysis_catalog
        from modport.analysis_contract import encoded_gap_id
        host, app = ResearchOrchestration(), {}
        old = {**self.row, 'entry_id': 'java:gap.api', 'gap_id': 'knowledge:java:gap.api'}
        host._ingest_analysis(app, {'project_research_gaps': [old]})
        host._apply_gap_resolutions(app, [{'gap_id': old['gap_id'], 'project_status': 'resolved'}], 'old-review')
        self.scan['skills']['platform']['report']['known_gaps'] = [{'id': 'java:gap.api'}]
        catalog = analysis_catalog(self.scan, list(app['project_research_gaps'].values()))
        rows = [{**self.row, 'skill': item['skill'], 'index': item['index'], 'entry_id': item['entry_id'],
                 'gap_id': item['gap_ids'][0]} for item in catalog]
        validate_analysis(self.analysis(rows), self.scan, self.baseline, strict=True)
        host._ingest_analysis(app, {'project_research_gaps': rows})
        current_java = encoded_gap_id('knowledge', 'java', 'gap.api')
        self.assertEqual(set(app['project_research_gaps']), {old['gap_id'], current_java})
        self.assertEqual(app['project_research_gaps'][old['gap_id']]['review_execution_id'], 'old-review')
        self.assertEqual(app['project_research_gaps'][current_java]['skill'], 'java')
        self.assertEqual(app['gap_identity_aliases'], {'knowledge:platform:java:gap.api': old['gap_id']})
        # Repeated catalogs keep the encoded identity and do not repurpose either owner.
        again = analysis_catalog(self.scan, list(app['project_research_gaps'].values()))
        self.assertEqual(again, catalog)

    def test_actual_catalog_prompt_preserves_historical_colon_owners_across_cycles(self):
        from modport.analysis_contract import encoded_gap_id
        host, app = ResearchOrchestration(), {}
        old = {**self.row, 'entry_id': 'java:gap.api', 'gap_id': 'knowledge:java:gap.api'}
        host._ingest_analysis(app, {'project_research_gaps': [old]})
        host._apply_gap_resolutions(app, [{'gap_id': old['gap_id'], 'project_status': 'resolved'}], 'historical-review')
        self.scan['skills']['platform']['report']['known_gaps'] = [{'id': 'java:gap.api'}]
        for cycle, include_platform in enumerate((True, False, True), 1):
            scan = deepcopy(self.scan)
            if not include_platform:
                scan['skills'].pop('platform')
            atomic_json(self.root / 'artifacts/scan.json', scan)
            command = self.command('mod_analysis',
                payload={'project_research_gaps': list(app['project_research_gaps'].values())},
                artifact_refs={'mod_scan_report': {'path': 'artifacts/scan.json'}})
            def factory(prompt, **kwargs):
                catalog = json.loads(prompt.split('Exact catalog identities (choose knowledge or verification gap_id): ')[1])
                def agent(command):
                    rows = [{**self.row, 'skill': item['skill'], 'entry_id': item['entry_id'],
                             'index': item['index'], 'gap_id': item['gap_ids'][0]} for item in catalog]
                    atomic_json(self.baseline / '.modport/mod-analysis.json', self.analysis(rows))
                    return OperationResult('completed', outputs=self.logs)
                return agent
            with patch('modport.handlers.CodexStageHandler', side_effect=factory):
                outcome = AnalysisStageHandler('mod_analysis', ('.modport/mod-analysis.json',))(command)
            self.assertEqual(outcome.error_code, 'relevant_skill_gap', outcome.detail)
            host._ingest_analysis(app, outcome.outputs)
            self.assertEqual(set(app['project_research_gaps']),
                             {old['gap_id'], encoded_gap_id('knowledge', 'java', 'gap.api')})
            self.assertEqual(app['project_research_gaps'][old['gap_id']]['review_execution_id'], 'historical-review')
