"""Current workflow routing and deterministic stages."""

from dataclasses import replace
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from modport import Budget, MigrationOperations, MigrationRequest
from modport.contracts import OperationInput
from modport.deterministic_stages import BuildPrepareHandler, CodemodHandler, EarlyCompileHandler, DeterministicInventoryHandler
from modport.handlers import _result
from modport.memory_admission import MemorySnapshot
from modport.workflow import WORKFLOW_VERSION, compile_migration_workflow, stage_routes
from fixtures_modport import registry
from test_development import git


def drive_run(operations, run, limit=180):
    with operations.session(run.run_dir, run.run_id) as (_, header, runtime, sdk):
        for _ in range(limit):
            runtime.reap()
            sdk.sync()
            state = operations.tick(sdk, header)
            if state['state'] != 'running' or any(
                    wait['state'] == 'open' for wait in state['waits'].values()):
                return state
            sdk.flush()
            runtime.run_once()
    raise AssertionError('fixture did not settle')


class BuildPrepareStageTests(unittest.TestCase):
    def setUp(self):
        import test_build_preparation
        from modport.development import _artifact
        self.fixture = test_build_preparation.BuildPreparationTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.root, self.work = self.fixture.root, self.fixture.worktree
        command = OperationInput('test', 'build_prepare', 'build_prepare', 'prepare', str(self.root),
                                 options={'workflow_version': 19})
        ref = _artifact(command, 'manifest.json', json.dumps(self.fixture.manifest.to_dict()).encode())
        self.command = replace(command, artifact_refs={'locked_manifest': ref})

    def test_supported_additions_are_committed_before_scanning(self):
        before = git(self.work, 'rev-parse', 'HEAD')
        result = BuildPrepareHandler()(self.command)
        self.assertEqual('completed', result.status, result.detail)
        self.assertEqual('produced', result.outputs['product_state'])
        self.assertNotEqual(before, git(self.work, 'rev-parse', 'HEAD'))
        self.assertEqual('', git(self.work, 'status', '--porcelain'))
        self.assertEqual(self.fixture.contents['build.gradle'], (self.work / 'build.gradle').read_text())
        patch_text = (self.root / result.outputs['artifact_refs']['build_preparation_patch']['path']).read_text()
        self.assertIn('GIT binary patch', patch_text)
        self.assertNotIn('Binary file addition:', patch_text)

    def test_custom_build_is_preserved_and_reported_for_semantic_merge(self):
        (self.work / 'build.gradle').write_text('customDependencyAndPackaging()\n')
        git(self.work, 'add', 'build.gradle')
        git(self.work, 'commit', '-m', 'custom build')
        before = git(self.work, 'rev-parse', 'HEAD')
        result = BuildPrepareHandler()(self.command)
        self.assertEqual('build_merge_unsupported', result.error_code)
        self.assertEqual(before, git(self.work, 'rev-parse', 'HEAD'))
        self.assertEqual('customDependencyAndPackaging()\n', (self.work / 'build.gradle').read_text())
        self.assertIn('build_preparation', result.outputs['artifact_refs'])

    def test_v26_custom_build_is_a_diagnostic_for_downstream_planning(self):
        (self.work / 'build.gradle').write_text('customDependencyAndPackaging()\n')
        git(self.work, 'add', 'build.gradle')
        git(self.work, 'commit', '-m', 'custom build')
        before = git(self.work, 'rev-parse', 'HEAD')
        command = replace(self.command, options={'workflow_version': 26})
        result = BuildPrepareHandler()(command)
        self.assertEqual('completed', result.status)
        self.assertIsNone(result.error_code)
        self.assertEqual('manual_merge_required', result.outputs['preparation_status'])
        self.assertEqual('build_merge_unsupported', result.outputs['diagnostic_code'])
        self.assertEqual('unavailable', result.outputs['product_state'])
        self.assertEqual('unverified', result.outputs['acceptance_status'])
        self.assertEqual(before, result.outputs['candidate_commit'])
        self.assertEqual(before, git(self.work, 'rev-parse', 'HEAD'))
        self.assertIn('build_preparation', result.outputs['artifact_refs'])

    def test_v26_missing_locked_mdk_input_remains_a_failure(self):
        from modport.development import _artifact
        checksums = dict(self.fixture.manifest.checksums)
        del checksums['mdk:gradle/wrapper/gradle-wrapper.jar']
        incomplete = replace(self.fixture.manifest, checksums=checksums)
        command = replace(self.command, options={'workflow_version': 26})
        ref = _artifact(command, 'incomplete-manifest.json',
                        json.dumps(incomplete.to_dict()).encode())
        command = replace(command, artifact_refs={'locked_manifest': ref})
        result = BuildPrepareHandler()(command)
        self.assertEqual('failed', result.status)
        self.assertEqual('locked_mdk_unavailable', result.outputs['preparation_status'])
        self.assertEqual('unverified', result.outputs['acceptance_status'])

    def test_commit_failure_reverts_authenticated_additions(self):
        from modport.development import _git
        before = git(self.work, 'rev-parse', 'HEAD')
        def fail_commit(command, workspace, *args, **kwargs):
            if args[0] == 'commit':
                raise ValueError('injected commit failure')
            return _git(command, workspace, *args, **kwargs)
        with patch('modport.development._git', side_effect=fail_commit):
            result = BuildPrepareHandler()(self.command)
        self.assertEqual('restored', result.outputs['recovery']['status'])
        self.assertEqual(before, git(self.work, 'rev-parse', 'HEAD'))
        self.assertEqual('', git(self.work, 'status', '--porcelain'))
        self.assertFalse((self.work / 'build.gradle').exists())


class DeterministicWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / 'run'
        self.request = MigrationRequest('example', 'https://example.invalid/source.git',
            '1.20.1', '26.1.2', source_revision='a' * 40,
            budget=Budget(max_agent_assignments=100))

    def test_current_route_has_expected_stage_order(self):
        new = compile_migration_workflow(self.request).to_dict()
        self.assertEqual(WORKFLOW_VERSION, new['workflow_version'])
        self.assertEqual('implementation', new['next_stage']['migration_plan'])
        main, early, _, dependencies = stage_routes(new)
        self.assertLess(main.index('codemod'), main.index('early_compile'))
        self.assertLess(main.index('early_compile'), main.index('migration_plan'))
        self.assertNotIn('skill_publish', early)
        self.assertNotIn('mod_analysis', early)
        self.assertEqual(('environment',), dependencies['skill_resolve'])
        self.assertFalse(next(row['agent'] for row in new['stages']
                              if row['stage_id'] == 'migration_inventory'))

    def test_exact_versions_scan_and_transform_before_agent_preparation(self):
        request = replace(self.request, source_loader_version='47.0.1', source_java='17',
                          target_loader_version='26.1.2.106', target_java='25')
        new = compile_migration_workflow(request).to_dict()
        _, _, _, dependencies = stage_routes(new)
        self.assertEqual(('source',), dependencies['skill_resolve'])
        self.assertEqual(('skill_resolve',), dependencies['mod_scan'])
        self.assertEqual(('mod_scan',), dependencies['codemod'])
        self.assertEqual(('codemod',), dependencies['background'])
        self.assertEqual(('environment', 'codemod'), dependencies['build_prepare'])
        self.assertEqual(('build_prepare', 'codemod'), dependencies['early_compile'])

    def test_real_sdk_flows_through_failed_compile_without_automatic_rework(self):
        operations = MigrationOperations(handlers=registry(fail_stage='early_compile', fail_count=1),
            isolation_mode='thread',
            memory_probe=lambda: MemorySnapshot(64 * 1024**3, 64 * 1024**3, 'fixture'))
        run = operations.submit(self.request, run_dir=self.root, run_id='deterministic')
        state = drive_run(operations, run)
        self.assertEqual('succeeded', state['state'])
        self.assertEqual('unverified', state['application_state']['acceptance_status'])
        self.assertEqual(1, len(state['tasks']['migration_plan']['attempts']))
        for absent in ('migration_tasks', 'parallel_review', 'skill_publish', 'mod_analysis'):
            self.assertNotIn(absent, state['tasks'])
        self.assertFalse(any(key.startswith('agent-rework') for key in state['tasks']))
        compile_result = state['application_state']['effective']['early_compile']
        self.assertEqual('failed', compile_result['status'])
        planner = state['tasks']['migration_plan']['attempts'][0]['command']['payload']
        self.assertIn('early_compile', planner['upstream_results'])
        self.assertIn('migration_inventory', planner['upstream_results'])

    def test_completed_provisional_compile_still_enters_business_diagnostics(self):
        operations = MigrationOperations()
        app = {'effective': {}, 'processed': [], 'history': []}
        command = OperationInput('run', 'early_compile', 'early_compile', 'compile',
                                 str(self.root), options={'workflow_version': 26})
        outcome = _result(command, 'completed', outputs={
            'diagnostic_code': 'provisional_source_compile_failed',
            'probe_returncode': 1, 'project_build_verified': False,
            'acceptance_status': 'unverified'})
        operations._flowthrough_record(app, command, outcome)
        self.assertEqual('provisional_source_compile_failed',
                         app['business_diagnostics'][0]['diagnostic_code'])
        self.assertEqual('completed', app['effective']['early_compile']['status'])


class DeterministicStageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.work = self.root / 'worktree'
        self.work.mkdir()
        git(self.work, 'init')
        (self.work / 'Example.java').write_text('import net.minecraftforge.eventbus.api.SubscribeEvent;\n')
        git(self.work, 'add', '.')
        git(self.work, 'commit', '-m', 'source')

    def command(self, stage):
        from modport.development import _artifact
        command = OperationInput('test', stage, stage, stage, str(self.root),
                                 options={'workflow_version': 19})
        candidate = git(self.work, 'rev-parse', 'HEAD').strip()
        records = {'mod_scan_report': {'candidate_identity': {'kind': 'git_commit', 'value': candidate}},
                   'codemod': {'after_commit': candidate}}
        if stage == 'migration_inventory':
            records = {}
        refs = {name: _artifact(command, name + '-input.json', json.dumps(record).encode())
                for name, record in records.items()}
        return replace(command, artifact_refs=refs)

    def target_command(self):
        import test_build_preparation
        from modport.development import _artifact
        fixture = test_build_preparation.BuildPreparationTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.install_exact_mdk()
        git(fixture.worktree, 'add', '.')
        git(fixture.worktree, 'commit', '-m', 'exact locked target build')
        self.root, self.work = fixture.root, fixture.worktree
        command = self.command('early_compile')
        manifest_ref = _artifact(command, 'locked-manifest.json', json.dumps(fixture.manifest.to_dict()).encode())
        command = replace(command, artifact_refs={**command.artifact_refs, 'locked_manifest': manifest_ref})
        return command

    def test_early_compile_runs_only_compile_and_records_candidate(self):
        command = self.target_command()
        with patch('modport.handlers.GradleHandler') as gradle:
            gradle.return_value.return_value = _result(command, 'failed',
                outputs={'process_executed': True}, error_code='compile_failed')
            result = EarlyCompileHandler()(command)
        self.assertEqual('compile_failed', result.error_code, result.detail)
        gradle.assert_called_once_with(baseline=False,
            tasks=('-Pneo_version=21.1.77', 'compileJava'), name='early-compile')
        self.assertEqual('failed', result.status)
        self.assertEqual(40, len(result.outputs['candidate_commit']))
        record = json.loads((self.root / result.outputs['artifact_refs']['early_compile']['path']).read_text())
        self.assertTrue(record['candidate_unchanged'])
        self.assertFalse(record['acceptance_evidence'])

    def test_source_build_is_not_misreported_as_target_compilation(self):
        command = self.command('early_compile')
        with patch('modport.handlers.GradleHandler') as gradle:
            result = EarlyCompileHandler()(command)
        gradle.assert_not_called()
        self.assertEqual('target_build_configuration_missing', result.error_code)
        self.assertFalse(result.outputs['process_executed'])
        record = json.loads((self.root / result.outputs['artifact_refs']['early_compile']['path']).read_text())
        self.assertEqual('failed', record['status'])

    def test_v20_compile_authenticates_post_codemod_build_chain(self):
        from modport.development import _artifact
        command = self.target_command()
        current = git(self.work, 'rev-parse', 'HEAD').strip()
        previous = 'b' * 40
        codemod = _artifact(command, 'prior-codemod.json', json.dumps({'after_commit': previous}).encode())
        preparation = _artifact(command, 'post-codemod-build.json', json.dumps({
            'before_commit': previous, 'after_commit': current}).encode())
        command = replace(command, options={**command.options, 'workflow_version': 20},
                          artifact_refs={**command.artifact_refs, 'codemod': codemod,
                                         'build_preparation_result': preparation})
        with patch('modport.handlers.GradleHandler') as gradle:
            gradle.return_value.return_value = _result(command, 'completed', outputs={'process_executed': True})
            result = EarlyCompileHandler()(command)
        self.assertEqual('completed', result.status, result.detail)
        bad = _artifact(command, 'unrelated-build.json', json.dumps({
            'before_commit': 'c' * 40, 'after_commit': current}).encode())
        with patch('modport.handlers.GradleHandler') as gradle:
            result = EarlyCompileHandler()(replace(command,
                artifact_refs={**command.artifact_refs, 'build_preparation_result': bad}))
        gradle.assert_not_called()
        self.assertEqual('candidate_identity_mismatch', result.error_code)

    def test_v20_inventory_exposes_missing_dependency_index_coverage(self):
        command = self.command('migration_inventory')
        result = DeterministicInventoryHandler()(replace(command, options={'workflow_version': 20}))
        self.assertEqual('completed', result.status, result.detail)
        ref = result.outputs['artifact_refs']['dependency_symbols']
        record = json.loads((self.root / ref['path']).read_text())
        self.assertFalse(record['effective_classpath_verified'])
        self.assertTrue(record['diagnostics'])

    def test_inventory_keeps_locations_without_model_or_version_rules(self):
        result = DeterministicInventoryHandler()(self.command('migration_inventory'))
        self.assertEqual('completed', result.status)
        record = json.loads((self.root / result.outputs['artifact_refs']['migration_inventory']['path']).read_text())
        self.assertTrue(record['issues'])
        self.assertFalse(record['source_scan_complete'])
        self.assertIn('mod_scan_report', result.outputs['diagnostics'])
        self.assertIn('target compilation not executed', result.outputs['diagnostics'])

    def test_inventory_rejects_a_different_compile_candidate(self):
        command = replace(self.command('migration_inventory'), upstream_results={
            'early_compile': {'outputs': {'candidate_commit': 'b' * 40}}})
        result = DeterministicInventoryHandler()(command)
        self.assertEqual('candidate_identity_mismatch', result.error_code)

    def test_v26_inventory_keeps_semantic_merge_and_provisional_compile_diagnostics(self):
        from modport.development import _artifact
        command = replace(self.command('migration_inventory'), options={'workflow_version': 26})
        candidate = git(self.work, 'rev-parse', 'HEAD')
        log = _artifact(command, 'provisional-compile.log',
            b'/workspace/src/main/java/Example.java:1: error: package custom.library does not exist\n')
        record = _artifact(command, 'early-compile.json', json.dumps({
            'candidate_commit': candidate, 'process_executed': True,
            'compile_scope': 'provisional_mdk_source_compile',
            'diagnostic_code': 'provisional_source_compile_failed',
            'artifact_refs': {'gradle_log:provisional_source_compile': log},
        }).encode())
        command = replace(command, artifact_refs={'early_compile': record},
            upstream_results={
                'build_prepare': {'outputs': {'preparation_status': 'manual_merge_required'}},
                'early_compile': {'outputs': {'candidate_commit': candidate}},
            })
        result = DeterministicInventoryHandler()(command)
        self.assertEqual('completed', result.status, result.detail)
        inventory = json.loads((self.root / result.outputs['artifact_refs']['migration_inventory']['path']).read_text())
        self.assertIn('build preparation requires semantic merge; original project build is unverified',
                      inventory['diagnostics'])
        self.assertIn('provisional source compile diagnostic: provisional_source_compile_failed',
                      inventory['diagnostics'])
        self.assertIn('only provisional MDK source compilation executed; project build remains unverified',
                      result.outputs['diagnostics'])
        self.assertIn('gradle_log:provisional_source_compile', inventory['diagnostic_refs'])

    def test_inventory_rejects_uncommitted_content(self):
        (self.work / 'Example.java').write_text('changed without a commit\n')
        result = DeterministicInventoryHandler()(self.command('migration_inventory'))
        self.assertEqual('candidate_identity_mismatch', result.error_code)

    def test_compile_capture_failures_still_check_candidate_integrity(self):
        from modport.development import _artifact
        from modport.evidence import file_digest
        for failure in ('large_log', 'freeze_error', 'handler_error'):
            with self.subTest(failure=failure):
                command = self.target_command()
                log = self.root / 'compile.log'
                with log.open('wb') as stream:
                    stream.truncate(32 * 1024 * 1024 + 1 if failure == 'large_log' else 10)
                ref = {'path': 'compile.log', 'sha256': file_digest(log)}
                def compile_and_modify(_command):
                    (self.work / 'Example.java').write_text('dirty compile side effect\n')
                    if failure == 'handler_error':
                        raise OSError('injected compile settlement failure')
                    return _result(command, 'completed', outputs={
                        'process_executed': True, 'artifact_refs': {'gradle_log:early_compile': ref}})
                def freeze(cmd, name, data, metadata=None):
                    if failure == 'freeze_error' and name.startswith('compile-evidence-'):
                        raise OSError('injected log freeze failure')
                    return _artifact(cmd, name, data, metadata)
                with patch('modport.handlers.GradleHandler') as gradle, \
                        patch('modport.development._artifact', side_effect=freeze):
                    gradle.return_value.side_effect = compile_and_modify
                    result = EarlyCompileHandler()(command)
                self.assertEqual('candidate_identity_mismatch', result.error_code)
                record = json.loads((self.root / result.outputs['artifact_refs']['early_compile']['path']).read_text())
                self.assertFalse(record['candidate_unchanged'])

    def test_codemod_stage_applies_and_commits_only_exact_supported_import(self):
        request = {'source_minecraft': '1.20.1', 'source_loader': 'forge',
            'source_loader_version': '47.0.1', 'target_minecraft': '26.1.2',
            'target_loader': 'neoforge', 'target_loader_version': '26.1.2.106',
            'source_java': '17', 'target_java': '25'}
        command = replace(self.command('codemod'), payload={'request': request})
        result = CodemodHandler()(command)
        self.assertEqual('completed', result.status, result.detail)
        self.assertEqual('produced', result.outputs['product_state'])
        self.assertIn('import net.neoforged.bus.api.SubscribeEvent;',
                      (self.work / 'Example.java').read_text())
        self.assertEqual('', git(self.work, 'status', '--porcelain'))
        from modport.development import _artifact
        repeated_command = replace(command, command_id='codemod-second')
        scan_ref = _artifact(repeated_command, 'fresh-scan.json', json.dumps({
            'candidate_identity': {'kind': 'git_commit', 'value': result.outputs['candidate_commit']}}).encode())
        repeated = CodemodHandler()(replace(repeated_command, artifact_refs={'mod_scan_report': scan_ref}))
        self.assertEqual('completed', repeated.status, repeated.detail)
        self.assertEqual('empty_valid', repeated.outputs['product_state'])
        self.assertEqual(result.outputs['candidate_commit'], repeated.outputs['candidate_commit'])

    def test_unsupported_version_is_not_a_valid_zero_change_result(self):
        request = {'source_minecraft': '1.20.1', 'source_loader': 'forge',
            'source_loader_version': '47.0.2', 'target_minecraft': '26.1.2',
            'target_loader': 'neoforge', 'target_loader_version': '26.1.2.106',
            'source_java': '17', 'target_java': '25'}
        command = replace(self.command('codemod'), payload={'request': request})
        before = git(self.work, 'rev-parse', 'HEAD')
        result = CodemodHandler()(command)
        self.assertEqual('failed', result.status)
        self.assertEqual('codemod_rules_unavailable', result.error_code)
        self.assertEqual('unavailable', result.outputs['product_state'])
        self.assertEqual(before, git(self.work, 'rev-parse', 'HEAD'))
        self.assertEqual('', git(self.work, 'status', '--porcelain'))

    def test_scan_candidate_change_is_rejected_before_writing(self):
        command = self.command('codemod')
        (self.work / 'Other.java').write_text('class Other {}\n')
        git(self.work, 'add', 'Other.java')
        git(self.work, 'commit', '-m', 'different clean candidate')
        result = CodemodHandler()(command)
        self.assertEqual('codemod_input_invalid', result.error_code)
        self.assertIn('net.minecraftforge', (self.work / 'Example.java').read_text())

    def test_commit_failure_restores_only_own_changes_and_retains_patch(self):
        from modport.development import _git
        request = {'source_minecraft': '1.20.1', 'source_loader': 'forge',
            'source_loader_version': '47.0.1', 'target_minecraft': '26.1.2',
            'target_loader': 'neoforge', 'target_loader_version': '26.1.2.106',
            'source_java': '17', 'target_java': '25'}
        command = replace(self.command('codemod'), payload={'request': request})
        original = (self.work / 'Example.java').read_bytes()
        before = git(self.work, 'rev-parse', 'HEAD')

        def fail_commit(cmd, workspace, *args, **kwargs):
            if args[0] == 'commit':
                raise ValueError('injected commit failure')
            return _git(cmd, workspace, *args, **kwargs)

        with patch('modport.development._git', side_effect=fail_commit):
            result = CodemodHandler()(command)
        self.assertEqual('failed', result.status)
        self.assertEqual('restored', result.outputs['recovery']['status'])
        self.assertEqual(original, (self.work / 'Example.java').read_bytes())
        self.assertEqual(before, git(self.work, 'rev-parse', 'HEAD'))
        self.assertEqual('', git(self.work, 'status', '--porcelain'))
        self.assertIn('codemod_patch', result.outputs['artifact_refs'])

    def test_compile_does_not_accept_clean_commit_after_codemod(self):
        command = self.command('early_compile')
        (self.work / 'Other.java').write_text('class Other {}\n')
        git(self.work, 'add', 'Other.java')
        git(self.work, 'commit', '-m', 'different clean candidate')
        with patch('modport.handlers.GradleHandler') as gradle:
            result = EarlyCompileHandler()(command)
        gradle.assert_not_called()
        self.assertEqual('candidate_identity_mismatch', result.error_code)
