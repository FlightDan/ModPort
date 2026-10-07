import copy
import unittest

from modport.research_policy import initialize_research, record_dispatch, remaining, eligible_kinds
import test_early_policy as preparation


class ResearchBudgetTests(unittest.TestCase):
    def test_new_pair_initial_plus_optional_supplement_is_total_two(self):
        app = {}
        initialize_research(app, {'missing_kinds': ['platform'], 'research_origins': {'platform': 'new', 'java': 'existing'}})
        record_dispatch(app, 'platform_diff', 'first', ['platform'])
        self.assertEqual(remaining(app, 'platform'), 1)
        record_dispatch(app, 'gap_research', 'supplement', ['platform', 'java'])
        self.assertEqual(remaining(app, 'platform'), 0)
        self.assertEqual(remaining(app, 'java'), 0)
        with self.assertRaises(ValueError):
            record_dispatch(app, 'gap_research', 'third', ['platform'])

    def test_restore_relookup_and_duplicate_dispatch_never_refill(self):
        app = {}
        initialize_research(app, {})
        record_dispatch(app, 'gap_research', 'one', ['java'])
        restored = copy.deepcopy(app)
        initialize_research(restored, {'missing_kinds': ['java']})
        record_dispatch(restored, 'gap_research', 'one', ['java'])
        self.assertEqual(restored['research_budget']['java']['dispatched'], 1)
        self.assertEqual(remaining(restored, 'java'), 0)
        self.assertEqual(restored['research_budget']['java']['origin'], 'existing')

    def test_unused_and_verification_gaps_never_consume_research(self):
        app = {}
        initialize_research(app, {})
        rows = [{'skill': 'platform', 'kind': 'verification', 'applicable': True},
                {'skill': 'java', 'kind': 'knowledge', 'applicable': False}]
        self.assertEqual(eligible_kinds(app, rows), [])


