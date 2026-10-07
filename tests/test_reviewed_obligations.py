import copy
import unittest

from modport.contracts import OperationResult
from modport.project_gaps import merge_reviewed_requirements, normalize_reviewed_requirements
from modport.research_orchestration import ResearchOrchestration
import test_early_policy as preparation


class ReviewedObligationTests(unittest.TestCase):
    def setUp(self):
        self.policy = ResearchOrchestration()
        self.original = {
            'gap_id': 'verification:visual', 'kind': 'verification',
            'resolution_stage': 'client_smoke', 'closure_criteria': ['Exercise all particle frames.'],
            'affected_tasks': ['port-particles', 'client-tests'],
            'evidence': ['authenticated baseline observation'],
            'usage_locations': [{'path': 'Particle.java', 'line': 4}],
            'question': 'Does rendering preserve behavior?', 'producer_execution_id': 'analysis:1',
            'verification_status': 'passed', 'project_status': 'passed', 'status': 'resolved',
        }
        self.app = {'project_verification_gaps': {'verification:visual': copy.deepcopy(self.original)},
                    'project_research_gaps': {'knowledge:render': {'affected_tasks': ['port-particles']}}}
        self.requirement = {'gap_id': 'verification:visual', 'research_gap_id': 'verification:visual',
                            'due_stage': 'client_smoke', 'closure_criteria': ['Exercise resource reload.']}

    def ingest(self, rows):
        outcome = OperationResult('completed', outputs={'verification_requirements': rows})
        self.policy._ingest_reviewed_requirements(self.app, outcome, 'review:1')

    def test_ingestion_keeps_scope_evidence_and_criteria_then_reanalysis_keeps_additions(self):
        self.ingest([self.requirement])
        row = self.app['project_verification_gaps']['verification:visual']
        for field in ('affected_tasks', 'evidence', 'usage_locations', 'question', 'producer_execution_id'):
            self.assertEqual(row[field], self.original[field])
        self.assertEqual(row['closure_criteria'], ['Exercise all particle frames.', 'Exercise resource reload.'])
        self.assertEqual(row['resolution_stage'], 'client_smoke')
        self.assertEqual(row['verification_status'], 'pending')
        self.assertEqual(row['requirement_reviews'][0]['execution_id'], 'review:1')
        self.policy._ingest_analysis(self.app, {'project_research_gaps': [],
                                              'project_verification_gaps': [self.original]})
        row = self.app['project_verification_gaps']['verification:visual']
        self.assertIn('Exercise resource reload.', row['closure_criteria'])
        self.assertEqual(row['requirement_reviews'][0]['execution_id'], 'review:1')

    def test_invalid_batch_never_partially_publishes(self):
        for change in ({'due_stage': 'target_build'},
                       {'resolution_stage': 'target_build'},
                       {'research_gap_id': 'knowledge:unknown'},
                       {'research_gap_id': ''}):
            before = copy.deepcopy(self.app)
            new = {**self.requirement, 'gap_id': 'verification:new'}
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.ingest([new, {**self.requirement, **change}])
            self.assertEqual(self.app, before)

    def test_only_declared_review_fields_can_enter_host_state(self):
        self.ingest([{**self.requirement, 'affected_tasks': [], 'evidence': ['forged'],
                      'producer_execution_id': 'forged', 'verification_status': 'passed',
                      'resolution_stage': 'client_smoke'}])
        row = self.app['project_verification_gaps']['verification:visual']
        self.assertEqual(row['affected_tasks'], self.original['affected_tasks'])
        self.assertEqual(row['evidence'], self.original['evidence'])
        self.assertEqual(row['producer_execution_id'], 'analysis:1')
        self.assertEqual(row['verification_status'], 'pending')

    def test_new_requirement_inherits_host_task_scope_and_replay_is_idempotent(self):
        row = {**self.requirement, 'gap_id': 'verification:new', 'research_gap_id': 'knowledge:render'}
        self.ingest([row])
        self.assertEqual(self.app['project_verification_gaps']['verification:new']['affected_tasks'],
                         ['port-particles'])
        before = copy.deepcopy(self.app)
        self.ingest([row])
        self.assertEqual(self.app, before)

    def test_analysis_retains_non_applicable_classification(self):
        row = {**self.original, 'applicable': False, 'status': 'not_applicable',
               'verification_status': 'pending'}
        self.app['project_verification_gaps'] = {}
        self.policy._ingest_analysis(self.app, {'project_verification_gaps': [row]})
        saved = self.app['project_verification_gaps']['verification:visual']
        self.assertFalse(saved['applicable'])
        self.assertEqual(saved['status'], 'not_applicable')
        self.policy._ingest_analysis(self.app, {'project_verification_gaps': [
            {**row, 'applicable': True, 'status': 'unresolved'}]})
        saved = self.app['project_verification_gaps']['verification:visual']
        self.assertTrue(saved['applicable'])
        self.assertEqual(saved['status'], 'unresolved')
        self.policy._ingest_analysis(self.app, {'project_verification_gaps': [row]})
        self.assertTrue(self.app['project_verification_gaps']['verification:visual']['applicable'])

    def test_early_and_background_reviews_preserve_obligations_through_reassessment(self):
        for background in (False, True):
            with self.subTest(background=background):
                policy = preparation.EarlyPolicyTests()
                policy.setUp()
                self.addCleanup(policy.doCleanups)
                if background:
                    policy.start_background_research()
                else:
                    policy.tick()
                    for stage in ('source', 'background', 'preparation', 'environment',
                                  'project_init', 'baseline_build', 'skill_lookup', 'skill_publish', 'mod_scan'):
                        policy.complete(stage)
                        policy.tick()
                    policy.complete('mod_analysis', status='failed', error_code='relevant_skill_gap', outputs={
                        'research_repairable': True,
                        'unresolved_relevant_gaps': [{'skill': 'platform', 'index': 0}]})
                    policy.tick()
                policy.snapshot['application_state']['project_verification_gaps'] = {
                    self.original['gap_id']: copy.deepcopy(self.original)}
                policy.complete('gap_research')
                policy.tick()
                policy.complete('research_review', outputs={'verdict': 'approved',
                    'verification_requirements': [self.requirement]})
                policy.tick()
                policy.complete('mod_analysis', outputs={'project_verification_gaps': [self.original]})
                policy.tick()
                row = policy.snapshot['application_state']['project_verification_gaps'][self.original['gap_id']]
                self.assertEqual(row['closure_criteria'], ['Exercise all particle frames.', 'Exercise resource reload.'])
                self.assertEqual(row['affected_tasks'], self.original['affected_tasks'])
                self.assertEqual(row['evidence'], self.original['evidence'])
                self.assertEqual(row['resolution_stage'], 'client_smoke')

    def test_existing_research_binding_cannot_be_replaced(self):
        self.app['project_verification_gaps']['verification:visual']['research_gap_id'] = 'knowledge:render'
        before = copy.deepcopy(self.app)
        with self.assertRaisesRegex(ValueError, 'binding cannot change'):
            self.ingest([self.requirement])
        self.assertEqual(self.app, before)

    def test_opt_in_normalization_merges_same_parent_and_retains_each_source(self):
        requirements = [
            {**self.requirement, 'closure_criteria': ['Exercise resource reload.']},
            {**self.requirement, 'closure_criteria': [
                'Exercise resource reload.', 'Exercise reconnect rendering.']},
        ]
        before = copy.deepcopy(requirements)
        merged = merge_reviewed_requirements(
            self.app['project_verification_gaps'], requirements, 'review:duplicates',
            self.app['project_research_gaps'], normalize_duplicates=True)
        row = merged['verification:visual']
        self.assertEqual(requirements, before)
        self.assertEqual(row['closure_criteria'], [
            'Exercise all particle frames.',
            'Exercise resource reload.',
            'Exercise reconnect rendering.',
        ])
        self.assertEqual(row['evidence'], self.original['evidence'])
        self.assertEqual(row['usage_locations'], self.original['usage_locations'])
        self.assertEqual([review['source_index'] for review in row['requirement_reviews']],
                         [0, 1])
        self.assertEqual([review['closure_criteria'] for review in row['requirement_reviews']], [
            ['Exercise resource reload.'],
            ['Exercise resource reload.', 'Exercise reconnect rendering.'],
        ])

    def test_parent_collision_gets_stable_host_ids_and_preserves_original_id(self):
        research = {
            'knowledge:a': {'affected_tasks': ['task-a']},
            'knowledge:b': {'affected_tasks': ['task-b']},
        }
        requirements = [
            {'gap_id': 'verification:shared', 'research_gap_id': 'knowledge:b',
             'due_stage': 'test_execute', 'closure_criteria': ['Check B.']},
            {'gap_id': 'verification:shared', 'research_gap_id': 'knowledge:a',
             'due_stage': 'test_execute', 'closure_criteria': ['Check A.']},
        ]
        first = normalize_reviewed_requirements({}, requirements, research)
        second = normalize_reviewed_requirements({}, list(reversed(requirements)), research)
        first_ids = {row['research_gap_id']: row['gap_id'] for row in first}
        second_ids = {row['research_gap_id']: row['gap_id'] for row in second}
        self.assertEqual(first_ids, second_ids)
        self.assertEqual(first_ids['knowledge:a'], 'verification:shared')
        self.assertTrue(first_ids['knowledge:b'].startswith(
            'verification:shared:parent-'))

        merged = merge_reviewed_requirements(
            {}, requirements, 'review:collision', research, normalize_duplicates=True)
        b = merged[first_ids['knowledge:b']]
        self.assertEqual(b['source_gap_id'], 'verification:shared')
        self.assertEqual(b['affected_tasks'], ['task-b'])
        self.assertEqual(b['requirement_reviews'][0]['source_gap_id'],
                         'verification:shared')
        before_replay = copy.deepcopy(merged)
        replay = merge_reviewed_requirements(
            merged, requirements, 'review:collision', research, normalize_duplicates=True)
        self.assertEqual(replay, before_replay)

    def test_normalization_keeps_parent_stage_and_identity_constraints(self):
        research = {'knowledge:a': {}, 'knowledge:b': {}}
        base = {'gap_id': 'verification:shared', 'research_gap_id': 'knowledge:a',
                'due_stage': 'test_execute', 'closure_criteria': ['Check A.']}
        invalid_batches = [
            [base, {**base, 'due_stage': 'client_smoke'}],
            [{**base, 'research_gap_id': 'knowledge:unknown'}],
            [{**base, 'id': 'verification:different'}],
        ]
        for rows in invalid_batches:
            with self.subTest(rows=rows), self.assertRaises(ValueError):
                normalize_reviewed_requirements({}, rows, research)
        existing = {'verification:shared': {
            'gap_id': 'verification:shared', 'research_gap_id': 'knowledge:a',
            'resolution_stage': 'client_smoke', 'closure_criteria': ['Existing check.']}}
        with self.assertRaisesRegex(ValueError, 'stage cannot change'):
            merge_reviewed_requirements(
                existing, [base], 'review:stage', research, normalize_duplicates=True)
