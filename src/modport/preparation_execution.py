"""Execute shared preparation through independently checked coder goals."""
from .workspace import project_path
from dataclasses import replace
from hashlib import sha256
import json
from pathlib import Path
import subprocess
import time

from . import handlers
from .development import (_artifact, _clean, _git, _head, _owned, _paths,
                          _plan, _verified, DevelopmentIntegrateHandler, validate_plan)


def _approved(command):
    from .planning import STAGES, _context, _upstream, _raw_report, validate_review
    command = replace(command, stage_id='development_prepare')
    workspace = project_path(Path(command.run_dir), 'worktree')
    if workspace.resolve() != workspace.absolute():
        raise ValueError('preparation workspace must not traverse symlinks')
    expected, _, _ = _context(command, workspace)
    documents = [_upstream(command, stage, current=expected) for stage in STAGES]
    if len({doc['producer_execution_id'] for doc in documents}) != len(STAGES):
        raise ValueError('preparation needs four independent planning rounds')
    task_doc, review = documents[2:]
    before = _head(command, workspace)
    if (review.get('parallel_decision') != 'prepare_first'
            or review.get('base_commit') != before or task_doc.get('base_commit') != before):
        raise ValueError('preparation needs current prepare_first approval')
    validate_review(replace(command, stage_id='parallel_review'), review, task_doc)
    if _raw_report(task_doc):
        return expected, review['preparation_plan']
    tasks = [task for task in task_doc['tasks'] if task.get('kind') == 'prepare']
    ids = {task['id'] for task in tasks}
    if not tasks or any(not set(task['dependencies']) <= ids for task in tasks):
        raise ValueError('preparation cannot depend on unfinished coders')
    groups = [{**task, 'source_task_ids': [task['id']],
               'source_objectives': {task['id']: task['objective']},
               'source_acceptance': {task['id']: task['acceptance']}}
              for task in tasks]
    plan = validate_plan({'schema_version': 1, 'base_commit': before,
                          'shared_paths': [], 'tasks': groups}, allow_preparation=True, workflow_version=command.options.get("workflow_version", 15),
                          model_policy=command.options.get('model_policy'))
    return expected, plan


class PreparationPrepareHandler:
    def __call__(self, command):
        try:
            expected, plan = _approved(command)
            _clean(command, project_path(Path(command.run_dir), 'worktree'))
            ref = _artifact(command, 'preparation-development-plan.json',
                            (json.dumps(plan, sort_keys=True) + '\n').encode(),
                            {'development_base': plan['base_commit'],
                             'planning_generation': expected['planning_generation']})
            return handlers._result(command, 'completed', outputs={
                'development_tasks': plan['tasks'], 'development_base': plan['base_commit'],
                'development_kind': 'preparation', 'goal_scope': 'migration',
                'artifact_refs': {'development_plan': ref}})
        except (OSError, ValueError, TypeError, KeyError, subprocess.TimeoutExpired) as exc:
            return handlers._result(command, 'blocked', detail=str(exc),
                                    error_code='development_prepare_invalid')


class PreparationIntegrateHandler:
    def __call__(self, command):
        applied = False
        before = None
        workspace = project_path(Path(command.run_dir), 'worktree')
        try:
            if (command.payload.get('development_kind') != 'preparation'
                    or command.payload.get('goal_scope') != 'migration'):
                raise ValueError('preparation integration scope mismatch')
            expected, approved = _approved(command)
            plan = _plan(command)
            if plan != approved:
                raise ValueError('preparation plan differs from four-round approval')
            before = plan['base_commit']
            _clean(command, workspace)
            results = command.payload.get('development_results')
            if not isinstance(results, list) or len(results) != len(plan['tasks']):
                raise ValueError('preparation requires every native coder result')
            for result in results:
                if not isinstance(result, dict) or not isinstance(result.get('outputs'), dict):
                    raise ValueError('preparation requires valid native coder results')
                outputs = result.get('outputs', {})
                native = outputs.get('native_goal')
                if not isinstance(native, dict) or native.get('host_accepted') is not True:
                    raise ValueError('preparation coder lacks host acceptance')
                ref = outputs.get('artifact_refs', {}).get('goal_acceptance_report')
                if not isinstance(ref, dict):
                    raise ValueError('preparation acceptance report is missing')
                _verified(command, ref)
            applied = True
            integrated = DevelopmentIntegrateHandler()(replace(command,
                options={**command.options, 'workspace': 'worktree'}))
            if integrated.status != 'completed':
                raise ValueError(integrated.detail or 'preparation integration failed')
            after = _head(command, workspace)
            paths = _paths(command, workspace, before, after)
            owner = {'id': 'shared-preparation', 'kind': 'prepare',
                     'owned_paths': [path for task in plan['tasks'] for path in task['owned_paths']]}
            _owned(paths, owner)
            for task in plan['tasks']:
                if not any(path == prefix or path.startswith(prefix + '/')
                           for path in paths for prefix in task['owned_paths']):
                    raise ValueError('preparation task has no implemented changes: ' + task['id'])
            root = Path(command.run_dir)
            patch = root / 'artifacts' / 'executions' / command.command_id / 'prepare.patch'
            if patch.exists() or patch.is_symlink():
                raise ValueError('preparation patch already exists')
            patch.parent.mkdir(parents=True, exist_ok=True)
            _git(command, workspace, 'diff', '--binary', '--full-index', '--no-ext-diff',
                 '--no-textconv', '--no-renames', f'--output={patch}', before, after, '--')
            patch_ref = {'path': patch.relative_to(root).as_posix(),
                         'sha256': sha256(patch.read_bytes()).hexdigest()}
            record = {**expected, 'before_commit': before, 'after_commit': after,
                      'completed_task_ids': [task['id'] for task in plan['tasks']],
                      'changed_paths': paths, 'patch_ref': patch_ref,
                      'patch_sha256': patch_ref['sha256'],
                      'checks': ['four independent planning rounds', 'native coder host acceptance',
                                 'per-task ownership', 'authenticated integrated patches'],
                      'coder_results': results,
                      'development_integration': integrated.outputs['artifact_refs']['development_integration']}
            ref = _artifact(command, 'development-prepare.json',
                            (json.dumps(record, sort_keys=True) + '\n').encode())
            return handlers._result(command, 'completed', outputs={
                'head': after, 'artifact_refs': {'development_prepare': ref}})
        except (OSError, ValueError, TypeError, KeyError, subprocess.TimeoutExpired) as exc:
            if applied:
                rollback = replace(command, options={**command.options, 'deadline_epoch': time.time() + 30})
                try:
                    _git(rollback, workspace, 'reset', '--hard', before)
                except (OSError, ValueError, subprocess.TimeoutExpired) as error:
                    return handlers._result(command, 'blocked', detail=f'{exc}; rollback failed: {error}',
                                            error_code='development_prepare_rollback_failed')
            return handlers._result(command, 'blocked', detail=str(exc),
                                    error_code='development_prepare_invalid')
