import unittest
from modport import MigrationRequest
from modport.workflow import (AGENT_STAGES, CODER_REVIVAL_STAGE, DEPENDENCIES,
                              MAIN_STAGES, STAGE_IDS, WORKFLOW_VERSION,
                              agent_model_policy, compile_migration_workflow)
from modport.handlers import build_registry


class WorkflowTests(unittest.TestCase):
    def test_new_rules_and_registry_cover_the_same_single_step_operations(self):
        request = MigrationRequest('example', 'https://example.invalid/mod.git', '1.20.1', '26.1.2')
        definition = compile_migration_workflow(request).to_dict()
        self.assertEqual(definition['format_version'], 2)
        self.assertEqual(definition['budget']['max_rework_rounds'], 10)
        self.assertEqual({f'modport.{s}' for s in (*STAGE_IDS, CODER_REVIVAL_STAGE)},
                         set(build_registry()))
        self.assertNotIn('agent_dispatcher', str(definition))
        self.assertNotIn('build_test_loop', STAGE_IDS)
        self.assertNotIn('contract_review_freeze', STAGE_IDS)

    def test_structural_dependencies_acyclic_and_repairs_have_no_back_edges(self):
        def visit(stage, stack):
            self.assertNotIn(stage, stack)
            for dependency in DEPENDENCIES[stage]:
                visit(dependency, stack + [stage])
        for stage in STAGE_IDS:
            visit(stage, [])
        self.assertIn('contract_verify', MAIN_STAGES)
        self.assertNotIn('contract_verify', AGENT_STAGES)
        self.assertNotIn('acceptance_build', AGENT_STAGES)

    def test_compile_package_scope_keeps_baseline_suite_and_defers_target_acceptance(self):
        self.assertEqual(33, WORKFLOW_VERSION)
        request = MigrationRequest('example', 'https://example.invalid/mod.git', '1.20.1', '26.1.2',
                                   validation_scope='compile_package')
        definition = compile_migration_workflow(request).to_dict()
        main = definition['main_stages']
        for stage in ('test_design', 'test_review', 'test_execute',
                      'acceptance_build', 'client_smoke'):
            self.assertNotIn(stage, main)
        self.assertEqual('acceptance_preflight', definition['next_stage']['code_review'])
        self.assertEqual('gap_review', definition['next_stage']['acceptance_preflight'])
        self.assertEqual('compile_package', definition['validation_policy']['scope'])
        self.assertIn('source_baseline_behavior_tests',
                      definition['validation_policy']['required_checks'])
        self.assertNotIn('source_baseline_behavior_tests',
                         definition['validation_policy']['deferred_checks'])
        self.assertIn('contract_review', main)
        self.assertIn('contract_review', definition['early_stages'])
        self.assertEqual(['contract_review'], next(row['depends_on'] for row in
                         definition['stages'] if row['stage_id'] == 'contract_freeze'))
        self.assertEqual('unverified', definition['validation_policy']['acceptance_status'])
        self.assertEqual(('gpt-6.1-sol', 'high'),
                         agent_model_policy(WORKFLOW_VERSION, 'contract_review'))
        self.assertEqual({'model': 'gpt-6.1-sol', 'reasoning_effort': 'high'},
                         definition['agent_model_policy']['stage_overrides']['contract_review'])

        default = MigrationRequest('example', 'https://example.invalid/mod.git', '1.20.1', '26.1.2')
        self.assertNotIn('validation_scope', default.to_dict())
