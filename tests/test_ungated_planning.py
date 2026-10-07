"""Business reports cannot reject execution or demand another author pass."""

import copy
from dataclasses import replace
from hashlib import sha256
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from modport.contracts import OperationInput
from modport.execution_plan import normalize_execution_plan
from modport.handlers import _result
from modport.planning import PlanningHandler, approved_repair_development_plan
from modport.ungated_planning import available_execution_plan
from modport.workflow import WORKFLOW_VERSION
from test_development import git


class ExecutionPlanTests(unittest.TestCase):
    def test_check_alias_survives_report_normalization(self):
        checks = [{'id': 'compile', 'argv': ['./gradlew', 'compileJava']}]
        plan = normalize_execution_plan({'tasks': [
            {'id': 'repair', 'objective': 'Repair API', 'dependencies': [], 'checks': checks},
            {'id': 'explicit', 'objective': 'Inspect API', 'dependencies': [],
             'checks': checks, 'validation_checks': []},
        ]}, workflow_version=WORKFLOW_VERSION)
        self.assertEqual(checks, plan['tasks'][0]['validation_checks'])
        self.assertEqual([], plan['tasks'][1]['validation_checks'])

    def test_markdown_embedded_plan_keeps_dependencies_and_issue_details(self):
        tasks = [
            {'id': 'caller', 'objective': 'Update Client.java to call the replacement API',
             'dependencies': ['provider'], 'owned_paths': ['src/Client.java'],
             'issue_ids': ['issue-caller'], 'validation_checks': []},
            {'id': 'provider', 'objective': 'Replace the removed registration method in Api.java',
             'dependencies': [], 'owned_paths': ['src/Api.java'], 'issue_ids': ['issue-api']},
        ]
        report = '# Findings\nEvidence follows.\n```json\n' + json.dumps(
            {'development_plan': {'tasks': tasks}}) + '\n```\n## Evidence\nSee inventory.json.\n'
        plan = normalize_execution_plan(report, workflow_version=WORKFLOW_VERSION)
        self.assertEqual(['provider', 'caller'], [t['id'] for t in plan['tasks']])
        self.assertEqual(['provider'], plan['tasks'][1]['dependencies'])
        self.assertEqual(['issue-caller'], plan['tasks'][1]['issue_ids'])
        self.assertEqual([], plan['diagnostics'])

    def test_multiple_distinct_markdown_plans_keep_visible_fallback(self):
        def block(identifier):
            return '```json\n' + json.dumps({'tasks': [
                {'id': identifier, 'objective': identifier, 'dependencies': []}]}) + '\n```\n'
        report = '# Alternative plans\n' + block('one') + block('two')
        plan = normalize_execution_plan(report, workflow_version=WORKFLOW_VERSION)
        self.assertEqual(['execute-plan'], [t['id'] for t in plan['tasks']])
        self.assertEqual(report, plan['tasks'][0]['objective'])
        self.assertIn('ambiguous_embedded_task_plans', {d['code'] for d in plan['diagnostics']})

    def test_markdown_examples_are_not_selected_as_task_plans(self):
        report = ('# Context\n```json\n{"status":"unverified"}\n```\n'
                  '~~~JSON\n{"tasks":[{"id":"repair","objective":"Repair the API",'
                  '"dependencies":[]}]}\n~~~\nRead evidence before editing.')
        plan = normalize_execution_plan(report, workflow_version=WORKFLOW_VERSION)
        self.assertEqual(['repair'], [t['id'] for t in plan['tasks']])

    def test_contract_and_project_tasks_are_executable_without_checks(self):
        source = {'development_plan': {'tasks': [
            {'id': 'A/CR1', 'objective': 'Repair client launch',
             'owned_paths': ['.modport/characterization.init.gradle']},
            {'id': 'B1', 'objective': 'Update target build and source',
             'owned_paths': ['build.gradle', 'src/main/java/**'],
             'dependencies': ['A/CR1'], 'validation_checks': []},
        ]}, 'parallel_decision': 'rejected'}
        original = copy.deepcopy(source)
        plan = normalize_execution_plan(source, base_commit='a' * 40)
        self.assertEqual(['A-CR1', 'B1'], [task['id'] for task in plan['tasks']])
        self.assertEqual(['A-CR1'], plan['tasks'][1]['dependencies'])
        self.assertEqual(['build.gradle', 'src/main/java'], plan['tasks'][1]['owned_paths'])
        self.assertEqual([], plan['tasks'][1]['validation_checks'])
        self.assertEqual(source, original)

    def test_missing_settings_and_free_text_produce_work(self):
        text = 'Build the adapter and investigate the missing callback.'
        plan = normalize_execution_plan(text, base_commit='b' * 40)
        self.assertEqual(text, plan['tasks'][0]['objective'])
        self.assertEqual('execute-plan', plan['tasks'][0]['id'])
        self.assertEqual([], plan['tasks'][0]['owned_paths'])

    def test_cycles_and_missing_dependencies_are_diagnostics_not_replanning(self):
        source = {'tasks': [
            {'id': 'a', 'objective': 'A', 'dependencies': ['b', 'missing']},
            {'id': 'b', 'objective': 'B', 'dependencies': ['a']},
        ]}
        plan = normalize_execution_plan(source, base_commit='a' * 40)
        done = set()
        for task in plan['tasks']:
            self.assertTrue(set(task['dependencies']) <= done)
            done.add(task['id'])
        self.assertEqual({'a', 'b'}, done)
        self.assertEqual({'unavailable_dependency', 'dependency_cycle_ordered'},
                         {item['code'] for item in plan['diagnostics']})

    def test_overlapping_declared_files_order_tasks_without_rejection(self):
        plan = normalize_execution_plan({'tasks': [
            {'id': 'a', 'objective': 'A', 'owned_paths': ['src/**']},
            {'id': 'b', 'objective': 'B', 'owned_paths': ['src/B.java']},
        ]})
        self.assertEqual(['a'], plan['tasks'][1]['dependencies'])

    def test_external_paths_are_not_given_to_the_execution_workspace(self):
        plan = normalize_execution_plan({'tasks': [
            {'id': 'a', 'objective': 'A', 'owned_paths': ['../outside', '/etc/a', '.git/config', 'build.gradle']},
        ]})
        self.assertEqual(['build.gradle'], plan['tasks'][0]['owned_paths'])
        self.assertEqual(3, len(plan['diagnostics']))

    def test_v22_preserves_explicit_task_ids_and_dependency_edges(self):
        source = {'development_plan': {'tasks': [
            {'id': 'MP-02', 'objective': 'Implement client behavior',
             'dependencies': ['MP-01'], 'owned_paths': ['src/client/']},
            {'id': 'MP-01', 'objective': 'Establish shared API',
             'dependencies': [], 'owned_paths': ['src/common/']},
        ]}}
        plan = normalize_execution_plan(source, workflow_version=22)
        self.assertEqual(['MP-01', 'MP-02'], [task['id'] for task in plan['tasks']])
        self.assertEqual([[], ['MP-01']], [task['dependencies'] for task in plan['tasks']])
        self.assertNotIn('task_id_normalized', {row['code'] for row in plan['diagnostics']})

    def test_v22_never_invents_dependency_for_overlapping_paths(self):
        plan = normalize_execution_plan({'tasks': [
            {'id': 'MP-01', 'objective': 'Edit shared API', 'dependencies': [],
             'owned_paths': ['src/shared/']},
            {'id': 'MP-02', 'objective': 'Edit shared adapter', 'dependencies': [],
             'owned_paths': ['src/shared/Adapter.java']},
        ]}, workflow_version=22)
        self.assertEqual([[], []], [task['dependencies'] for task in plan['tasks']])
        self.assertIn('overlapping_paths_without_dependency',
                      {row['code'] for row in plan['diagnostics']})

    def test_v22_ambiguous_dags_fall_back_visibly_without_repair(self):
        cases = [
            [{'id': 'MP-01', 'objective': 'A', 'dependencies': []},
             {'id': 'MP-01', 'objective': 'Duplicate', 'dependencies': []}],
            [{'id': 'MP-01', 'objective': 'A', 'dependencies': ['missing']}],
            [{'id': 'MP-01', 'objective': 'A', 'dependencies': ['MP-02']},
             {'id': 'MP-02', 'objective': 'B', 'dependencies': ['MP-01']}],
            [{'id': 'MP/01', 'objective': 'Unsafe id', 'dependencies': []}],
            [{'id': 'MP-01', 'objective': 'Unspecified edges'}],
        ]
        for tasks in cases:
            with self.subTest(tasks=tasks):
                source = {'tasks': tasks}
                plan = normalize_execution_plan(source, workflow_version=22)
                self.assertEqual(['execute-plan'], [task['id'] for task in plan['tasks']])
                diagnostic = next(row for row in plan['diagnostics']
                                  if row['code'] == 'task_settings_unavailable')
                self.assertEqual('single_task', diagnostic['fallback'])
                self.assertIn(json.dumps(source, ensure_ascii=False, sort_keys=True),
                              plan['tasks'][0]['objective'])

    def test_v22_free_text_is_not_guessed_into_work_packages(self):
        report = ('MP-01: update registration. MP-02 depends on MP-01: test client reload.')
        plan = normalize_execution_plan(report, workflow_version=22)
        self.assertEqual(1, len(plan['tasks']))
        self.assertEqual('execute-plan', plan['tasks'][0]['id'])
        self.assertEqual(report, plan['tasks'][0]['objective'])
        self.assertEqual('single_task', plan['diagnostics'][0]['fallback'])


class UngatedPlanningTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.baseline = self.root / 'baseline'
        self.baseline.mkdir()
        git(self.baseline, 'init')
        (self.baseline / 'build.gradle').write_text('// source build\n')
        git(self.baseline, 'add', '.')
        git(self.baseline, 'commit', '-m', 'source')
        self.worktree = self.root / 'worktree'
        self.worktree.mkdir()
        git(self.worktree, 'init')
        (self.worktree / 'build.gradle').write_text('// migration build\n')
        git(self.worktree, 'add', '.')
        git(self.worktree, 'commit', '-m', 'migration')
        self.head = git(self.worktree, 'rev-parse', 'HEAD')
        (self.root / 'logs').mkdir()
        self.refs = {}
        self.calls = []

    def command(self, stage, *, upstream=None, version=17, refs=None):
        return OperationInput('run', stage, stage, 'run.' + stage, str(self.root),
                              options={'workflow_version': version},
                              artifact_refs=self.refs if refs is None else refs,
                              upstream_results=upstream or {})

    def artifact(self, name, value):
        path = self.root / 'logs' / name
        raw = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
        path.write_text(raw)
        return {'path': path.relative_to(self.root).as_posix(),
                'sha256': sha256(raw.encode()).hexdigest(),
                'media_type': 'application/json'}

    def run_stage(self, stage, raw, status='completed'):
        def author(handler, command):
            self.calls.append(command.stage_id)
            output = self.root / 'logs' / (command.command_id + '.txt')
            output.write_text(raw)
            return _result(command, status, outputs={'last_message': str(output)},
                           error_code='author_failure' if status != 'completed' else None)
        with patch('modport.handlers.CodexStageHandler.__call__', author):
            result = PlanningHandler()(self.command(stage))
        self.refs.update(result.outputs.get('artifact_refs', {}))
        return result

    def run_v22_migration_plan(self, report):
        calls = []

        def author(handler, command):
            calls.append(command.stage_id)
            return _result(command, 'completed', outputs={'raw_report': report})

        with patch('modport.handlers.CodexStageHandler.__call__', author):
            result = PlanningHandler()(self.command('migration_plan', version=22, refs={}))
        return result, calls

    def test_current_prompt_markdown_handoff_reaches_frozen_task_artifact(self):
        report = '# Concrete changes\n```json\n' + json.dumps({'development_plan': {'tasks': [
            {'id': 'API', 'objective': 'Replace missing Registry.old in src/Api.java; verify registration',
             'owned_paths': ['src/Api.java'], 'dependencies': [], 'issue_ids': ['issue-api'],
             'acceptance': ['registration occurs once'], 'validation_checks': []},
            {'id': 'CLIENT', 'objective': 'Update src/Client.java to use the replacement Registry method',
             'owned_paths': ['src/Client.java'], 'dependencies': ['API'], 'issue_ids': ['issue-client']},
        ]}}) + '\n```\n## Evidence\nSee the locked target source.\n'
        observed = []

        def author(handler, command):
            observed.append(handler.prompt)
            return _result(command, 'completed', outputs={'raw_report': report})

        with patch('modport.handlers.CodexStageHandler.__call__', author):
            result = PlanningHandler()(self.command('migration_plan', version=WORKFLOW_VERSION, refs={}))
        self.assertEqual(1, len(observed))
        self.assertIn('exactly one JSON code block', observed[0])
        self.assertIn('issue_ids', observed[0])
        self.assertIn('validation_checks', observed[0])
        self.assertIn('source path and class/method', observed[0])
        tasks = result.outputs['development_tasks']
        self.assertEqual(['API', 'CLIENT'], [task['id'] for task in tasks])
        self.assertEqual(['API'], tasks[1]['dependencies'])
        self.assertEqual(['issue-client'], tasks[1]['issue_ids'])
        sealed = json.loads((self.root / result.outputs['artifact_refs']['development_plan']['path']).read_text())
        self.assertEqual(tasks, sealed['tasks'])

    def test_v22_planning_handoff_keeps_explicit_dag_and_free_text_falls_back_once(self):
        structured = json.dumps({'development_plan': {'tasks': [
            {'id': 'MP-02', 'objective': 'Update the client adapter',
             'dependencies': ['MP-01'], 'owned_paths': ['src/client/']},
            {'id': 'MP-01', 'objective': 'Add the shared registration API',
             'dependencies': [], 'owned_paths': ['src/common/']},
        ]}})
        result, calls = self.run_v22_migration_plan(structured)
        self.assertEqual(['migration_plan'], calls)
        self.assertEqual(['MP-01', 'MP-02'],
                         [task['id'] for task in result.outputs['development_tasks']])
        self.assertEqual([[], ['MP-01']],
                         [task['dependencies'] for task in result.outputs['development_tasks']])
        plan_ref = result.outputs['artifact_refs']['development_plan']
        sealed = json.loads((self.root / plan_ref['path']).read_text())
        self.assertEqual(result.outputs['development_tasks'], sealed['tasks'])

        self.setUp()
        free_text = 'MP-01: register the adapter; MP-02 depends on MP-01 and adds reload checks.'
        fallback, fallback_calls = self.run_v22_migration_plan(free_text)
        self.assertEqual(['migration_plan'], fallback_calls)
        self.assertEqual(1, len(fallback.outputs['development_tasks']))
        task, = fallback.outputs['development_tasks']
        self.assertEqual('execute-plan', task['id'])
        self.assertEqual(free_text, task['objective'])
        self.assertTrue(any(row.get('code') == 'task_settings_unavailable'
                            and row.get('fallback') == 'single_task'
                            for row in fallback.outputs['diagnostics']))

    def test_full_contract_plan_runs_without_contract_schema_or_approval(self):
        self.run_stage('contract_diagnose', 'The task lookup is too early.')
        self.run_stage('contract_repair_plan', 'Repair registration and update the target build.')
        result = self.run_stage('contract_repair_tasks', json.dumps({'development_plan': {'tasks': [
            {'id': 'launch', 'objective': 'Repair launch', 'owned_paths': ['.modport/characterization.init.gradle']},
            {'id': 'target', 'objective': 'Update build', 'owned_paths': ['build.gradle']},
        ]}}))
        self.assertEqual('completed', result.status, result.detail)
        self.run_stage('contract_repair_review', 'Rejected: the reports still have missing evidence.')
        plan = approved_repair_development_plan(self.command('contract_revise'))
        self.assertEqual(['launch', 'target'], [task['id'] for task in plan['tasks']])
        self.assertEqual(4, len(self.calls))

    def test_failed_author_retains_failure_and_supplies_partial_tasks(self):
        result = self.run_stage('contract_repair_tasks', json.dumps({'tasks': [
            {'id': 'build', 'objective': 'Repair build', 'owned_paths': ['build.gradle']},
        ]}), status='failed')
        self.assertEqual('failed', result.status)
        self.assertEqual('author_failure', result.error_code)
        self.assertEqual('build', result.outputs['development_tasks'][0]['id'])
        self.assertIn('development_plan', result.outputs['artifact_refs'])
        self.assertEqual(['contract_repair_tasks'], self.calls)

    def test_plain_synthesis_report_is_carried_to_coder_without_format_retry(self):
        text = 'Repair the client task registration using the existing Gradle adapter.'
        result = self.run_stage('contract_repair_tasks', text)
        self.assertEqual('completed', result.status, result.detail)
        self.assertEqual(text, result.outputs['development_tasks'][0]['objective'])
        self.assertEqual(['contract_repair_tasks'], self.calls)

    def test_failed_historical_synthesis_text_can_feed_dispatch(self):
        report = self.root / 'logs' / 'old-tasks.txt'
        report.write_text(json.dumps({'development_plan': {'tasks': [
            {'id': 'B1', 'objective': 'Repair target build', 'owned_paths': ['build.gradle']},
        ]}}))
        result = _result(self.command('contract_repair_tasks'), 'failed',
                         error_code='planning_output_invalid', outputs={'last_message': str(report)})
        command = self.command('contract_revise', upstream={'contract_repair_tasks': result.to_dict()})
        plan = approved_repair_development_plan(command)
        self.assertEqual(['build.gradle'], plan['tasks'][0]['owned_paths'])

    def test_large_reference_metadata_is_linked_without_inlining(self):
        raw = 'Current plan with original failure details.\n' * 10000
        path = self.root / 'logs/previous-plan.md'
        path.write_text(raw)
        self.refs['current_plan'] = {'path': 'logs/previous-plan.md',
            'sha256': sha256(raw.encode()).hexdigest(),
            'metadata': {'historical_input': 'nested previous execution\n' * 100000}}

        def author(handler, command):
            self.assertLess(len(handler.prompt.encode()), 10000)
            self.assertNotIn('nested previous execution', handler.prompt)
            self.assertNotIn(raw, handler.prompt)
            return _result(command, 'completed', outputs={'raw_report': 'Repair the source adapter.'})

        with patch('modport.handlers.CodexStageHandler.__call__', author):
            result = PlanningHandler()(self.command('contract_diagnose'))
        refs = result.outputs['artifact_refs']
        index = json.loads((self.root / refs['planning_input_index']['path']).read_text())
        self.assertNotIn('metadata', index['artifact_refs']['current_plan'])
        self.assertEqual(raw, (self.root / index['primary_report']['path']).read_text())

    def test_v19_quarantines_bad_refs_before_calling_planner(self):
        bad = self.artifact('bad.json', {'do_not_trust': True})
        bad['sha256'] = '0' * 64
        inventory = self.artifact('inventory.json', {'schema_version': 1,
            'candidate_id': self.head, 'issues': [],
            'source_scan_ref': bad})
        scan = self.artifact('scan.json', {'schema_version': 1, 'scan_complete': True,
            'skills': {}})
        nested = _result(self.command('early_compile'), 'failed', outputs={
            'artifact_refs': {'nested_bad': bad}, 'last_message': bad['path']})
        seen = []

        def author(handler, command):
            seen.append(command)
            self.assertNotIn('bad', command.artifact_refs)
            outputs = command.upstream_results['early_compile']['outputs']
            self.assertNotIn('nested_bad', outputs['artifact_refs'])
            self.assertNotIn('last_message', outputs)
            return _result(command, 'completed', outputs={'raw_report': ''})

        refs = {'migration_inventory': inventory, 'mod_scan_report': scan, 'bad': bad}
        with patch('modport.handlers.CodexStageHandler.__call__', author):
            result = PlanningHandler()(self.command('migration_plan', version=19,
                                                     refs=refs,
                                                     upstream={'early_compile': nested.to_dict()}))
        self.assertEqual(1, len(seen))
        index_ref = result.outputs['artifact_refs']['planning_input_index']
        index = json.loads((self.root / index_ref['path']).read_text())
        self.assertNotIn('bad', index['artifact_refs'])
        self.assertIn('artifact_refs:bad', index['unavailable_artifact_refs'])
        primary = result.outputs['artifact_refs']['planning_input_report']
        primary_text = (self.root / primary['path']).read_text()
        self.assertNotIn(bad['path'], primary_text)
        self.assertIn('artifact digest mismatch', primary_text)
        self.assertTrue(any(row.get('code') == 'planning_artifact_unavailable'
                            for row in result.outputs['diagnostics']))

    def test_v19_empty_plan_falls_back_to_authenticated_inventory_and_scan(self):
        inventory = self.artifact('inventory-facts.json', {'schema_version': 1,
            'candidate_id': self.head, 'issues': [{
            'rule_id': 'forge-package-reference', 'summary': 'Forge package remains',
            'locations': [{'path': 'src/Main.java', 'line': 7}]}]})
        scan = self.artifact('scan-facts.json', {'schema_version': 1, 'scan_complete': True,
            'skills': {'platform': {'report': {'scan_complete': True, 'findings': [{
                'rule_id': 'removed-api', 'path': 'src/Main.java', 'line': 9}],
                'known_gaps': ['event timing']}}}})

        def author(handler, command):
            return _result(command, 'completed', outputs={
                'raw_report': json.dumps({'development_plan': {'tasks': []}})})

        with patch('modport.handlers.CodexStageHandler.__call__', author):
            result = PlanningHandler()(self.command('migration_plan', version=19, refs={
                'migration_inventory': inventory, 'mod_scan_report': scan}))
        primary = result.outputs['artifact_refs']['planning_input_report']
        primary_text = (self.root / primary['path']).read_text()
        self.assertIn('forge-package-reference', primary_text)
        self.assertIn('removed-api', primary_text)
        objective = result.outputs['development_tasks'][0]['objective']
        self.assertIn('forge-package-reference', objective)
        self.assertIn('removed-api', objective)
        self.assertEqual('execute-plan', result.outputs['development_tasks'][0]['id'])

    def test_v19_inventory_candidate_mismatch_does_not_launch_model(self):
        inventory = self.artifact('mismatched-inventory.json', {'schema_version': 1,
            'candidate_id': 'a' * 40, 'issues': []})
        scan = self.artifact('mismatched-scan.json', {'schema_version': 1,
            'scan_complete': True, 'skills': {}})
        with patch('modport.handlers.CodexStageHandler.__call__') as author:
            result = PlanningHandler()(self.command('migration_plan', version=19, refs={
                'migration_inventory': inventory, 'mod_scan_report': scan}))
        author.assert_not_called()
        self.assertEqual('failed', result.status)
        self.assertEqual('candidate_identity_mismatch', result.error_code)

    def test_v19_missing_inventory_candidate_is_unavailable_not_a_retry(self):
        inventory = self.artifact('candidate-missing-inventory.json', {
            'schema_version': 1, 'issues': [{'rule_id': 'unbound-fact'}]})
        scan = self.artifact('candidate-missing-scan.json', {'schema_version': 1,
            'scan_complete': True, 'skills': {}})
        calls = []

        def author(handler, command):
            calls.append(command)
            self.assertNotIn('migration_inventory', command.artifact_refs)
            return _result(command, 'completed', outputs={'raw_report': json.dumps({
                'development_plan': {'tasks': [{'id': 'scan-only',
                    'objective': 'Use the authenticated scan facts'}]}})})

        with patch('modport.handlers.CodexStageHandler.__call__', author):
            result = PlanningHandler()(self.command('migration_plan', version=19, refs={
                'migration_inventory': inventory, 'mod_scan_report': scan}))
        self.assertEqual(1, len(calls))
        self.assertEqual('completed', result.status)
        index_ref = result.outputs['artifact_refs']['planning_input_index']
        index = json.loads((self.root / index_ref['path']).read_text())
        self.assertNotIn('migration_inventory', index['artifact_refs'])
        self.assertTrue(any(row.get('artifact') == 'migration_inventory'
                            and 'candidate_id' in row.get('detail', '')
                            for row in result.outputs['diagnostics']))

    def test_cleanup_failure_keeps_evidence_without_dispatchable_plan(self):
        inventory = self.artifact('cleanup-inventory.json', {'schema_version': 1,
            'candidate_id': self.head, 'issues': []})
        scan = self.artifact('cleanup-scan.json', {'schema_version': 1,
            'scan_complete': True, 'skills': {}})
        cleanup = self.artifact('opencode-cleanup.json', {
            'cleanup_confirmed': False, 'process_group_gone': False})

        def author(handler, command):
            return _result(command, 'failed', error_code='opencode_cleanup_unconfirmed',
                outputs={'artifact_refs': {'opencode_cleanup': cleanup}})

        with patch('modport.handlers.CodexStageHandler.__call__', author):
            result = PlanningHandler()(self.command('migration_plan', version=26, refs={
                'migration_inventory': inventory, 'mod_scan_report': scan}))
        self.assertEqual('failed', result.status)
        self.assertEqual('opencode_cleanup_unconfirmed', result.error_code)
        self.assertEqual(cleanup, result.outputs['artifact_refs']['opencode_cleanup'])
        self.assertIn('planning_input_index', result.outputs['artifact_refs'])
        self.assertNotIn('development_plan', result.outputs['artifact_refs'])
        self.assertNotIn('development_tasks', result.outputs)

    def test_v19_candidate_change_during_model_is_operational(self):
        inventory = self.artifact('stable-inventory.json', {'schema_version': 1,
            'candidate_id': self.head, 'issues': []})
        scan = self.artifact('stable-scan.json', {'schema_version': 1,
            'scan_complete': True, 'skills': {}})

        def author(handler, command):
            (self.worktree / 'changed.txt').write_text('explicit rework changed candidate\n')
            git(self.worktree, 'add', 'changed.txt')
            git(self.worktree, 'commit', '-m', 'change during planning')
            return _result(command, 'completed', outputs={'raw_report': 'stale plan'})

        with patch('modport.handlers.CodexStageHandler.__call__', author):
            result = PlanningHandler()(self.command('migration_plan', version=19, refs={
                'migration_inventory': inventory, 'mod_scan_report': scan}))
        self.assertEqual('failed', result.status)
        self.assertEqual('candidate_identity_mismatch', result.error_code)
        self.assertNotIn('development_plan', result.outputs.get('artifact_refs', {}))

    def test_v19_executable_plan_requires_matching_digest(self):
        plan = self.artifact('development-plan.json', {'schema_version': 1,
            'base_commit': git(self.worktree, 'rev-parse', 'HEAD'), 'tasks': [{
                'id': 'build', 'objective': 'Update target build',
                'owned_paths': ['build.gradle']}]})
        bad = dict(plan, sha256='f' * 64)
        with self.assertRaisesRegex(ValueError, 'digest mismatch'):
            available_execution_plan(self.command('implementation', version=19,
                                                   refs={'development_plan': bad}))
        resolved = available_execution_plan(self.command('implementation', version=19,
                                                          refs={'development_plan': plan}))
        self.assertEqual('build', resolved['tasks'][0]['id'])

    def test_legacy_plan_reference_keeps_path_only_behavior(self):
        plan = self.artifact('legacy-development-plan.json', {'schema_version': 1,
            'tasks': [{'id': 'legacy', 'objective': 'Keep legacy behavior'}]})
        plan['sha256'] = 'f' * 64
        resolved = available_execution_plan(self.command('implementation', version=17,
                                                          refs={'development_plan': plan}))
        self.assertEqual('legacy', resolved['tasks'][0]['id'])


if __name__ == '__main__':
    unittest.main()
