"""Current workflow local-source -> isolated coder -> selected workspace evidence."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
import unittest
from unittest.mock import patch

from modport.contracts import OperationInput
from modport.desktop_local_source import prepare_local_workspace
from modport.development import CoderHandler, DevelopmentIntegrateHandler, _artifact, validate_plan
from modport.handlers import ValidateInputHandler, _exec, _result
from modport.workspace import project_path, project_relative, workspace_context
from modport.workflow import WORKFLOW_VERSION


class WorkspaceExecutionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.original = self.directory / 'original'
        self.original.mkdir()
        self.root = self.directory / 'run'
        (self.root / 'artifacts').mkdir(parents=True)
        (self.original / 'a.txt').write_text('original\n')
        self.git('init', '-q', cwd=self.original)
        self.git('add', '.', cwd=self.original)
        self.git('commit', '-qm', 'original', cwd=self.original)

    def git(self, *args, cwd=None):
        result = subprocess.run(['git', '-c', 'user.name=Test', '-c', 'user.email=test@example.invalid',
                                 '-c', 'core.hooksPath=' + os.devnull, *args],
                                cwd=cwd, check=True, capture_output=True, text=True)
        return result.stdout.strip()

    def setup_mode(self, mode):
        self.mode = mode
        self.before = {path.relative_to(self.original / '.git').as_posix(): path.read_bytes()
                       for path in (self.original / '.git').rglob('*') if path.is_file()}
        prepared = prepare_local_workspace(str(self.original), application_root=self.directory / 'app',
            snapshot_id='desktop-' + 'a' * 32, mode=mode,
            branch='migration/target' if mode == 'git_worktree' else None)
        request = {key: prepared[key] for key in ('source_repository', 'source_revision', 'source_snapshot')}
        request['local_workspace'] = prepared['workspace']
        (self.root / 'run.json').write_text(json.dumps({'request': request,
            'definition': {'workflow_version': WORKFLOW_VERSION}}))
        (self.root / 'artifacts' / 'rubric.json').write_text(json.dumps({'rubric_id': 'workspace', 'rubric_version': 1}))
        command = OperationInput('workspace', 'source', 'source', 'source', str(self.root), payload=request,
            options={'workflow_version': WORKFLOW_VERSION}, artifact_refs={'acceptance_rubric': {'path': 'artifacts/rubric.json'}})
        result = ValidateInputHandler()(command)
        self.assertEqual(result.status, 'completed', result.detail)
        self.assertFalse((self.root / 'worktree').exists())
        self.work = project_path(self.root)
        self.assertEqual(self.work, Path(prepared['workspace']['path']))
        self.assertEqual(project_relative(self.root, self.work / 'a.txt').as_posix(), 'worktree/a.txt')
        self.base = prepared['source_revision']
        plan = validate_plan({'schema_version': 1, 'base_commit': self.base, 'shared_paths': [],
            'tasks': [{'id': 'a', 'objective': 'Update file', 'owned_paths': ['a.txt'],
                       'dependencies': [], 'acceptance': ['Change content']}]}, workflow_version=WORKFLOW_VERSION)
        reference = _artifact(command, 'development-plan.json', json.dumps(plan).encode(), {'development_base': self.base})
        self.refs = {'development_plan': reference}
        self.task = plan['tasks'][0]

    def command(self, stage, results=()):
        payload = {'development_base': self.base, 'development_generation': 1,
                   'dependency_patches': [], 'development_results': list(results)}
        options = {'workflow_version': WORKFLOW_VERSION}
        if stage == 'coder':
            payload['development_task'] = self.task
            options['workspace'] = 'workspaces/development/g1/a'
        return OperationInput('workspace', stage, stage, stage, str(self.root),
                              payload=payload, options=options, artifact_refs=self.refs)

    def execute_mode(self, mode):
        self.setup_mode(mode)
        (self.work / '.ENV').write_text('PRIVATE_LOCAL_VALUE=not-source\n')
        def author(_handler, command):
            work = self.root / command.options['workspace']
            self.assertEqual((work / 'a.txt').read_text(), 'original\n')
            (work / 'a.txt').write_text('migrated\n')
            return _result(command, 'completed')
        with workspace_context(self.root), patch('modport.handlers.CodexStageHandler.__call__', author):
            coded = CoderHandler()(self.command('coder'))
            self.assertEqual(coded.status, 'completed', coded.detail)
            integrated = DevelopmentIntegrateHandler()(self.command('development_integrate', [coded.to_dict()]))
        self.assertEqual(integrated.status, 'completed', integrated.detail)
        self.assertEqual((self.work / 'a.txt').read_text(), 'migrated\n')
        self.assertEqual((self.root / 'baseline' / 'a.txt').read_text(), 'original\n')
        self.assertEqual(project_path(self.root), self.work)  # recovery reuses frozen binding
        if mode in {'copy', 'direct'}:
            after = {path.relative_to(self.original / '.git').as_posix(): path.read_bytes()
                     for path in (self.original / '.git').rglob('*') if path.is_file()}
            self.assertEqual(after, self.before)
        if mode != 'direct':
            self.assertEqual((self.original / 'a.txt').read_text(), 'original\n')
        else:
            with self.assertRaisesRegex(ValueError, 'preserves partial edits'):
                _exec(['git', 'reset', '--hard', self.base], cwd=self.work,
                      log=self.root / 'logs' / 'forbidden-reset.log')
        with workspace_context(self.root):
            from modport.workspace import git_probe
            status = git_probe(['git', 'status', '--porcelain'], cwd=self.work, capture_output=True, text=True)
            tracked = git_probe(['git', 'ls-files'], cwd=self.work, capture_output=True, text=True)
        self.assertEqual(status.returncode, 0, status.stderr)
        self.assertEqual(status.stdout, '')
        self.assertNotIn('.ENV', tracked.stdout.splitlines())

    def test_direct_source_coder_and_integration_preserve_original_git(self):
        self.execute_mode('direct')

    def test_logical_paths_reject_windows_drive_components_and_streams(self):
        self.setup_mode('direct')
        for value in ('worktree/D:secret.txt', 'worktree/D:/secret.txt',
                      'worktree/../secret', 'worktree/dir\\file'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                project_path(self.root, value)
        if os.name == 'nt':
            with self.assertRaises(ValueError):
                project_path(self.root, 'worktree/a.txt:secret')
        else:
            log = self.root / 'logs' / 'test:source:1.log'
            log.parent.mkdir(exist_ok=True)
            log.write_text('completed\n')
            from modport.evidence import verified_path
            self.assertEqual(verified_path(self.root, {'path': 'logs/test:source:1.log'}), log)

    def test_shared_planning_review_and_package_paths_use_frozen_workspace(self):
        from modport.ungated_planning import _workspace
        from modport.skill_runtime import _scan_workspace
        from modport.rework_tools import prepare_session
        from modport.handlers import _target_package_receipt
        from modport.evidence import verified_path
        self.setup_mode('direct')
        command = OperationInput('workspace', 'review', 'code_review', 'review', str(self.root),
            options={'workflow_version': WORKFLOW_VERSION, 'deadline_epoch': time.time() + 60},
            payload={'review_rework_targets': [{'target_agent': 'coder-a'}]})
        self.assertEqual(_workspace(command), self.work)
        self.assertEqual(_scan_workspace(command)[0], self.work)
        session_path = prepare_session(command, self.work, 30)
        self.assertEqual(json.loads(session_path.read_text())['workspace'], 'worktree')
        output = self.work / 'build/libs/output.jar'
        output.parent.mkdir(parents=True)
        output.write_bytes(b'package-path-fixture')
        receipt, _ = _target_package_receipt(command)
        self.assertEqual(receipt['artifacts'][0]['path'], 'worktree/build/libs/output.jar')
        self.assertEqual(verified_path(self.root, receipt['artifacts'][0]), output)
        from modport.repair_evidence import snapshot_repair_evidence
        captured = snapshot_repair_evidence(self.root, {'package': receipt['artifacts'][0]})
        self.assertTrue(captured['package']['path'].startswith('artifacts/repair-evidence/'))
        self.assertEqual(verified_path(self.root, captured['package']).read_bytes(), output.read_bytes())

    def repair_commit(self, mode):
        from modport.repair_execution import _commit_target
        self.setup_mode(mode)
        (self.work / 'a.txt').write_text('reviewed repair\n')
        staging = self.root / 'repair-staging'
        staging.mkdir()
        transaction = _commit_target(self.command('target_repair_integrate'), self.work,
                                     ['a.txt'], staging, self.base)
        self.assertNotEqual(transaction['new_head'], self.base)
        from modport.workspace import git_probe
        with workspace_context(self.root):
            committed = git_probe(['git', 'show', 'HEAD:a.txt'], cwd=self.work,
                                  capture_output=True, text=True)
        self.assertEqual(committed.stdout, 'reviewed repair\n')
        if mode == 'direct':
            after = {path.relative_to(self.original / '.git').as_posix(): path.read_bytes()
                     for path in (self.original / '.git').rglob('*') if path.is_file()}
            self.assertEqual(after, self.before)
            from modport.rework_coder import _rollback_integration
            self.assertFalse(_rollback_integration(self.command('agent_rework'), self.work, self.base))
            self.assertEqual((self.work / 'a.txt').read_text(), 'reviewed repair\n')

    def test_direct_reviewed_repair_uses_private_index_and_preserves_partial_edits(self):
        self.repair_commit('direct')

    def test_git_reviewed_repair_updates_only_registered_worktree_index(self):
        self.repair_commit('git_worktree')

    def test_copy_source_coder_and_integration_leave_original_unchanged(self):
        self.execute_mode('copy')

    def test_git_source_coder_and_integration_update_named_branch(self):
        self.execute_mode('git_worktree')
        self.assertEqual(self.git('branch', '--show-current', cwd=self.work), 'migration/target')
        self.assertEqual(self.git('rev-parse', 'migration/target', cwd=self.original),
                         self.git('rev-parse', 'HEAD', cwd=self.work))

    def test_formal_sdk_executes_coder_integration_and_reuses_receipt(self):
        from modport.kernel_runtime import open_runtime, reconcile_receipt
        from modport.payload_storage import unpack_result
        from dispatcher_sdk.execution_kernel import RetryPolicy
        self.setup_mode('direct')
        class Handler:
            __execution_kernel_revision__ = 'local-workspace-integration-current-v37'
            def __init__(self, delegate):
                self.delegate = delegate
            def __call__(self, command):
                return self.delegate(command)
        def author(_handler, command):
            (self.root / command.options['workspace'] / 'a.txt').write_text('formal SDK edit\n')
            return _result(command, 'completed')
        handlers = {'modport.coder': Handler(CoderHandler()),
                    'modport.development_integrate': Handler(DevelopmentIntegrateHandler())}
        with open_runtime(self.root, handlers=handlers, isolation_mode='thread') as runtime:
            def execute(operation):
                command = runtime.command('modport.' + operation.stage_id,
                    execution_id=operation.command_id, idempotency_key=operation.command_id,
                    correlation_id=operation.run_id, timeout_seconds=60,
                    retry_policy=RetryPolicy(max_attempts=1), payload=operation.to_dict())
                runtime.submit(command)
                outcome = runtime.run_once()
                self.assertEqual(outcome.state, 'succeeded', outcome)
                effect = runtime.kernel.get_effect('modport:' + operation.command_id)
                result = unpack_result(self.root, effect.response)
                self.assertEqual(result['status'], 'completed', result)
                return command, effect, result
            with patch('modport.handlers.CodexStageHandler.__call__', author):
                _, _, coded = execute(self.command('coder'))
            command, effect, integrated = execute(self.command('development_integrate', [coded]))
            recovered = unpack_result(self.root, reconcile_receipt(self.root, command.to_dict(), effect))
            self.assertEqual(recovered, integrated)
        self.assertEqual((self.original / 'a.txt').read_text(), 'formal SDK edit\n')

    @unittest.skipUnless(os.name == 'posix', 'Linux bubblewrap witness')
    def test_registered_sandbox_command_filters_credentials_and_syncs(self):
        from modport.opencode_shell_mcp import prepare_sandbox_tool, _run
        self.setup_mode('direct')
        (self.original / '.env').write_text('SECRET_SENTINEL=hidden\n')
        prepare_sandbox_tool(self.root, self.work, 'local-build', 30)
        session = json.loads((self.root / 'artifacts/executions/local-build/opencode-shell/session.json').read_text())
        result = _run(session,
            'test ! -e .env && test ! -e .git && git status --porcelain && '
            'printf "sandbox result\\n" > a.txt && mkdir empty-result', 20)
        if 'Operation not permitted' in result.get('stderr', ''):
            self.skipTest('bubblewrap namespace unavailable in restricted sandbox')
        self.assertEqual(result['exit_code'], 0, result)
        self.assertEqual((self.original / 'a.txt').read_text(), 'sandbox result\n')
        self.assertTrue((self.original / 'empty-result').is_dir())
        self.assertEqual((self.original / '.env').read_text(), 'SECRET_SENTINEL=hidden\n')
        stages = self.root / 'toolchains/local-build-stages'
        self.assertFalse(list(stages.glob('*/workspace')))
        self.assertFalse(list(stages.glob('*/baseline')))


if __name__ == '__main__':
    unittest.main()
