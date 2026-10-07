"""Real artifact/session boundaries for diagnostic repair feedback."""
from dataclasses import replace
import json
import io
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from modport.contracts import OperationInput, OperationResult
from modport.evidence import file_digest
from modport.handlers import (AcceptancePreflightHandler, BaselineContractVerificationHandler,
                              CodexStageHandler,
                              BuildAndBehaviorHandler, FreezeContractHandler)
from modport.prompt_compressor import CompressedPrompt
from modport.repair_context import load_inventory, observe_candidate
from modport.repair_routing import route_inventory
from modport.rework_mcp import Session, ReworkServer
from modport.rework_orchestration import project_rework_responses
from modport.rework_tools import prepare_session, tool_prompt
import test_rework_flowthrough as flowthrough


class RepairIntegrationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.workspace = self.root / 'worktree'
        self.workspace.mkdir()
        self.source = self.workspace / 'src/A.java'
        self.source.parent.mkdir()
        self.source.write_text('import net.minecraftforge.common.MinecraftForge;\n')
        self.git('init', '-q')
        self.git('add', '.')
        self.git('-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid',
                 'commit', '-qm', 'fixture')
        self.command = OperationInput('run', 'build', 'target_build', 'verify-exec',
                                     str(self.root), options={'workflow_version': 18})

    def git(self, *args):
        return subprocess.run(['git', '-C', str(self.workspace), *args], check=True,
                              capture_output=True, text=True).stdout.strip()

    def result(self, status='completed', **outputs):
        return OperationResult(status, 'run', 'build', 'target_build', 'verify-exec', outputs=outputs)

    def build_handler(self, *, mutate=False):
        root, source, make_result = self.root, self.source, self.result

        class Build:
            baseline = False

            def __call__(self, command):
                log = root / 'logs/build.log'
                log.parent.mkdir(exist_ok=True)
                log.write_text('/workspace/src/A.java:1: error: package net.minecraftforge.common does not exist\n')
                if mutate:
                    source.write_text('changed during verification\n')
                return make_result('failed', artifact_refs={'gradle_log:target_build': {
                    'path': 'logs/build.log', 'sha256': file_digest(log), 'media_type': 'text/plain'}})

        return BuildAndBehaviorHandler(Build(), lambda command: replace(
            make_result('failed'), error_code='locked_artifact_invalid', detail='contract lock missing'))

    def test_host_failure_has_locations_candidate_and_durable_log(self):
        result = self.build_handler()(self.command)
        self.assertEqual('failed', result.status)
        self.assertEqual(self.git('rev-parse', 'HEAD'), result.outputs['verification_candidate_id'])
        self.assertEqual('failed', result.outputs['build_status'])
        self.assertEqual('failed', result.outputs['verification_status'])
        inventory = load_inventory(self.root, result.outputs['repair_inventory_ref'])
        kinds = {issue['kind'] for issue in inventory['issues']}
        self.assertTrue({'source_reference', 'compilation_error', 'missing_input'} <= kinds)
        ref = result.outputs['artifact_refs']['gradle_log:target_build']
        (self.root / 'logs/build.log').write_text('next build replaced the log')
        self.assertIn('error:', (self.root / ref['path']).read_text())
        self.assertEqual(ref['sha256'], file_digest(self.root / ref['path']))

    def test_mutation_during_verification_never_binds_old_commit(self):
        result = self.build_handler(mutate=True)(self.command)
        self.assertIsNone(result.outputs['verification_candidate_id'])
        self.assertEqual('unknown_or_changed_candidate', result.outputs['verification_binding'])

    def test_dirty_workspace_never_uses_head_as_verified_candidate(self):
        self.source.write_text('uncommitted source')
        self.assertIsNone(observe_candidate(self.workspace))

    def test_inherited_harness_candidate_binds_untracked_source_bytes(self):
        harness = self.workspace / '.modport'
        harness.mkdir()
        contract = harness / 'functional-contract.json'
        contract.write_text('{"candidate":"first"}\n')
        generated = self.workspace / 'build' / 'result.txt'
        generated.parent.mkdir()
        generated.write_text('first build output\n')
        gradle = self.workspace / '.gradle' / 'state.bin'
        gradle.parent.mkdir()
        gradle.write_bytes(b'first cache')

        self.assertIsNone(observe_candidate(self.workspace))
        first = observe_candidate(self.workspace, inherited_harness=True)
        self.assertIsNotNone(first)
        self.assertEqual(64, len(first))
        generated.write_text('second build output\n')
        gradle.write_bytes(b'second cache')
        self.assertEqual(first, observe_candidate(self.workspace, inherited_harness=True))
        contract.write_text('{"candidate":"second"}\n')
        second = observe_candidate(self.workspace, inherited_harness=True)
        self.assertNotEqual(first, second)
        evidence = harness / 'evidence/run.json'
        evidence.parent.mkdir()
        evidence.write_text('{"generated":true}\n')
        self.assertEqual(second, observe_candidate(self.workspace, inherited_harness=True))
        extra = harness / 'src/ContractTest.java'
        extra.parent.mkdir()
        extra.write_text('class ContractTest {}\n')
        self.assertNotEqual(second, observe_candidate(self.workspace, inherited_harness=True))
        extra.unlink()
        extra.symlink_to(self.source)
        self.assertIsNone(observe_candidate(self.workspace, inherited_harness=True))
        extra.unlink()
        (self.workspace / 'unrelated.txt').write_text('untracked source')
        self.assertIsNone(observe_candidate(self.workspace, inherited_harness=True))

    def test_inherited_harness_candidate_tracks_staged_sources_and_renames(self):
        harness = self.workspace / '.modport'
        harness.mkdir()
        (harness / 'functional-contract.json').write_text('{}\n')
        source = harness / 'src/ContractTest.java'
        source.parent.mkdir()
        source.write_text('class ContractTest {}\n')
        first = observe_candidate(self.workspace, inherited_harness=True)
        self.assertIsNotNone(first)

        self.git('add', '.modport/src/ContractTest.java')
        self.assertEqual(first, observe_candidate(self.workspace, inherited_harness=True))
        self.git('-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid',
                 'commit', '-qm', 'add harness source')
        self.git('mv', '.modport/src/ContractTest.java', '.modport/src/Renamed.java')
        renamed = observe_candidate(self.workspace, inherited_harness=True)
        self.assertIsNotNone(renamed)
        self.assertNotEqual(first, renamed)

        self.git('mv', '.modport/src/Renamed.java', 'escaped.java')
        self.assertIsNone(observe_candidate(self.workspace, inherited_harness=True))
        self.git('mv', 'escaped.java', '.modport/src/Renamed.java')
        (harness / 'src/Renamed.java').unlink()
        self.git('add', '-u', '.modport/src')
        self.assertIsNotNone(observe_candidate(self.workspace, inherited_harness=True))

    def test_inherited_harness_candidate_rejects_hidden_project_changes(self):
        harness = self.workspace / '.modport'
        harness.mkdir()
        (harness / 'functional-contract.json').write_text('{}\n')
        self.assertIsNotNone(observe_candidate(self.workspace, inherited_harness=True))
        for set_flag, clear_flag in (('--assume-unchanged', '--no-assume-unchanged'),
                                     ('--skip-worktree', '--no-skip-worktree')):
            with self.subTest(set_flag=set_flag):
                self.git('update-index', set_flag, 'src/A.java')
                self.source.write_text('hidden changed project source\n')
                self.assertIsNone(observe_candidate(self.workspace, inherited_harness=True))
                self.git('update-index', clear_flag, 'src/A.java')
                self.git('checkout', '--', 'src/A.java')
        exclude = self.workspace / '.git/info/exclude'
        exclude.write_text('hidden.properties\n')
        (self.workspace / 'hidden.properties').write_text('build input outside Git\n')
        self.assertIsNone(observe_candidate(self.workspace, inherited_harness=True))

    def test_inherited_verifier_binds_only_unchanged_harness_candidate(self):
        baseline = self.root / 'baseline'
        baseline.mkdir()
        source = baseline / 'src/A.java'
        source.parent.mkdir()
        source.write_text('class A {}\n')
        def git(*args):
            return subprocess.run(['git', '-C', str(baseline), *args], check=True,
                                  capture_output=True, text=True).stdout.strip()
        git('init', '-q')
        git('add', 'src/A.java')
        git('-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid',
            'commit', '-qm', 'source')
        contract = baseline / '.modport/functional-contract.json'
        contract.parent.mkdir()
        contract.write_text('{"candidate":"first"}\n')
        generated = baseline / 'build/result.txt'
        generated.parent.mkdir()
        generated.write_text('generated output\n')
        command = OperationInput('run', 'contract_verify', 'contract_verify', 'harness-verify-1',
                                 str(self.root), options={'workflow_version': 25},
                                 artifact_refs={'inherited_harness': {'path': 'artifacts/harness.json',
                                                                     'sha256': '0' * 64}})
        handler = BaselineContractVerificationHandler()
        def completed(current):
            return OperationResult('completed', current.run_id, current.task_id,
                                   current.stage_id, current.command_id)
        with patch.object(handler, '_execute', side_effect=completed):
            first = handler(command)
        expected = observe_candidate(baseline, inherited_harness=True)
        self.assertEqual(expected, first.outputs['verification_candidate_id'])
        self.assertEqual('host_observed_harness_candidate', first.outputs['verification_binding'])

        changed = replace(command, command_id='harness-verify-2')
        def mutate(current):
            contract.write_text('{"candidate":"changed"}\n')
            return completed(current)
        with patch.object(handler, '_execute', side_effect=mutate):
            second = handler(changed)
        self.assertIsNone(second.outputs['verification_candidate_id'])
        self.assertEqual('unknown_or_changed_candidate', second.outputs['verification_binding'])

    def test_inherited_contract_author_reports_matching_content_candidate(self):
        import test_handlers
        baseline = self.root / 'baseline'
        baseline.mkdir()
        source = baseline / 'src/A.java'
        source.parent.mkdir()
        source.write_text('class A {}\n')
        def git(*args):
            return subprocess.run(['git', '-C', str(baseline), *args], check=True,
                                  capture_output=True, text=True).stdout.strip()
        git('init', '-q')
        git('add', 'src/A.java')
        git('-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid',
            'commit', '-qm', 'source')
        head = git('rev-parse', 'HEAD')
        source_record = self.root / 'artifacts/source.json'
        source_record.parent.mkdir()
        source_record.write_text(json.dumps({'source_commit': head}))
        original = test_handlers.HandlerTests._command(self.root, 'contract_draft',
            payload={'reviewer_rework': {'request_id': 'rework-1'}})
        command = replace(original, options={**original.options, 'workflow_version': 25},
            artifact_refs={**original.artifact_refs,
                'inherited_harness': {'path': 'artifacts/inherited.json', 'sha256': '0' * 64},
                'source_evidence': {'path': 'artifacts/source.json',
                                    'sha256': file_digest(source_record)}})
        contract = baseline / '.modport/functional-contract.json'
        contract.parent.mkdir()
        contract.write_text('{"candidate":"inherited"}\n')
        generated = baseline / 'build/result.txt'
        generated.parent.mkdir()
        generated.write_text('generated output\n')

        class Compressor:
            def compress(self, prompt, **kwargs):
                return CompressedPrompt(prompt, {'compressed': False})

        def agent(**kwargs):
            contract.write_text('{"candidate":"repaired"}\n')
            return subprocess.CompletedProcess(['opencode'], 0,
                json.dumps({'type': 'item.completed', 'item': {
                    'type': 'agent_message', 'text': 'repaired'}}) + '\n')

        with (patch('modport.handlers.PromptCompressor.from_environment', return_value=Compressor()),
              patch('modport.rework_tools.prepare_session', return_value=None),
              patch('modport.rework_tools.opencode_tool_config', return_value={}),
              patch('modport.opencode_agent.run_agent', side_effect=agent)):
            result = CodexStageHandler('Repair contract', baseline=True,
                                       required_paths=('.modport/functional-contract.json',))(command)
        self.assertEqual('completed', result.status, result.detail)
        self.assertEqual(head, result.outputs['after_head'])
        self.assertEqual(observe_candidate(baseline, inherited_harness=True),
                         result.outputs['after_candidate_id'])

    def test_preflight_keeps_original_failure_and_adds_specific_locations(self):
        result = AcceptancePreflightHandler()(replace(self.command, stage_id='acceptance_preflight'))
        self.assertEqual('forbidden_dependency', result.error_code)
        inventory = load_inventory(self.root, result.outputs['repair_inventory_ref'])
        finding = next(row for row in inventory['issues'] if row['rule_id'] == 'forge-package-reference')
        self.assertEqual('src/A.java', finding['locations'][0]['path'])
        self.assertEqual(1, finding['locations'][0]['line'])

    def test_session_routes_shared_files_and_missing_contract_to_real_authors(self):
        targets = [
            {'target_agent': 'coder-a', 'description': 'API', 'stage': 'coder', 'owned_paths': ['src/']},
            {'target_agent': 'coder-b', 'description': 'caller', 'stage': 'coder', 'owned_paths': ['src/A.java']},
            {'target_agent': 'contract', 'description': 'baseline', 'stage': 'contract_draft', 'owned_paths': []}]
        command = replace(self.command, stage_id='code_review', task_id='review', command_id='review-exec',
                          payload={'review_rework_targets': targets})
        session = Session.load(prepare_session(command, self.workspace, 60))
        inventory = load_inventory(self.root, session.repair_context['inventory_ref'])
        routes = route_inventory(inventory, targets)['routes']
        shared = next(row for row in routes if row['coordination_required'])
        self.assertEqual(['coder-a', 'coder-b'], shared['target_agents'])
        missing = next(row for row in routes if row['reason'] == 'prerequisite_producer')
        self.assertEqual(['contract'], missing['target_agents'])
        self.assertTrue(session.targets[0].to_dict()['recommended_issue_ids'])
        self.assertNotIn('recommended_issue_ids', targets[0])
        # A transport restart reuses the frozen context and does not rescan.
        with patch('modport.repair_context.publish_inventory', side_effect=AssertionError('rescanned')):
            restarted = Session.load(prepare_session(command, self.workspace, 60))
        self.assertEqual(session.repair_context, restarted.repair_context)
        self.assertIn('Broad scope is a reason to split', tool_prompt(command))

    def test_v17_session_and_results_keep_legacy_shape(self):
        command = replace(self.command, options={'workflow_version': 17}, stage_id='code_review',
            payload={'review_rework_targets': [{'target_agent': 'coder-a', 'description': 'legacy', 'stage': 'coder'}]})
        session = Session.load(prepare_session(command, self.workspace, 60))
        self.assertEqual({}, session.repair_context)
        self.assertEqual({'target_agent': 'coder-a', 'description': 'legacy'}, session.targets[0].to_dict())
        self.assertNotIn('repair_inventory_ref', self.build_handler()(command).outputs)

    def test_missing_contract_never_publishes_a_synthetic_lock(self):
        command = replace(self.command, stage_id='contract_freeze')
        result = FreezeContractHandler()(command)
        self.assertEqual('failed', result.status)
        self.assertEqual('contract_missing', result.error_code)
        self.assertNotIn('functional_contract_lock', result.outputs['artifact_refs'])
        self.assertIn('functional_contract_observation', result.outputs['artifact_refs'])

    def test_freeze_preserves_fresh_verifier_failure_instead_of_old_report(self):
        contract = self.root / 'baseline/.modport/functional-contract.json'
        contract.parent.mkdir(parents=True)
        contract.write_text(json.dumps({'entries': [{'behavior': 'real source description'}]}))
        evidence = self.root / 'artifacts/baseline-contract-tests.json'
        evidence.parent.mkdir(parents=True)
        evidence.write_text(json.dumps({'exit_code': 0, 'command_id': 'old-success'}))
        failure = self.result('failed').to_dict()
        failure['command_id'] = 'fresh-failure'
        command = replace(self.command, stage_id='contract_freeze',
                          upstream_results={'contract_verify': failure})
        result = FreezeContractHandler()(command)
        ref = result.outputs['artifact_refs']['functional_contract_lock']
        lock = json.loads((self.root / ref['path']).read_text())
        self.assertEqual('fresh-failure', lock['baseline_verification']['command_id'])
        self.assertEqual('failed', lock['baseline_verification']['status'])

    def test_v26_failed_review_does_not_copy_old_approval_into_observation(self):
        contract = self.root / 'baseline/.modport/functional-contract.json'
        contract.parent.mkdir(parents=True)
        contract.write_text(json.dumps({'entries': [{'behavior': 'source description'}]}))
        old_review = self.root / 'baseline/.modport/contract-review.json'
        old_review.write_text(json.dumps({'verdict': 'approved', 'review_id': 'old-review'}))
        failed_review = OperationResult('failed', 'run', 'review', 'contract_review',
            'fresh-review', outputs={'artifact_refs': {'contract_review': {
                'path': 'baseline/.modport/contract-review.json', 'sha256': '0' * 64}}}).to_dict()
        command = replace(self.command, stage_id='contract_freeze',
            options={'workflow_version': 26, 'business_gates_disabled': True},
            upstream_results={'contract_review': failed_review})
        result = FreezeContractHandler()(command)
        ref = result.outputs['artifact_refs']['functional_contract_lock']
        observation = json.loads((self.root / ref['path']).read_text())
        self.assertEqual({}, observation['review'])
        self.assertNotIn('contract_review', result.outputs['artifact_refs'])
        self.assertIn('fresh review decision digest does not match file',
                      ' '.join(observation['business_diagnostics']))

    def test_next_target_list_uses_returned_inventory_without_rescanning(self):
        targets = [{'target_agent': 'coder-a', 'stage': 'coder', 'description': 'API',
                    'owned_paths': ['src/'], 'goal_scope': 'migration'}]
        reviewer = replace(self.command, stage_id='code_review', command_id='review-exec',
                           payload={'review_rework_targets': targets})
        session = Session.load(prepare_session(reviewer, self.workspace, 60))
        result = self.build_handler()(self.command)
        record = {'request_id': 'request-1', 'reviewer_execution_id': 'review-exec',
                  'sequence': 1, 'state': 'failed', 'updates': [], 'repair_diagnostics': True,
                  'after_inventory_ref': result.outputs['repair_inventory_ref']}
        app = {'review_rework': {'requests': {'request-1': record}}}
        project_rework_responses(self.root, app)
        with patch('modport.repair_context.load_inventory', side_effect=AssertionError('reprojected')):
            project_rework_responses(self.root, app)
        stream = io.StringIO()
        server = ReworkServer(session, output_stream=stream)
        server._dispatch({'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
                          'params': {'name': 'list_rework_targets', 'arguments': {}}})
        result = json.loads(stream.getvalue())['result']['structuredContent']
        self.assertIn('/verify-exec/', result['repair_context']['inventory_ref']['path'])
        self.assertTrue(result['targets'][0]['recommended_issue_ids'])
        self.assertTrue((self.root / result['repair_context']['routes_path']).is_file())
        self.assertEqual(1, len(list(session.responses_dir.glob('*.json'))))


