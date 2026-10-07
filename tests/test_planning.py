"""Host planning gates; fake agents do not execute model or project code."""
import copy
from dataclasses import replace
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from modport import handlers
from modport.contracts import OperationInput
from modport.development import _artifact, ImplementationHandler
from modport.planning import (PlanningHandler, DevelopmentPrepareHandler, _context,
                              PlannedRepairHandler, validate_document, approved_development_plan,
                              approved_repair_development_plan, require_repair_plan, _obligations,
                              _depends_on)
from test_development import git, task


class PlanningTests(unittest.TestCase):
    def test_coupling_dependency_accepts_transitive_group_path(self):
        groups = {
            'prepare-contract-guards': {'dependencies': []},
            'repair-evidence-path': {'dependencies': ['prepare-contract-guards']},
            'verify-baseline-characterization': {'dependencies': ['repair-evidence-path']},
        }
        self.assertTrue(_depends_on(groups, 'verify-baseline-characterization',
                                    'prepare-contract-guards'))
        self.assertTrue(_depends_on(groups, 'verify-baseline-characterization',
                                    'repair-evidence-path'))
        self.assertFalse(_depends_on(groups, 'prepare-contract-guards',
                                     'verify-baseline-characterization'))

    def test_stable_gap_identity_takes_precedence_over_legacy_index(self):
        from modport.planning import _gap_ids
        self.assertEqual({'stable', 'java:2'}, _gap_ids([
            {'gap_id': 'stable', 'skill': 'platform', 'index': 1},
            {'skill': 'java', 'index': 2}], 'unresolved_knowledge_gaps'))

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.work = self.root / 'worktree'
        self.work.mkdir()
        git(self.work, 'init')
        for name in ('a', 'b', 'shared'):
            (self.work / name).write_text('original\n')
        git(self.work, 'add', '.'); git(self.work, 'commit', '-m', 'base')
        self.refs = {}
        self.generated_documents = {}
        for alias, body in [('source_evidence', {'source_commit': 'a' * 40}),
                            ('functional_contract_lock', {'contract': {'behaviors': [{'id': 'behavior'}]}})]:
            cmd = self.command('migration_inventory', identifier='fixture-' + alias)
            self.refs[alias] = _artifact(cmd, alias + '.json', json.dumps(body).encode())
        rubric = patch('modport.handlers._acceptance_rubric_for', return_value={'rubric_sha256': 'r' * 64})
        locked = patch('modport.handlers._verify_locked_artifacts', return_value={})
        rubric.start(); locked.start()
        self.addCleanup(rubric.stop); self.addCleanup(locked.stop)

    def command(self, stage, identifier=None):
        return OperationInput('run', stage, stage, identifier or stage, str(self.root),
                              payload={'planning_generation': 0, **getattr(self, 'gap_payload', {}),
                                       **getattr(self, 'replanning_payload', {})}, artifact_refs=self.refs,
                              options={'workflow_version': getattr(self, 'workflow_version', 10)})

    def scoped_document(self, stage, doc, *, current_stage='implementation',
                        inventory_stage='migration_inventory', task_stage='migration_tasks'):
        """One immediate correction and one later verification duty."""
        if stage == 'migration_inventory':
            doc['issues'][0].update(resolution_scope='current', resolution_stage=current_stage,
                                    scope_reason='The current implementation must preserve this behavior')
            later = copy.deepcopy(doc['issues'][0])
            later.update(id='later', resolution_scope='downstream', resolution_stage='client_smoke',
                         scope_reason='Client startup must be verified after the migrated build exists')
            doc['issues'].append(later)
        elif stage == 'migration_plan':
            later = copy.deepcopy(doc['strategies'][0])
            later.update(id='later-strategy', issue_ids=['later'])
            doc['strategies'].append(later)
        elif stage == 'migration_tasks':
            first, later = doc['tasks']
            first['validation_checks'] = [{'id': 'a:test', 'type': 'gradle_tasks',
                                          'tasks': ['test'], 'acceptance': first['acceptance']}]
            later.update(kind='deferred', issue_ids=['later'], strategy_ids=['later-strategy'],
                         acceptance=['Start the migrated client and verify its expected state'])
        elif stage == 'parallel_review':
            source = self.generated_documents[task_stage]['tasks']
            first, later = source
            doc['development_plan']['tasks'] = [doc['development_plan']['tasks'][0]]
            doc['development_plan']['tasks'][0]['validation_checks'] = copy.deepcopy(first['validation_checks'])
            doc['coupling_checks'] = []
            doc['deferred_obligations'] = [{
                'id': inventory_stage + ':b', 'source_task_id': 'b', 'source_issue_ids': ['later'],
                'objective': later['objective'], 'closure_criteria': later['acceptance'],
                'resolution_stage': 'client_smoke',
                'scope_reason': 'Client startup must be verified after the migrated build exists'}]

    def scoped_round(self, stage, *, mutation=None, identifier=None):
        self.workflow_version = 11
        def mutate(doc):
            self.scoped_document(stage, doc)
            if mutation:
                mutation(doc)
        return self.run_stage(stage, mutation=mutate, identifier=identifier)

    def validate_scoped_round(self, stage, *, mutation=None, identifier=None):
        self.workflow_version = 11
        def mutate(document):
            self.scoped_document(stage, document)
            if mutation:
                mutation(document)
        return self.validate_legacy_round(stage, mutation=mutate, identifier=identifier)

    def validate_legacy_round(self, stage, *, mutation=None, prepare=False, identifier=None,
                              task_stage='migration_tasks'):
        """Exercise the retained production validator without emulating transport."""
        command = self.command(stage, identifier)
        if stage.startswith(('target_', 'contract_')):
            command = replace(command, payload=self.repair_payload(stage))
        workspace = self.root / ('baseline' if stage.startswith('contract_') else 'worktree')
        expected, contract, rubric = _context(command, workspace)
        kind = ('migration_inventory' if stage.endswith('_diagnose') else
                'migration_plan' if stage.endswith('_plan') else
                'parallel_review' if stage.endswith('_review') else
                'migration_tasks' if stage.endswith('_tasks') else stage)
        body = (self.repair_body(kind, task_stage=task_stage)
                if stage.startswith(('target_', 'contract_')) else self.body(kind, prepare))
        document = {**expected, 'base_commit': git(workspace, 'rev-parse', 'HEAD'), **body}
        if mutation:
            mutation(document)
        plan = validate_document(command, document, expected,
                                 obligations=_obligations(
                                     contract, rubric,
                                     tolerate_malformed=stage.startswith('contract_')))
        self.generated_documents[stage] = copy.deepcopy(document)
        self.refs[stage] = _artifact(command, stage + '.json',
                                     (json.dumps(document, sort_keys=True) + '\n').encode())
        if plan:
            self.refs['development_plan'] = _artifact(
                command, 'development-plan.json',
                (json.dumps(plan, sort_keys=True) + '\n').encode())
        return document, plan

    def v12_repair_round(self, stage, *, mutation=None, identifier=None):
        self.workflow_version = 12
        kind = {'target_diagnose': 'migration_inventory', 'target_repair_plan': 'migration_plan',
                'target_repair_tasks': 'migration_tasks', 'target_repair_review': 'parallel_review'}[stage]
        def mutate(doc):
            self.scoped_document(kind, doc, current_stage='target_revise',
                                 inventory_stage='target_diagnose', task_stage='target_repair_tasks')
            if kind == 'migration_tasks':
                first = doc['tasks'][0]
                first['validation_checks'] = [{'id': 'a:test', 'type': 'gradle_regression',
                    'tasks': [':test'], 'reports': ['build/test-results/test/TEST-a.xml'],
                    'acceptance': first['acceptance']}]
            if mutation:
                mutation(doc)
        return self.repair_round(stage, mutation=mutate, identifier=identifier)

    def test_v12_repair_pipeline_uses_references_and_keeps_current_failure_active(self):
        from modport.planning_references import context_closure, read_context, reference_history
        from modport.prompt_compressor import prompt_regions
        self.repair_payload('target_diagnose')
        history = 'old unrelated repair detail ' * 20000
        self.repair_context['prior_attempts'] = [{'run_id': 'parent', 'result': {'detail': history}}]
        self.repair_context['current_failure']['result']['error_code'] = 'reload_race'
        self.repair_context = reference_history(self.root, self.repair_context)
        for stage in ('target_diagnose', 'target_repair_plan', 'target_repair_tasks', 'target_repair_review'):
            result = self.v12_repair_round(stage)
            self.assertEqual('completed', result.status, result.detail)
            prompt = self.repair_prompts[stage]
            active, historical, protected = prompt_regions(prompt)
            self.assertNotIn(history, prompt)
            for text in ('ColorList reload race', 'reload_race', 'failed-execution', 'client_smoke'):
                self.assertIn(text, active)
            if stage != 'target_repair_review':
                self.assertIn('no JSON schema or identity echo is required', protected)
            else:
                self.assertIn('Return one JSON object', protected)
            self.assertIn('Planning input manifest', protected)
            self.assertIn('retrieval index', historical)
            document = json.loads((self.root / self.refs[stage]['path']).read_text())
            archived = read_context(self.root, document['repair_context'])
            self.assertEqual(self.repair_context, archived)
            original = read_context(self.root, archived['history_source'])
            self.assertEqual(history, original['prior_attempts'][0]['result']['detail'])
            manifest = json.loads((self.root / document['input_manifest']['path']).read_text())
            self.assertNotIn(history, json.dumps(manifest))
            bindings = read_context(self.root, manifest['input_bindings'])
            self.assertIn('source_evidence', bindings)
            for prior in manifest['preceding_rounds']:
                self.assertEqual({'stage', 'producer_execution_id', 'source_ref'}, set(prior))
            exported = {ref['path'] for ref in result.outputs['artifact_refs'].values()}
            self.assertIn(document['input_manifest']['path'], exported)
            self.assertTrue({ref['path'] for ref in context_closure(self.root, manifest)} <= exported)
        command = replace(self.command('target_revise'), payload=self.repair_payload('target_revise'))
        plan = approved_repair_development_plan(command)
        self.assertEqual('gradle_regression', plan['tasks'][0]['validation_checks'][0]['type'])
        self.assertEqual([':test'], plan['tasks'][0]['validation_checks'][0]['tasks'])

    def test_v12_review_cannot_downgrade_frozen_regression(self):
        for stage in ('target_diagnose', 'target_repair_plan', 'target_repair_tasks'):
            result = self.v12_repair_round(stage)
            self.assertEqual('completed', result.status, result.detail)
        result = self.v12_repair_round('target_repair_review', mutation=lambda doc:
            doc['development_plan']['tasks'][0].update(validation_checks=[{
                'id': 'a:test', 'type': 'gradle_tasks', 'tasks': ['test'],
                'acceptance': doc['development_plan']['tasks'][0]['acceptance']}]))
        self.assertEqual('planning_output_invalid', result.error_code)

    def test_v12_tampered_upstream_context_rejected_before_agent(self):
        result = self.v12_repair_round('target_diagnose')
        self.assertEqual('completed', result.status, result.detail)
        document = json.loads((self.root / self.refs['target_diagnose']['path']).read_text())
        manifest = json.loads((self.root / document['input_manifest']['path']).read_text())
        path = self.root / manifest['input_bindings']['source_ref']['path']
        path.write_text('{}')
        result = self.v12_repair_round('target_repair_plan')
        self.assertEqual('blocked', result.status)
        self.assertNotIn('target_repair_plan', self.repair_prompts)

    def test_v12_context_mapping_order_does_not_invalidate_next_round(self):
        result = self.v12_repair_round('target_diagnose')
        self.assertEqual('completed', result.status, result.detail)
        self.repair_context['artifact_refs'] = dict(reversed(
            list(self.repair_context['artifact_refs'].items())))
        result = self.v12_repair_round('target_repair_plan')
        self.assertEqual('completed', result.status, result.detail)

    def test_v11_requires_explicit_issue_scope_and_due_stage(self):
        self.workflow_version = 11
        with self.assertRaisesRegex(ValueError, 'resolution_scope'):
            self.validate_legacy_round('migration_inventory', identifier='missing-scope')
        for index, mutation in enumerate((
            lambda doc: doc['issues'][0].pop('scope_reason'),
            lambda doc: doc['issues'][1].update(resolution_stage='source'),
            lambda doc: doc['issues'][0].update(resolution_stage='client_smoke'),
        )):
            with self.assertRaises(ValueError):
                self.validate_scoped_round('migration_inventory', mutation=mutation,
                                           identifier='invalid-scope-' + str(index))

    def test_v11_retains_downstream_closure_without_immediate_coder(self):
        for stage in ('migration_inventory', 'migration_plan', 'migration_tasks', 'parallel_review'):
            result = self.scoped_round(stage)
            self.assertEqual('completed', result.status, result.detail)
        self.assertEqual(['b'], [item['source_task_id'] for item in result.outputs['deferred_obligations']])
        self.assertEqual(['Start the migrated client and verify its expected state'],
                         result.outputs['deferred_obligations'][0]['closure_criteria'])
        self.assertEqual('client_smoke', result.outputs['deferred_obligations'][0]['resolution_stage'])
        plan = approved_development_plan(self.command('implementation'))
        self.assertEqual(['a'], [item['id'] for item in plan['tasks']])
        self.assertEqual('gradle_tasks', plan['tasks'][0]['validation_checks'][0]['type'])
        reviewed = json.loads((self.root / self.refs['parallel_review']['path']).read_text())
        self.assertEqual(reviewed['deferred_obligations'], result.outputs['deferred_obligations'])

    def test_v11_current_checks_are_required_and_review_cannot_weaken_them(self):
        for stage in ('migration_inventory', 'migration_plan'):
            self.validate_scoped_round(stage)
        with self.assertRaisesRegex(ValueError, 'validation_checks'):
            self.validate_scoped_round('migration_tasks', identifier='missing-check',
                mutation=lambda doc: doc['tasks'][0].pop('validation_checks'))
        self.validate_scoped_round('migration_tasks')
        for index, mutation in enumerate((
            lambda doc: doc['development_plan']['tasks'][0].pop('validation_checks'),
            lambda doc: doc['development_plan']['tasks'][0].update(validation_checks=[{
                'id': 'a:test', 'type': 'file_exists', 'path': 'a', 'acceptance': ['Preserve expected behavior']}]),
            lambda doc: doc.pop('deferred_obligations'),
            lambda doc: doc['deferred_obligations'][0].update(closure_criteria=['weaker closure']),
        )):
            with self.assertRaises(ValueError):
                self.validate_scoped_round('parallel_review', mutation=mutation,
                                           identifier='weakened-' + str(index))

    def test_v11_immediate_tasks_cannot_depend_on_deferred_work(self):
        for stage in ('migration_inventory', 'migration_plan'):
            self.validate_scoped_round(stage)
        with self.assertRaisesRegex(ValueError, 'depends on a downstream obligation'):
            self.validate_scoped_round('migration_tasks', mutation=lambda doc:
                                       doc['tasks'][0].update(dependencies=['b']))

    def test_v11_repair_review_retains_later_tests_and_executes_prerequisite(self):
        self.workflow_version = 11
        stages = ('target_diagnose', 'target_repair_plan', 'target_repair_tasks', 'target_repair_review')
        kinds = ('migration_inventory', 'migration_plan', 'migration_tasks', 'parallel_review')
        for stage, kind in zip(stages, kinds):
            def mutate(doc):
                self.scoped_document(kind, doc, current_stage='target_revise',
                                     inventory_stage='target_diagnose', task_stage='target_repair_tasks')
                if kind == 'migration_inventory':
                    doc['issues'][0]['resolution_scope'] = 'prerequisite'
            result = self.repair_round(stage, mutation=mutate)
            self.assertEqual('completed', result.status, result.detail)
        self.assertEqual('target_diagnose:b', result.outputs['deferred_obligations'][0]['id'])
        command = replace(self.command('target_revise'), payload=self.repair_payload('target_revise'))
        plan = approved_repair_development_plan(command)
        self.assertEqual(['a'], [item['id'] for item in plan['tasks']])
        sources = self.generated_documents['target_repair_tasks']['tasks']
        diagnosis = self.generated_documents['target_diagnose']['issues']
        self.assertEqual('prerequisite', diagnosis[0]['resolution_scope'])
        self.assertEqual(sources[0]['validation_checks'], plan['tasks'][0]['validation_checks'])

    def repair_payload(self, stage):
        if not hasattr(self, 'repair_context'):
            failure = {'stage': 'contract_verify' if stage.startswith('contract_') else 'client_smoke',
                       'execution_id': 'failed-execution', 'findings': [],
                       'result': {'status': 'failed', 'detail': 'ColorList reload race', 'outputs': {}}}
            self.repair_context = {'schema_version': 1, 'run_id': 'run', 'repair_generation': 1,
                                   'repair_scope': stage.split('_')[0], 'failure_execution_id': 'failed-execution',
                                   'current_failure': failure, 'request': {'mod_id': 'example'},
                                   'prior_findings': [], 'prior_attempts': [], 'upstream_results': {},
                                   'artifact_refs': copy.deepcopy(self.refs)}
        return {'repair_generation': 1, 'repair_scope': stage.split('_')[0],
                'repair_context': copy.deepcopy(self.repair_context),
                'rework_context': copy.deepcopy(self.repair_context['current_failure'])}

    def repair_body(self, kind, *, task_stage='migration_tasks'):
        body = self.body(kind, task_stage=task_stage)
        if kind == 'migration_inventory':
            body['failure_analysis'] = {'cause': 'configuration watcher invalidates cache',
                'evidence_refs': ['source_evidence'], 'unknowns': [],
                'previous_attempts_analysis': 'No previous attempt addressed reload synchronization'}
        elif kind == 'migration_plan':
            for strategy in body['strategies']:
                strategy.update(risks=['configuration watcher races'], constraints=['preserve source behavior'])
        elif kind == 'migration_tasks':
            body['consistency_review'] = {'consistent': True, 'explanation': 'tasks preserve diagnosed scope', 'contradictions': []}
            for value in body['tasks']:
                value['stop_conditions'] = ['report unexpected configuration reload behavior']
        return body

    def body(self, stage, prepare=False, *, task_stage='migration_tasks'):
        if stage == 'migration_inventory':
            return {'issues': [{'id': 'issue', 'classification': 'fact', 'summary': 'preserve behavior',
                                'evidence_refs': ['source_evidence'], 'obligation_ids': ['behavior']}]}
        if stage == 'migration_plan':
            return {'strategies': [{'id': 'strategy', 'issue_ids': ['issue'], 'approach': 'port',
                                    'regression_method': 'frozen characterization', 'disposition_reason': 'required',
                                    'interfaces': ['shared ABI'], 'prerequisites': []}]}
        if stage == 'migration_tasks':
            tasks = []
            for name in (['shared', 'a', 'b'] if prepare else ['a', 'b']):
                tasks.append({**task(name, name, ['shared'] if prepare and name != 'shared' else []),
                              'kind': 'prepare' if name == 'shared' else 'coder', 'inputs': ['strategy'],
                              'outputs': ['ported behavior'], 'issue_ids': ['issue'], 'strategy_ids': ['strategy']})
            return {'tasks': tasks}
        if stage == 'parallel_review':
            source = self.generated_documents[task_stage]['tasks']
            if prepare:
                return {'parallel_decision': 'prepare_first', 'reason': 'shared ABI first',
                        'preparation_plan': {
                            'schema_version': 1,
                            'base_commit': git(self.work, 'rev-parse', 'HEAD'),
                            'shared_paths': [],
                            'tasks': [{**copy.deepcopy(task),
                                       'source_task_ids': [task['id']],
                                       'source_objectives': {task['id']: task['objective']},
                                       'source_acceptance': {task['id']: task['acceptance']}}
                                      for task in source if task['kind'] == 'prepare']}}
            groups = []
            for value in source:
                if value['kind'] == 'prepare':
                    continue
                groups.append({**task(value['id'], value['id']), 'source_task_ids': [value['id']],
                               'source_objectives': {value['id']: value['objective']},
                               'source_acceptance': {value['id']: value['acceptance']}})
            return {'parallel_decision': 'parallel', 'reason': 'independent behavior boundaries',
                    'preparation_assessment': {'conforms_to_plan': True, 'evidence': 'verified ABI against shared strategy'},
                    'coupling_checks': [{'groups': ['a', 'b'], 'evidence': 'separate ABI consumers', 'resolution': 'independent'}],
                    'development_plan': {'schema_version': 1, 'base_commit': git(self.work, 'rev-parse', 'HEAD'),
                                         'shared_paths': [], 'tasks': groups}}

    def run_stage(self, stage, *, mutation=None, prepare=False, identifier=None):
        command = self.command(stage, identifier)
        def fake(_handler, cmd):
            self.assertTrue(_handler.read_only)
            expected, _, _ = _context(cmd, self.work)
            body = (self.repair_body(stage) if cmd.payload.get('repair_scope') == 'migration'
                    and stage != 'parallel_review' else self.body(stage, prepare))
            doc = {**expected, 'base_commit': git(self.work, 'rev-parse', 'HEAD'), **body}
            if mutation:
                mutation(doc)
            self.generated_documents[stage] = copy.deepcopy(doc)
            output = self.root / 'logs' / (cmd.command_id + '.txt')
            output.write_text(json.dumps(doc))
            return handlers._result(cmd, 'completed', outputs={'last_message': str(output)})
        with patch('modport.handlers.CodexStageHandler.__call__', fake):
            result = PlanningHandler()(command)
        if result.status == 'completed':
            self.refs.update(result.outputs['artifact_refs'])
        return result

    def chain(self, prepare=False):
        for stage in ('migration_inventory', 'migration_plan', 'migration_tasks', 'parallel_review'):
            result = self.run_stage(stage, prepare=prepare)
            self.assertEqual('completed', result.status, result.detail)

    def test_four_rounds_bind_approval_and_actual_head(self):
        self.chain()
        cmd = self.command('implementation')
        result = ImplementationHandler()(cmd)
        self.assertEqual('completed', result.status, result.detail)
        (self.work / 'a').write_text('changed\n')
        git(self.work, 'add', '.'); git(self.work, 'commit', '-m', 'changed')
        result = ImplementationHandler()(cmd)
        self.assertEqual('failed', result.status)

    def test_legacy_plan_without_approval_rejected(self):
        result = ImplementationHandler()(self.command('implementation'))
        self.assertEqual('failed', result.status)

    def test_resolved_gap_ids_remain_valid_without_blocking_execution(self):
        self.gap_payload = {'known_knowledge_gap_ids': ['platform:0'],
                            'unresolved_knowledge_gaps': []}
        for stage in ('migration_inventory', 'migration_plan'):
            self.assertEqual('completed', self.run_stage(stage).status)
        result = self.run_stage('migration_tasks', mutation=lambda d:
                                d['tasks'][0].update(blocked_by_gaps=['platform:0']))
        self.assertEqual('completed', result.status, result.detail)
        result = self.run_stage('parallel_review')
        self.assertEqual('completed', result.status, result.detail)
        plan = approved_development_plan(self.command('implementation'))
        self.assertEqual([], plan['tasks'][0]['blocked_by_gaps'])
        for stage in ('migration_inventory', 'migration_plan'):
            self.validate_legacy_round(stage, identifier='validate-' + stage)
        with self.assertRaisesRegex(ValueError, 'unknown knowledge gap'):
            self.validate_legacy_round('migration_tasks', identifier='unknown-gap', mutation=lambda d:
                                       d['tasks'][0].update(blocked_by_gaps=['invented:0']))

    def test_preparation_only_reports_current_unresolved_gap_blockers(self):
        self.gap_payload = {'known_knowledge_gap_ids': ['platform:0', 'platform:1'],
                            'unresolved_knowledge_gaps': ['platform:1']}
        for stage in ('migration_inventory', 'migration_plan'):
            self.assertEqual('completed', self.run_stage(stage, prepare=True).status)
        result = self.run_stage('migration_tasks', prepare=True, mutation=lambda d:
                                d['tasks'][0].update(blocked_by_gaps=['platform:0', 'platform:1']))
        self.assertEqual('completed', result.status, result.detail)
        result = self.run_stage('parallel_review', prepare=True)
        self.assertEqual('completed', result.status, result.detail)
        self.assertEqual(['platform:1'], result.outputs['prepare_blocked_by_gaps'])

    def test_gap_closure_after_approval_preserves_frozen_plan_and_allows_implementation(self):
        self.workflow_version = 11
        self.gap_payload = {'known_knowledge_gap_ids': ['platform:0'],
                            'unresolved_knowledge_gaps': ['platform:0']}
        def mutate(stage, doc):
            if stage == 'migration_inventory':
                for issue in doc['issues']:
                    issue.update(resolution_scope='current', resolution_stage='implementation',
                                 scope_reason='Current migration task')
            if stage == 'migration_tasks':
                for task in doc['tasks']:
                    task['validation_checks'] = [{'id': task['id'] + '-exists', 'type': 'file_exists',
                                                 'path': task['id'], 'acceptance': task['acceptance']}]
                doc['tasks'][0]['blocked_by_gaps'] = ['platform:0']
            if stage == 'parallel_review':
                source = self.generated_documents['migration_tasks']['tasks']
                for group, task in zip(doc['development_plan']['tasks'], source):
                    group['validation_checks'] = task['validation_checks']
                    group['blocked_by_gaps'] = task.get('blocked_by_gaps', [])
        for stage in ('migration_inventory', 'migration_plan', 'migration_tasks', 'parallel_review'):
            result = self.run_stage(stage, mutation=lambda doc, stage=stage: mutate(stage, doc))
            self.assertEqual('completed', result.status, result.detail)
        before = approved_development_plan(self.command('implementation'))
        self.gap_payload['unresolved_knowledge_gaps'] = []
        after = approved_development_plan(self.command('implementation'))
        self.assertEqual(before, after)
        self.assertEqual(['platform:0'], after['tasks'][0]['blocked_by_gaps'])
        implemented = ImplementationHandler()(self.command('implementation'))
        self.assertEqual('completed', implemented.status, implemented.detail)
        self.assertEqual(after['tasks'], implemented.outputs['development_tasks'])

    def test_inventory_obligation_omission_and_unknown_evidence_rejected(self):
        for mutate in (lambda d: d['issues'][0].update(obligation_ids=[]),
                       lambda d: d['issues'][0].update(evidence_refs=['invented'])):
            with self.assertRaises(ValueError):
                self.validate_legacy_round('migration_inventory', mutation=mutate)

    def test_product_update_during_planning_does_not_invalidate_response(self):
        result = self.run_stage('migration_inventory', mutation=lambda d: (self.work / 'a').write_text('forbidden'))
        self.assertEqual('completed', result.status, result.detail)

    def test_stale_generation_and_tampered_input_rejected(self):
        self.assertEqual('completed', self.run_stage('migration_inventory').status)
        cmd = replace(self.command('migration_plan'), payload={'planning_generation': 1})
        self.assertEqual('blocked', PlanningHandler()(cmd).status)
        (self.root / self.refs['migration_inventory']['path']).write_text('{}')
        self.assertEqual('blocked', PlanningHandler()(self.command('migration_plan')).status)

    def test_groups_must_preserve_coverage_acceptance_and_semantic_checks(self):
        for stage in ('migration_inventory', 'migration_plan', 'migration_tasks'):
            self.validate_legacy_round(stage)
        mutations = [lambda d: d['development_plan']['tasks'].pop(),
                     lambda d: d['development_plan']['tasks'][0].update(source_acceptance={'a': ['weaken']}),
                     lambda d: d.update(coupling_checks=[]),
                     lambda d: d['coupling_checks'][0].update(resolution='dependency')]
        for index, mutate in enumerate(mutations):
            with self.assertRaises(ValueError):
                self.validate_legacy_round('parallel_review', mutation=mutate,
                                           identifier='bad-' + str(index))

    def test_sequential_advice_preserves_independent_dependencies(self):
        for stage in ('migration_inventory', 'migration_plan', 'migration_tasks'):
            self.assertEqual('completed', self.run_stage(stage).status)
        result = self.run_stage('parallel_review', mutation=lambda d: d.update(parallel_decision='sequential'))
        self.assertEqual('completed', result.status, result.detail)
        plan = approved_development_plan(self.command('implementation'))
        self.assertEqual([], plan['tasks'][1]['dependencies'])

    def test_shared_preparation_authenticates_base_transition_and_coverage(self):
        self.chain(prepare=True)
        cmd = self.command('development_prepare')
        def fake(_handler, command):
            work = self.root / command.options['workspace']
            (work / 'shared').write_text('implemented ABI\n')
            git(work, 'add', '.'); git(work, 'commit', '-m', 'prepare')
            return handlers._result(command, 'completed')
        with patch('modport.handlers.CodexStageHandler.__call__', fake):
            result = DevelopmentPrepareHandler()(cmd)
        self.assertEqual('completed', result.status, result.detail)
        self.refs.update(result.outputs['artifact_refs'])
        result = self.run_stage('parallel_review', identifier='review-prepared')
        self.assertEqual('completed', result.status, result.detail)
        result = ImplementationHandler()(self.command('implementation'))
        self.assertEqual('completed', result.status, result.detail)

    def test_shared_preparation_rejects_unowned_committed_changes(self):
        self.chain(prepare=True)
        def fake(_handler, command):
            work = self.root / command.options['workspace']
            (work / 'a').write_text('forbidden\n')
            git(work, 'add', '.'); git(work, 'commit', '-m', 'forbidden')
            return handlers._result(command, 'completed')
        with patch('modport.handlers.CodexStageHandler.__call__', fake):
            result = DevelopmentPrepareHandler()(self.command('development_prepare'))
        self.assertEqual('blocked', result.status)
        self.assertEqual('original\n', (self.work / 'a').read_text())

    def test_contract_diagnosis_accepts_missing_candidate_but_binds_source(self):
        baseline = self.root / 'baseline'
        git(self.root, 'clone', str(self.work), str(baseline))
        command = replace(self.command('contract_diagnose'), payload=self.repair_payload('contract_diagnose'))
        expected, _, _ = _context(command, baseline)
        self.assertIsNone(expected['contract_id'])
        self.assertEqual('a' * 40, expected['source_fingerprint'])
        self.assertNotIn('contract_sha256', expected)
        def fake(_handler, cmd):
            doc = {**expected, 'base_commit': git(baseline, 'rev-parse', 'HEAD'),
                   **self.repair_body('migration_inventory')}
            output = self.root / 'logs' / 'diagnosis.txt'
            output.write_text(json.dumps(doc))
            return handlers._result(cmd, 'completed', outputs={'last_message': 'logs/diagnosis.txt'})
        with patch('modport.handlers.CodexStageHandler.__call__', fake):
            result = PlanningHandler()(command)
        self.assertEqual('completed', result.status, result.detail)

    def test_planning_rebuilds_prompt_and_binds_current_execution_identity(self):
        baseline = self.root / 'baseline'
        git(self.root, 'clone', str(self.work), str(baseline))
        payload = self.repair_payload('contract_diagnose')
        payload['prompt_reuse'] = {'execution_id': 'old-execution'}
        command = replace(self.command('contract_diagnose', identifier='recovery-execution'),
                          payload=payload)
        expected, _, _ = _context(command, baseline)
        old_execution = 'old-execution'

        def fake(_handler, cmd):
            self.assertFalse(_handler.reuse_recovery_prompt)
            doc = {**expected, 'producer_execution_id': old_execution,
                   'base_commit': git(baseline, 'rev-parse', 'HEAD'),
                   **self.repair_body('migration_inventory')}
            output = self.root / 'logs' / 'reused-diagnosis.txt'
            output.write_text(json.dumps(doc))
            return handlers._result(cmd, 'completed', outputs={'last_message': str(output)})

        with patch('modport.handlers._recovery_prompt_cache_paths',
                   return_value=(Path('source'), Path('metadata'), Path('compressed'), old_execution)), \
             patch('modport.handlers.CodexStageHandler.__call__', fake):
            result = PlanningHandler()(command)
        self.assertEqual('completed', result.status, result.detail)
        sealed = json.loads((self.root / result.outputs['artifact_refs']['contract_diagnose']['path']).read_text())
        self.assertEqual(command.command_id, sealed['producer_execution_id'])
        self.assertNotIn('prompt_reused_from_execution_id', sealed)

    def test_invalid_reused_prompt_identity_is_replaced_without_provenance(self):
        baseline = self.root / 'baseline'
        git(self.root, 'clone', str(self.work), str(baseline))
        payload = self.repair_payload('contract_diagnose')
        payload['prompt_reuse'] = {'execution_id': 'old-execution'}
        command = replace(self.command('contract_diagnose', identifier='recovery-arbitrary'),
                          payload=payload)
        expected, _, _ = _context(command, baseline)

        def fake(_handler, cmd):
            doc = {**expected, 'producer_execution_id': 'untrusted-model-value',
                   'base_commit': git(baseline, 'rev-parse', 'HEAD'),
                   **self.repair_body('migration_inventory')}
            output = self.root / 'logs' / 'arbitrary-diagnosis.txt'
            output.write_text(json.dumps(doc))
            return handlers._result(cmd, 'completed', outputs={'last_message': str(output)})

        with patch('modport.handlers._recovery_prompt_cache_paths',
                   return_value=(Path('source'), Path('metadata'), Path('compressed'), 'old-execution')), \
             patch('modport.handlers.CodexStageHandler.__call__', fake):
            result = PlanningHandler()(command)
        self.assertEqual('completed', result.status, result.detail)
        sealed = json.loads((self.root / result.outputs['artifact_refs']['contract_diagnose']['path']).read_text())
        self.assertEqual(command.command_id, sealed['producer_execution_id'])
        self.assertNotIn('prompt_reused_from_execution_id', sealed)

    def test_repair_execution_rejects_missing_or_stale_rounds(self):
        command = replace(self.command('target_revise'), payload=self.repair_payload('target_revise'))
        with self.assertRaises((ValueError, KeyError)):
            require_repair_plan(command)

    def test_repair_requires_fourth_review_and_binds_approved_groups(self):
        for stage in ('target_diagnose', 'target_repair_plan', 'target_repair_tasks'):
            result = self.repair_round(stage)
            self.assertEqual('completed', result.status, result.detail)
        command = replace(self.command('target_revise'), payload=self.repair_payload('target_revise'))
        with self.assertRaises((ValueError, KeyError)):
            approved_repair_development_plan(command)
        result = self.repair_round('target_repair_review')
        self.assertEqual('completed', result.status, result.detail)
        command = replace(command, artifact_refs=self.refs)
        plan = approved_repair_development_plan(command)
        self.assertEqual(['a', 'b'], [task['id'] for task in plan['tasks']])
        self.assertEqual([['a'], ['b']], [task['source_task_ids'] for task in plan['tasks']])
        altered = copy.deepcopy(plan)
        altered['tasks'][0]['objective'] = 'unreviewed objective'
        self.refs['development_plan'] = _artifact(self.command('replacement'), 'unreviewed-plan.json',
                                                  json.dumps(altered).encode())
        with self.assertRaisesRegex(ValueError, 'differs from independent review'):
            approved_repair_development_plan(replace(command, artifact_refs=self.refs))

    def test_preparation_patch_tamper_is_rejected_before_review(self):
        self.chain(prepare=True)
        def fake(_handler, command):
            work = self.root / command.options['workspace']
            (work / 'shared').write_text('implemented ABI\n')
            git(work, 'add', '.'); git(work, 'commit', '-m', 'prepare')
            return handlers._result(command, 'completed')
        with patch('modport.handlers.CodexStageHandler.__call__', fake):
            result = DevelopmentPrepareHandler()(self.command('development_prepare'))
        self.assertEqual('completed', result.status, result.detail)
        self.refs.update(result.outputs['artifact_refs'])
        record = json.loads((self.root / self.refs['development_prepare']['path']).read_text())
        (self.root / record['patch_ref']['path']).write_text('tampered')
        result = self.run_stage('parallel_review', identifier='updated-prepared')
        self.assertEqual('blocked', result.status, result.detail)
        self.assertEqual('planning_artifact_invalid', result.error_code)
        self.assertIn('digest mismatch', result.detail)

    def test_preparation_can_request_replan_without_integrating(self):
        self.chain(prepare=True)
        def fake(_handler, command):
            message = self.root / 'logs' / 'prepare-replan.txt'
            message.write_text(json.dumps({'replan_stage': 'migration_plan', 'reason': 'ABI needs correction'}))
            return handlers._result(command, 'completed', outputs={'last_message': str(message)})
        with patch('modport.handlers.CodexStageHandler.__call__', fake):
            result = DevelopmentPrepareHandler()(self.command('development_prepare'))
        self.assertEqual('completed', result.status, result.detail)
        self.assertEqual('migration_plan', result.outputs['replan_stage'])
        self.assertEqual('original\n', (self.work / 'shared').read_text())

    def repair_round(self, stage, *, identifier=None, mutation=None):
        command = replace(self.command(stage, identifier), payload=self.repair_payload(stage))
        workspace = self.root / ('baseline' if stage.startswith('contract_') else 'worktree')
        def fake(_handler, cmd):
            if not hasattr(self, 'repair_prompts'):
                self.repair_prompts = {}
            self.repair_prompts[stage] = _handler.prompt
            expected, _, _ = _context(cmd, workspace)
            kind = ('migration_inventory' if stage.endswith('_diagnose') else
                    'migration_plan' if stage.endswith('_plan') else
                    'parallel_review' if stage.endswith('_review') else 'migration_tasks')
            doc = {**expected, 'base_commit': git(workspace, 'rev-parse', 'HEAD'),
                   **self.repair_body(kind, task_stage=stage.split('_')[0] + '_repair_tasks')}
            if mutation:
                mutation(doc)
            self.generated_documents[stage] = copy.deepcopy(doc)
            output = self.root / 'logs' / (cmd.command_id + '.txt')
            output.write_text(json.dumps(doc))
            return handlers._result(cmd, 'completed', outputs={'last_message': str(output)})
        with patch('modport.handlers.CodexStageHandler.__call__', fake):
            result = PlanningHandler()(command)
        if result.status == 'completed':
            self.refs.update(result.outputs['artifact_refs'])
        return result

    def test_typed_schema_rejection_produces_bounded_feedback(self):
        from modport.planning import _format_feedback
        from modport.planning_schema import PlanningValidationError
        self.workflow_version = 11
        def invalid(doc):
            self.scoped_document('migration_inventory', doc, current_stage='target_revise',
                                 inventory_stage='target_diagnose', task_stage='target_repair_tasks')
            doc['failure_analysis']['previous_attempts_analysis'] = {
                'attempts': ['reload remained broken']}
        with self.assertRaises(PlanningValidationError) as raised:
            self.validate_legacy_round('target_diagnose', identifier='bad-diagnosis',
                                       mutation=invalid)
        diagnostic = raised.exception.diagnostic
        self.assertEqual('/failure_analysis/previous_attempts_analysis', diagnostic['path'])
        self.assertEqual('invalid_type', diagnostic['code'])
        self.assertEqual('object', diagnostic['actual_type'])
        self.assertEqual('string', diagnostic['expected_type'])
        first = self.command('target_diagnose', 'bad-diagnosis')
        rejected = handlers._result(first, 'failed', error_code='planning_output_invalid',
                                    detail=str(raised.exception), outputs={
                                        'validation_error': diagnostic,
                                        'validation_errors': [diagnostic]})
        retry = replace(first, command_id='corrected-diagnosis', payload={
            **self.repair_payload('target_diagnose'),
            'format_context': {'stage': first.stage_id, 'execution_id': first.command_id,
                               'result': rejected.to_dict()}})
        feedback = _format_feedback(retry)
        self.assertEqual(diagnostic, feedback['validation_error'])
        self.assertEqual([diagnostic], feedback['validation_errors'])
        self.assertLessEqual(len(json.dumps(feedback).encode()), 4096)

    def test_format_feedback_is_bounded_and_validates_prior_identity(self):
        from modport.planning import _format_feedback
        first = self.command('migration_inventory', 'first')
        result = handlers._result(first, 'failed', error_code='planning_output_invalid',
                                  detail='错误' * 3000).to_dict()
        context = {'stage': first.stage_id, 'execution_id': first.command_id, 'result': result}
        command = replace(first, command_id='second', payload={'format_context': context})
        feedback = _format_feedback(command)
        self.assertTrue(feedback['detail_truncated'])
        self.assertLessEqual(len(feedback['detail'].encode()), 2048)
        for mutate in (
            lambda c: c.update(stage='another-stage'),
            lambda c: c['result'].update(task_id='another-task'),
            lambda c: c['result'].update(outputs=['bad']),
            lambda c: c['result'].update(outputs={'artifact_refs': []}),
            lambda c: c['result'].update(outputs={'last_message': '../outside.txt'}),
        ):
            bad = copy.deepcopy(context)
            mutate(bad)
            with self.assertRaises(ValueError):
                _format_feedback(replace(command, payload={'format_context': bad}))

    def test_non_object_and_unparseable_first_round_responses_are_preserved_as_reports(self):
        for index, raw in enumerate(('null', '42', '"text"', '[]', '{invalid')):
            with self.subTest(raw=raw):
                command = self.command('migration_inventory', 'malformed-' + str(index))
                def fake(_handler, cmd):
                    output = self.root / 'logs' / (cmd.command_id + '.txt')
                    output.write_text(raw)
                    return handlers._result(cmd, 'completed', outputs={'last_message': str(output)})
                with patch('modport.handlers.CodexStageHandler.__call__', fake):
                    result = PlanningHandler()(command)
                self.assertEqual('completed', result.status, result.detail)
                sealed = json.loads((self.root / result.outputs['artifact_refs']['migration_inventory']['path']).read_text())
                self.assertEqual('raw_text', sealed['report_format'])
                self.assertEqual(raw, sealed['raw_report'])
                self.assertEqual(command.command_id, sealed['producer_execution_id'])
                self.assertEqual(set(command.artifact_refs), set(sealed['input_refs']))

    def test_v18_malformed_planning_report_is_diagnostic_not_a_gate(self):
        from modport.planning import PlanningHandler as CurrentPlanningHandler
        command = replace(self.command('migration_tasks', 'v18-malformed-report'),
                          options={'workflow_version': 18,
                                   'business_gates_disabled': True})

        def fake(_handler, cmd):
            output = self.root / 'logs' / 'v18-malformed-report.txt'
            output.parent.mkdir(exist_ok=True)
            output.write_text('{not a structured planning document')
            return handlers._result(cmd, 'completed', outputs={'last_message': str(output)})

        with patch('modport.handlers.CodexStageHandler.__call__', fake):
            result = CurrentPlanningHandler()(command)
        self.assertEqual('completed', result.status, result.detail)
        self.assertEqual('disabled', result.outputs['business_gates'])
        self.assertTrue(result.outputs['development_tasks'])
        self.assertTrue(result.outputs['diagnostics'])
        self.assertIn('planning_report:migration_tasks', result.outputs['artifact_refs'])
        self.assertIn('development_plan', result.outputs['artifact_refs'])

    def test_complete_failure_context_and_rounds_reach_actual_coder_prompt(self):
        self.repair_payload('target_diagnose')
        self.repair_context['prior_attempts'] = [{'failure_execution_id': 'previous-run-failure',
            'result': {'detail': 'previous onboarding repair', 'changed_paths': ['ClientCharacterization.java']}}]
        for stage in ('target_diagnose', 'target_repair_plan', 'target_repair_tasks', 'target_repair_review'):
            result = self.repair_round(stage)
            self.assertEqual('completed', result.status, result.detail)
            self.assertIn('previous onboarding repair', self.repair_prompts[stage])
            self.assertIn('ColorList reload race', self.repair_prompts[stage])
        self.assertIn('configuration watcher invalidates cache', self.repair_prompts['target_repair_plan'])
        self.assertIn('frozen characterization', self.repair_prompts['target_repair_tasks'])
        command = replace(self.command('target_revise'), payload=self.repair_payload('target_revise'))
        def coder(handler, cmd):
            package = json.loads(handler.prompt.split('Complete coder work package: ', 1)[1])
            self.assertEqual(self.repair_context, package['repair_context'])
            self.assertEqual(3, len(package['rounds']))
            diagnosis = json.loads(package['rounds'][0]['raw_report'])
            strategies = json.loads(package['rounds'][1]['raw_report'])
            tasks = json.loads(package['rounds'][2]['raw_report'])
            self.assertEqual('configuration watcher invalidates cache', diagnosis['failure_analysis']['cause'])
            self.assertEqual('frozen characterization', strategies['strategies'][0]['regression_method'])
            self.assertEqual(['issue'], tasks['tasks'][0]['issue_ids'])
            return handlers._result(cmd, 'completed')
        with patch('modport.handlers.CodexStageHandler.__call__', coder):
            result = PlannedRepairHandler('repair')(command)
        self.assertEqual('completed', result.status, result.detail)

    def test_large_repair_planner_explicitly_partitions_history(self):
        from modport.prompt_compressor import PromptCompressor, prompt_regions
        self.repair_payload('target_diagnose')
        history = 'historical failure evidence ' * 100000
        self.repair_context['prior_attempts'] = [{'detail': history}]
        result = self.repair_round('target_diagnose')
        self.assertEqual('completed', result.status, result.detail)
        prompt = self.repair_prompts['target_diagnose']
        active, historical, protected = prompt_regions(prompt)
        self.assertIn(history, historical)
        self.assertNotIn(history, active)
        self.assertIn('no JSON schema or identity echo is required', protected)
        self.assertIn('Planning input manifest', protected)
        command = replace(self.command('target_diagnose', identifier='large-partition'),
                          payload=self.repair_payload('target_diagnose'))
        from modport.prompt_compressor import DirectApiSummaryBackend
        backend = DirectApiSummaryBackend(lambda request: json.dumps({
            'objective': 'Diagnose the current target failure',
            'important_details': ['Complete historical evidence remains available in the manifest'],
            'completed': [], 'active': ['Diagnose the failed repair'], 'blocked': [],
            'next_steps': ['Read authenticated evidence'], 'evidence_refs': []}), tool_free=True)
        compressed = PromptCompressor(catalog={'models': [{'slug': 'test-model', 'context_window': 272000}]},
                                      summary_backend=backend, summary_model='test-model').compress(
            prompt, model='test-model', command=command, root=self.root, worktree=self.work)
        self.assertTrue(compressed.text.startswith(active))
        self.assertTrue(compressed.text.endswith(protected))
        self.assertLessEqual(len(compressed.text.encode()), 190400)

    def test_missing_context_stops_before_planner_call(self):
        command = replace(self.command('target_diagnose'), payload={'repair_generation': 1, 'repair_scope': 'target'})
        with patch('modport.handlers.CodexStageHandler.__call__') as agent:
            result = PlanningHandler()(command)
        self.assertEqual('blocked', result.status)
        agent.assert_not_called()

    def test_migration_replanning_sends_full_package_to_individual_coders(self):
        from modport.prompts import build_prompt
        self.replanning_payload = self.repair_payload('migration_inventory')
        self.chain()
        approved_task = approved_development_plan(self.command('implementation'))['tasks'][0]
        command = replace(self.command('coder'), payload={**self.command('coder').payload,
                          'development_task': approved_task})
        prompt = build_prompt('assigned task a', command, self.root, {},
                              {'rubric_id': 'rubric', 'rubric_version': 1})
        self.assertIn('Current assigned repair requirements:', prompt)
        self.assertIn('Authenticated repair context references', prompt)
        for alias in ('migration_inventory', 'migration_plan', 'migration_tasks', 'parallel_review', 'repair_work_package'):
            self.assertIn(self.refs[alias]['path'], prompt)
            self.assertIn(self.refs[alias]['sha256'], prompt)
        package = json.loads((self.root / self.refs['repair_work_package']['path']).read_text())
        self.assertEqual('ColorList reload race', package['repair_context']['current_failure']['result']['detail'])
        diagnosis = json.loads(package['rounds'][0]['raw_report'])
        self.assertEqual('configuration watcher invalidates cache', diagnosis['failure_analysis']['cause'])
        self.assertIn('frozen characterization', prompt)
        self.assertIn('source_task_ids', prompt)
        self.assertIn('report unexpected configuration reload behavior', prompt)

    def test_work_package_rejects_changed_failure_context(self):
        for stage in ('target_diagnose', 'target_repair_plan', 'target_repair_tasks', 'target_repair_review'):
            self.assertEqual('completed', self.repair_round(stage).status)
        command = replace(self.command('target_revise'), payload=self.repair_payload('target_revise'))
        changed = copy.deepcopy(command.payload)
        changed['repair_context']['current_failure']['result']['detail'] = 'unrelated failure'
        changed['rework_context'] = changed['repair_context']['current_failure']
        with self.assertRaisesRegex(ValueError, 'differs from diagnosis.*failure context'):
            require_repair_plan(replace(command, payload=changed))

    def test_work_package_rejects_changed_candidate(self):
        for stage in ('target_diagnose', 'target_repair_plan', 'target_repair_tasks', 'target_repair_review'):
            self.assertEqual('completed', self.repair_round(stage).status)
        command = replace(self.command('target_revise'), payload=self.repair_payload('target_revise'))
        (self.work / 'a').write_text('new candidate\n')
        git(self.work, 'add', '.'); git(self.work, 'commit', '-m', 'new candidate')
        with self.assertRaisesRegex(ValueError, 'candidate commit changed'):
            require_repair_plan(command)

    def test_changed_package_never_reaches_coder(self):
        for stage in ('target_diagnose', 'target_repair_plan', 'target_repair_tasks', 'target_repair_review'):
            self.assertEqual('completed', self.repair_round(stage).status)
        command = replace(self.command('target_revise'), payload=self.repair_payload('target_revise'))
        ref = self.refs['repair_work_package']
        path = self.root / ref['path']
        body = json.loads(path.read_text())
        body['repair_context']['prior_attempts'] = [{'invented': True}]
        # Even a freshly hashed but semantically mismatched replacement fails.
        self.refs['repair_work_package'] = _artifact(self.command('replacement'), 'replacement.json', json.dumps(body).encode())
        with patch('modport.handlers.CodexStageHandler.__call__') as coder:
            result = PlannedRepairHandler('repair')(replace(command, artifact_refs=self.refs))
        coder.assert_not_called()
        self.assertEqual('blocked', result.status)
        self.assertIn('differs from diagnosis', result.detail)

    def test_task_type_errors_arrive_together_from_schema_validation(self):
        from modport.planning_schema import PlanningValidationError
        self.workflow_version = 11
        for stage in ('target_diagnose', 'target_repair_plan'):
            kind = 'migration_inventory' if stage.endswith('diagnose') else 'migration_plan'
            self.validate_legacy_round(stage, mutation=lambda doc, kind=kind:
                self.scoped_document(kind, doc, current_stage='target_revise',
                                     inventory_stage='target_diagnose', task_stage='target_repair_tasks'))
        def malformed(doc):
            self.scoped_document('migration_tasks', doc, current_stage='target_revise',
                                 inventory_stage='target_diagnose', task_stage='target_repair_tasks')
            immediate = next(task for task in doc['tasks'] if task['kind'] != 'deferred')
            immediate['validation_checks'][0]['acceptance'] = immediate['acceptance'][0]
            immediate['inputs'] = 'input prose instead of array'
        with self.assertRaises(PlanningValidationError) as raised:
            self.validate_legacy_round('target_repair_tasks', identifier='bad-task-wire',
                                       mutation=malformed)
        errors = raised.exception.diagnostics
        self.assertTrue(any(error['path'].endswith('/validation_checks/0/acceptance') for error in errors))
        self.assertTrue(any(error['path'].endswith('/inputs') for error in errors))

    def test_explicit_handoff_contradictions_return_to_planner(self):
        for stage in ('target_diagnose', 'target_repair_plan'):
            self.validate_legacy_round(stage)
        from modport.planning import PlanningHandoffConflict
        with self.assertRaises(PlanningHandoffConflict) as raised:
            self.validate_legacy_round('target_repair_tasks', mutation=lambda d:
                d['consistency_review'].update(
                    consistent=False, contradictions=['task drops required regression']))
        self.assertEqual('target_repair_plan', raised.exception.replan_stage)
        self.assertEqual(['task drops required regression'],
                         raised.exception.review['contradictions'])

    def test_real_policy_and_handlers_preserve_scalinghealth_failure_across_cycles(self):
        from modport.operations import MigrationOperations
        from modport.models import Budget, MigrationRequest
        from modport.workflow import WorkflowDefinition
        operations = MigrationOperations()
        request = MigrationRequest('scalinghealth', 'https://example.invalid/mod', '1.20.1', '26.1.2',
                                   budget=Budget(max_agent_assignments=100))
        header = {'run_dir': str(self.root), 'request': request.to_dict(), 'initial_refs': self.refs,
                  'prior_findings': [], 'definition': WorkflowDefinition(
                      request.to_dict(), version=18).to_dict(),
                  'deadline_epoch': None, 'rubric_sha256': 'b' * 64, 'registry_revision': 'c' * 64}
        header['definition']['workflow_version'] = 11  # frozen inline-context protocol
        snapshot = {'run_id': 'run', 'tasks': {}, 'waits': {}, 'state': 'running'}
        app = operations._new_application()
        log = self.root / 'logs' / 'client.log'
        log.parent.mkdir(exist_ok=True)

        def operation(changes):
            return OperationInput.from_dict(next(change['command']['payload'] for change in changes
                if change['kind'] in ('add_task', 'new_attempt')))

        def settle(command, result):
            operations._capture_repair_result(header, app, result.to_dict())
            self.refs.update(result.outputs.get('artifact_refs', {}))
            app['effective'][command.stage_id] = result.to_dict()
            app['history'].append({'execution_id': command.command_id, 'stage': command.stage_id})
            snapshot['tasks'].setdefault(command.stage_id, {'attempts': []})['attempts'].append(
                {'state': 'succeeded', 'result': {'value': result.to_dict()},
                 'command': {'execution_id': command.command_id, 'payload': command.to_dict()}})

        for cycle in (1, 2):
            # Excerpt of the observed baseline failure; no Minecraft/model execution.
            raw = ('Config file scalinghealth-client.toml changed, sending notifies\n'
                   'java.lang.AssertionError: sh.presentation: ColorList caches until recalculation '
                   '{actual=[4478310], expected=[1122867]}\n' + f'attempt={cycle}\n')
            log.write_text(raw)
            failed = self.command('client_smoke', identifier=f'failed-{cycle}')
            failure = handlers._result(failed, 'failed', detail='ColorList caches until recalculation',
                error_code='target_contract_failed', outputs={'artifact_refs': {
                    'client_log': {'path': 'logs/client.log', 'media_type': 'text/plain'}}})
            app['effective']['client_smoke'] = failure.to_dict()
            app['history'].append({'execution_id': failed.command_id, 'stage': 'client_smoke'})
            command = operation(operations._repair_failure(snapshot, header, app,
                'client_smoke', failure, failed.command_id))
            context = command.payload['repair_context']
            self.assertEqual(cycle - 1, len(context['prior_attempts']))
            original_ref = context['current_failure']['result']['outputs']['artifact_refs']['client_log']
            log.write_text('later execution overwrote the mutable log\n')
            self.assertEqual(raw, (self.root / original_ref['path']).read_text())
            for index, stage in enumerate(('target_diagnose', 'target_repair_plan', 'target_repair_tasks', 'target_repair_review')):
                self.assertEqual(stage, command.stage_id)
                def planner(handler, cmd):
                    self.assertIn('ColorList caches until recalculation', handler.prompt)
                    self.assertEqual(context, cmd.payload['repair_context'])
                    expected, _, _ = _context(cmd, self.work)
                    doc = {**expected, 'base_commit': git(self.work, 'rev-parse', 'HEAD'),
                           **self.repair_body(('migration_inventory', 'migration_plan', 'migration_tasks', 'parallel_review')[index], task_stage='target_repair_tasks')}
                    if index == 0:
                        for issue in doc['issues']:
                            issue.update(resolution_scope='current', resolution_stage='target_revise',
                                         scope_reason='Repair the observed cache invalidation failure')
                    elif index == 2:
                        for task in doc['tasks']:
                            task['validation_checks'] = [{'id': task['id'] + ':regression',
                                'type': 'gradle_tasks', 'tasks': ['test'], 'acceptance': task['acceptance']}]
                    elif index == 3:
                        tasks = self.generated_documents['target_repair_tasks']['tasks']
                        for group in doc['development_plan']['tasks']:
                            group['validation_checks'] = [check for task in tasks
                                if task['id'] in group['source_task_ids'] for check in task['validation_checks']]
                    self.generated_documents[stage] = copy.deepcopy(doc)
                    path = self.root / 'logs' / (cmd.command_id + '.txt')
                    path.write_text(json.dumps(doc))
                    return handlers._result(cmd, 'completed', outputs={'last_message': str(path)})
                with patch('modport.handlers.CodexStageHandler.__call__', planner):
                    result = PlanningHandler()(command)
                self.assertEqual('completed', result.status, result.detail)
                settle(command, result)
                next_stage = ('target_repair_plan', 'target_repair_tasks', 'target_repair_review', 'target_revise')[index]
                command = operation(operations._schedule(snapshot, header, app, next_stage))
            self.assertTrue(require_repair_plan(command))
            self.assertEqual(context, command.payload['repair_context'])
            report = _artifact(command, 'coder-report.json', json.dumps({'changed_paths': ['a'],
                'reason': 'separate cache semantics from configuration watcher'}).encode())
            settle(command, handlers._result(command, 'completed', outputs={'artifact_refs': {'repair_report': report}}))
            if cycle == 2:
                previous = context['prior_attempts'][0]
                stages = {result['stage_id'] for result in previous['stage_results'].values()}
                self.assertTrue({'target_diagnose', 'target_repair_plan', 'target_repair_tasks', 'target_repair_review', 'target_revise'} <= stages)

    def test_malformed_contract_structure_remains_diagnosable_with_exact_bytes(self):
        baseline = self.root / 'baseline'
        git(self.root, 'clone', str(self.work), str(baseline))
        (baseline / '.modport').mkdir()
        candidate = baseline / '.modport/functional-contract.json'
        variants = [{'behaviors': None}, {'contract': None}, {'behaviors': [None]},
                    {'behaviors': [{'id': 'behavior', 'test_mapping': None}]}]
        for index, value in enumerate(variants):
            with self.subTest(value=value):
                raw = json.dumps(value).encode()
                candidate.write_bytes(raw)
                result = self.repair_round('contract_diagnose', identifier='malformed-' + str(index))
                self.assertEqual('completed', result.status, result.detail)
                sealed = json.loads((self.root / result.outputs['artifact_refs']['contract_diagnose']['path']).read_text())
                self.assertNotIn('contract_sha256', sealed)
                self.assertEqual(raw, candidate.read_bytes())
                self.assertEqual('source_evidence', sealed['input_refs']['source_evidence'])
                with self.assertRaises(ValueError):
                    _obligations(value, {'rules': []})

    def test_task_path_normalization_survives_fourth_round_and_repair_execution(self):
        for stage in ('migration_inventory', 'migration_plan'):
            self.assertEqual('completed', self.run_stage(stage).status)
        result = self.run_stage('migration_tasks', mutation=lambda d: d['tasks'][0].update(owned_paths=['a/']))
        self.assertEqual('completed', result.status, result.detail)
        sealed = json.loads((self.root / self.refs['migration_tasks']['path']).read_text())
        self.assertEqual(['a/'], json.loads(sealed['raw_report'])['tasks'][0]['owned_paths'])
        self.assertEqual('completed', self.run_stage('parallel_review').status)
        reviewed = approved_development_plan(self.command('implementation'))
        self.assertEqual(['a'], reviewed['tasks'][0]['owned_paths'])
        self.assertEqual('completed', ImplementationHandler()(self.command('implementation')).status)
        for stage in ('target_diagnose', 'target_repair_plan'):
            result = self.repair_round(stage)
            self.assertEqual('completed', result.status, result.detail)
        result = self.repair_round('target_repair_tasks', mutation=lambda d: d['tasks'][0].update(owned_paths=['a/']))
        self.assertEqual('completed', result.status, result.detail)
        result = self.repair_round('target_repair_review')
        self.assertEqual('completed', result.status, result.detail)
        command = replace(self.command('target_revise'), payload=self.repair_payload('target_revise'))
        self.assertEqual(['a'], require_repair_plan(command)[0]['owned_paths'])
        self.assertEqual(['a'], approved_repair_development_plan(command)['tasks'][0]['owned_paths'])

    def test_malformed_nested_structured_documents_fail_validation(self):
        for stage in ('migration_inventory', 'migration_plan', 'migration_tasks'):
            self.validate_legacy_round(stage)
        mutations = [lambda d: d.update(coupling_checks=[None]),
                     lambda d: d.update(coupling_checks=['wrong type']),
                     lambda d: d['coupling_checks'][0].update(groups=None),
                     lambda d: d['coupling_checks'][0].update(evidence={'unexpected': 'object'}),
                     lambda d: d['development_plan'].update(tasks=[None])]
        for index, mutate in enumerate(mutations):
            with self.assertRaises(ValueError):
                self.validate_legacy_round('parallel_review',
                                           identifier='malformed-review-' + str(index),
                                           mutation=mutate)
        with self.assertRaises(ValueError):
            self.validate_legacy_round('migration_inventory', identifier='bad-summary',
                                       mutation=lambda d: d['issues'][0].update(summary={'not': 'text'}))

    def test_repair_chain_ignores_replaced_derived_summaries_but_binds_failure_refs(self):
        cmd = self.command('migration_inventory', identifier='derived-inputs')
        initial = _artifact(cmd, 'old-summary.txt', b'old summary')
        self.refs.update({'planning_summary:dependencies': initial,
                          'rework:planning_summary:dependencies': initial,
                          'format:agent_log:target_repair_plan': initial,
                          'rework:failure': _artifact(cmd, 'failure.json', b'{"error":"original failure"}')})
        result = self.repair_round('target_diagnose')
        self.assertEqual('completed', result.status, result.detail)
        diagnosis = json.loads((self.root / self.refs['target_diagnose']['path']).read_text())
        self.assertNotIn('planning_summary:dependencies', diagnosis['input_refs'])
        self.assertNotIn('rework:planning_summary:dependencies', diagnosis['input_refs'])
        self.assertNotIn('format:agent_log:target_repair_plan', diagnosis['input_refs'])
        self.assertEqual('rework:failure', diagnosis['input_refs']['rework:failure'])
        result = self.repair_round('target_repair_plan')
        self.assertEqual('completed', result.status, result.detail)
        self.refs['rework:planning_summary:dependencies'] = self.refs['planning_summary:dependencies']
        result = self.repair_round('target_repair_tasks')
        self.assertEqual('completed', result.status, result.detail)
        result = self.repair_round('target_repair_review')
        self.assertEqual('completed', result.status, result.detail)
        self.refs['rework:failure'] = _artifact(cmd, 'changed-failure.json', b'{"error":"different failure"}')
        command = replace(self.command('target_revise'), payload=self.repair_payload('target_revise'))
        with self.assertRaisesRegex(ValueError, 'repair input reference changed'):
            require_repair_plan(command)
        del self.refs['rework:failure']
        command = replace(command, artifact_refs=self.refs)
        with self.assertRaisesRegex(ValueError, 'planning input reference is missing'):
            require_repair_plan(command)

    def test_consecutive_local_replans_preserve_authenticated_prior_evidence(self):
        from modport.operations import MigrationOperations
        operations = object.__new__(MigrationOperations)
        app = operations._new_application()
        header = {'initial_refs': dict(self.refs)}

        def run(stage, identifier, mutation=None):
            self.refs = operations._refs(header, app)
            result = self.run_stage(stage, identifier=identifier, mutation=mutation)
            self.assertEqual('completed', result.status, result.detail)
            app['effective'][stage] = result.to_dict()
            return result

        def replan(identifier, destination):
            result = run('parallel_review', identifier,
                         lambda doc: doc.update(parallel_decision='replan', replan_stage=destination))
            operations._rework_context(app, 'parallel_review', result, identifier)
            self.assertEqual(destination, operations._invalidate_planning(app, destination))

        run('migration_inventory', 'inventory-1')
        run('migration_plan', 'plan-1')
        run('migration_tasks', 'tasks-1')
        replan('review-1', 'migration_plan')
        run('migration_plan', 'plan-2')
        run('migration_tasks', 'tasks-2')
        replan('review-2', 'migration_tasks')
        run('migration_tasks', 'tasks-3')
        run('parallel_review', 'review-3')

        self.refs = operations._refs(header, app)
        plan = json.loads((self.root / self.refs['migration_plan']['path']).read_text())
        permanent = 'rework_evidence:review-1:parallel_review'
        self.assertEqual(permanent, plan['input_refs'][permanent])
        self.assertNotIn('rework:parallel_review', plan['input_refs'])
        self.assertNotEqual(self.refs[permanent]['sha256'], self.refs['rework:parallel_review']['sha256'])
        self.assertEqual('completed', ImplementationHandler()(self.command('implementation')).status)

        # Refreshing historical evidence through its alias is allowed.
        app['rework_evidence'][permanent] = _artifact(
            self.command('parallel_review', identifier='replacement'), 'replacement.json', b'{"changed":true}')
        self.refs = operations._refs(header, app)
        result = self.run_stage('parallel_review', identifier='tampered-history')
        self.assertEqual('completed', result.status, result.detail)


if __name__ == '__main__':
    unittest.main()