class ProjectSchedulingTests(unittest.TestCase):
    def setUp(self):
        self.policy = preparation.EarlyPolicyTests()
        self.policy.setUp()
        self.addCleanup(self.policy.doCleanups)

    def tick(self):
        operations = self.policy.tick()
        for operation in operations:
            if operation['kind'] == 'wait':
                self.policy.snapshot['waits'][operation['wait_id']] = {'state': 'open', 'payload': operation['payload']}
            elif operation['kind'] == 'release_wait':
                self.policy.snapshot['waits'][operation['wait_id']]['state'] = 'released'
        return operations

    def exhaust_background(self):
        self.policy.start_local_coders()
        self.policy.complete('gap_research', status='failed', error_code='gap_research_invalid')
        self.tick()
        self.assertIn('gap_plan', self.policy.snapshot['tasks'])
        self.assertNotIn('administrator_wait', self.policy.snapshot['application_state'])
        self.assertEqual(len(self.policy.snapshot['tasks']['gap_research']['attempts']), 1)

    def review_plan(self, action):
        self.policy.complete('gap_plan')
        self.tick()
        requirements = [] if action == 'wait_admin' else [{'id': 'compat', 'closure_criteria': ['original behavior preserved'], 'resolution_stage': 'test_execute'}]
        self.policy.complete('gap_plan_review', outputs={'verdict': 'approved', 'approved_gap_resolutions': [
            {'gap_id': 'platform:0', 'action': action, 'alternative_id': 'compat-v1', 'affected_tasks': ['a'],
             'verification_requirements': requirements}]})
        self.tick()

    def test_budget_exhaustion_plans_beside_independent_coder_and_bypass_releases_only_knowledge(self):
        self.exhaust_background()
        self.review_plan('compatibility_layer')
        state = self.policy.snapshot
        self.assertIn('coder.g1.a', state['tasks'])
        app = state['application_state']
        self.assertEqual(app['project_research_gaps']['platform:0']['project_status'], 'bypassed')
        self.assertEqual(app['project_verification_gaps']['verify:platform:0:compat']['project_status'], 'pending')
        self.assertEqual(app['research_budget']['platform']['dispatched'], 1)
        self.assertEqual(len(app['project_research_gaps']['platform:0']['attempted_alternatives']), 1)
        self.assertNotIn('administrator_wait', app)

    def test_wait_only_after_unrelated_work_settles_and_same_plan_not_repeated(self):
        self.exhaust_background()
        self.review_plan('wait_admin')
        self.assertNotIn('administrator_wait', self.policy.snapshot['application_state'])
        self.policy.complete('coder.g1.b')
        self.tick()
        # A due asynchronous supervisor is unrelated work and must settle
        # before the host opens the administrator wait.
        for task_id, task in list(self.policy.snapshot['tasks'].items()):
            command = task['attempts'][-1]['command']['payload']
            if command['stage_id'] == 'supervisor' and task['attempts'][-1]['state'] != 'succeeded':
                packet = command['payload']['supervision_packet']
                self.policy.complete(task_id, outputs={'supervisor_decision': {
                    'schema_version': 1, 'decision': 'continue', 'reason': 'No intervention',
                    'evidence_execution_ids': packet['evidence_execution_ids'],
                    'process_improvements': []}})
        self.tick()
        app = self.policy.snapshot['application_state']
        self.assertIn('administrator_wait', app)
        before = app['agent_assignments']
        self.tick()
        self.assertEqual(len(self.policy.snapshot['tasks']['gap_plan']['attempts']), 1)
        self.assertEqual(self.policy.snapshot['application_state']['agent_assignments'], before)

    def test_admin_wait_has_own_deadline_and_pauses_execution_clock(self):
        operations = self.policy.operations
        operations.clock = lambda: 50
        app = operations._new_application()
        app['administrator_wait'] = {'wait_id': 'administrator:1', 'started_at': 10, 'deadline_epoch': 100}
        self.assertEqual(operations._effective_deadline({'deadline_epoch': 20}, app), 60)
        snapshot = {'waits': {'administrator:1': {'state': 'open'}}}
        released = operations._release_administrator_wait(snapshot, app)
        self.assertEqual(released, [{'kind': 'release_wait', 'wait_id': 'administrator:1'}])
        self.assertEqual(app['administrator_wait_seconds'], 40)
        self.assertEqual(operations._effective_deadline({'deadline_epoch': 20}, app), 60)

    def test_recovery_deadline_is_durable_and_not_rebased_to_current_clock(self):
        operations = self.policy.operations
        operations.clock = lambda: 500
        app = operations._new_application()
        app['recovery_deadline_epoch'] = 400
        self.assertEqual(operations._effective_deadline({'deadline_epoch': 20}, app), 400)
        app['administrator_wait_seconds'] = 25
        self.assertEqual(operations._effective_deadline({'deadline_epoch': 20}, app), 425)

    def test_unreviewed_reanalysis_cannot_silently_close_known_gap(self):
        operations = self.policy.operations
        app = operations._new_application()
        operations._ingest_analysis(app, {'unresolved_relevant_gaps': [{'skill': 'platform', 'index': 0}]})
        operations._ingest_analysis(app, {'unresolved_relevant_gaps': []})
        self.assertEqual([row['gap_id'] for row in operations._knowledge_gaps(app)], ['platform:0'])

    def test_initial_research_exhaustion_builds_limited_catalog_instead_of_empty_admin_wait(self):
        p = self.policy
        p.tick()
        for stage in ('source', 'background', 'preparation', 'environment', 'project_init', 'baseline_build'):
            p.complete(stage)
            p.tick()
        p.complete('skill_lookup', outputs={'missing_kinds': ['platform']})
        p.tick()
        for stage in ('contract_draft', 'contract_verify', 'contract_review', 'contract_freeze'):
            p.complete(stage, outputs={'verdict': 'approved'})
            p.tick()
        for _ in range(2):
            p.complete('platform_diff', status='failed', error_code='skill_agent_failed')
            p.tick()
        app = p.snapshot['application_state']
        self.assertNotIn('administrator_wait', app)
        lookup = p.snapshot['tasks']['skill_lookup']['attempts'][-1]['command']['payload']['payload']
        self.assertTrue(lookup['allow_empty_research_material'])
        self.assertEqual(app['research_budget']['platform']['dispatched'], 2)
        p.complete('skill_lookup', outputs={'missing_kinds': [], 'needs_review_kinds': ['platform']})
        p.tick()
        self.assertEqual(len(p.snapshot['tasks']['platform_diff']['attempts']), 2)
        self.assertIn('platform_skill_review', p.snapshot['tasks'])

    def test_stale_queued_administrator_submission_does_not_launch_review(self):
        app = self.policy.operations._new_application()
        app.update(gap_revision=2, knowledge_revisions={'java': {'revision': 'r2'}},
            admin_submission_queue=[{'admin_submission_id': 'old', 'admin_submission_ref': {'path': 'old.json'}}],
            admin_imports={'old': {'base_gap_revision': 1, 'knowledge_revisions': {'java': {'revision': 'r1'}}}})
        operations = self.policy.operations._support_decision(self.policy.snapshot, self.policy.header, app)
        self.assertEqual(operations, [])
        self.assertEqual(app['admin_imports']['old']['status'], 'stale')
        self.assertEqual(app['agent_assignments'], 0)

    def test_changed_task_contract_replans_and_preserves_unrelated_patch_as_evidence(self):
        self.exhaust_background()
        p = self.policy
        patch_ref = p.fixture_artifact('b.patch', 'authenticated prior patch')
        p.complete('coder.g1.b', outputs={'artifact_refs': {'coder_patch': patch_ref}})
        self.tick()
        p.complete('gap_plan')
        self.tick()
        p.complete('gap_plan_review', outputs={'verdict': 'approved', 'approved_gap_resolutions': [
            {'gap_id': 'platform:0', 'action': 'compatibility_layer', 'alternative_id': 'adapter',
             'affected_tasks': ['a'], 'verification_requirements': [{'id': 'behavior',
                 'closure_criteria': ['same behavior'], 'resolution_stage': 'test_execute'}]}],
            'approved_task_updates': [{'id': 'a', 'inputs': ['new adapter strategy']}]})
        self.tick()
        app = p.snapshot['application_state']
        self.assertIsNone(app['active_group'])
        self.assertEqual('migration_inventory', app['active_stage'])
        failure = app['repair_context']['current_failure']['result']
        self.assertEqual('development_contract_changed', failure['error_code'])
        self.assertEqual(['b'], failure['outputs']['preserved_task_ids'])
        self.assertIn('previous_goal:coder.g1.b:coder_patch', failure['outputs']['artifact_refs'])
        self.assertEqual(len(p.snapshot['tasks']['coder.g1.b']['attempts']), 1)
        self.assertNotIn('coder.g1.a', p.snapshot['tasks'])
        self.assertEqual([{'id': 'a', 'inputs': ['new adapter strategy']}], failure['outputs']['approved_task_updates'])