class CrossStageReworkTests(unittest.TestCase):
    setUp = flowthrough.ReworkFlowthroughTests.setUp
    def test_v18_gap_reviewer_coder_rework_receives_fresh_build(self):
        self.header['definition']['workflow_version'] = 18
        self.record.update(reviewer_stage='gap_review', target_scope='migration')
        result = OperationResult('completed', 'run', 'child', 'agent_rework', 'child-exec',
                                outputs={'after_head': 'b' * 40})
        self.attempt.update(state='succeeded', result={'value': result.to_dict()})
        self.host._review_rework_decision(self.snapshot, self.header, self.app)
        self.assertEqual('target_build', self.host.scheduled)

    def test_v18_contract_scope_coder_uses_contract_verifier(self):
        self.header['definition']['workflow_version'] = 18
        self.record.update(reviewer_stage='gap_review', target_scope='contract')
        result = OperationResult('completed', 'run', 'child', 'agent_rework', 'child-exec')
        self.attempt.update(state='succeeded', result={'value': result.to_dict()})
        self.host._review_rework_decision(self.snapshot, self.header, self.app)
        self.assertEqual('contract_verify', self.host.scheduled)

    def test_failed_verification_is_returned_to_original_waiting_reviewer(self):
        self.header['definition']['workflow_version'] = 18
        self.record.update(reviewer_stage='gap_review', target_scope='migration', sequence=1,
                           followup_stage='target_build')
        author = OperationResult('completed', 'run', 'author', 'agent_rework', 'author-exec',
                                 outputs={'after_head': 'b' * 40})
        self.record['updates'] = [{'stage': 'coder', 'result': author.to_dict()}]
        verify = OperationResult('failed', 'run', 'child', 'agent_rework', 'child-exec',
            outputs={'verification_candidate_id': 'b' * 40, 'build_status': 'failed',
                     'build_executed': True, 'verification_executed': False,
                     'verification_status': 'failed', 'verification_detail': 'contract missing'},
            detail='fresh host build failed', error_code='gradle_failed')
        self.attempt.update(state='succeeded', result={'value': verify.to_dict()})
        self.host._review_rework_decision(self.snapshot, self.header, self.app)
        feedback = self.record['repair_feedback']
        self.assertTrue(feedback['verification_current'])
        self.assertEqual('child-exec', feedback['verification_execution_id'])
        self.assertEqual('closed-review', feedback['reviewer_execution_id'])
        self.assertEqual('failed', feedback['verification']['build_status'])
        self.assertIn('fresh host build failed', self.record['text'])
        self.assertEqual('failed', self.record['state'])

    def test_contract_verification_refreshes_lock_without_losing_verifier_identity(self):
        self.header['definition']['workflow_version'] = 18
        self.record.update(reviewer_stage='gap_review', target_scope='contract',
                           followup_stage='contract_verify')
        verify = OperationResult('failed', 'run', 'child', 'agent_rework', 'child-exec',
                                error_code='contract_missing')
        self.attempt.update(state='succeeded', result={'value': verify.to_dict()})
        self.host._review_rework_decision(self.snapshot, self.header, self.app)
        self.assertEqual('contract_freeze', self.host.scheduled)
        self.assertEqual('child-exec', self.record['verification_execution_id'])
