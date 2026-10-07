"""Deterministic handlers for integration tests: never call an external model/build."""
from dataclasses import dataclass
from pathlib import Path
import re
from modport.contracts import OperationResult
from modport.evidence import atomic_json, file_digest, digest
from modport.workflow import STAGE_IDS, REVIEW_STAGES


@dataclass
class FixtureHandler:
    fail_stage: str = ''
    fail_count: int = 0
    reject_stage: str = ''
    raise_stage: str = ''
    resolve_research_gaps: bool = False
    __execution_kernel_revision__ = 'modport-test-fixture-v2'

    def __call__(self, operation):
        root = Path(operation.run_dir)
        generation = re.fullmatch(re.escape(operation.stage_id) + r'\.g(\d+)(?:\..+)?', operation.task_id)
        fixture_attempt = operation.attempt + (int(generation[1]) - 1 if generation else 0)
        if operation.stage_id == self.raise_stage:
            (root / 'partial.txt').write_text('partial external action')
            raise RuntimeError('uncertain external modification')
        outputs = {}
        if operation.options.get('workflow_version', 0) >= 15:
            drafts = {'migration_inventory', 'contract_diagnose', 'target_diagnose'}
            refiners = {'migration_plan', 'contract_repair_plan', 'target_repair_plan'}
            if operation.stage_id in drafts | refiners:
                revision = (1 if operation.stage_id in drafts
                            else operation.payload.get('plan_refinement_round', 0) + 1)
                parent = operation.artifact_refs.get('current_plan')
                path = root / 'artifacts' / 'executions' / operation.command_id / 'planning-plan.md'
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text('# Fixture plan\n\nAuthenticated fixed planning fixture.\n')
                ref = {'path': path.relative_to(root).as_posix(), 'sha256': file_digest(path),
                       'media_type': 'text/markdown', 'metadata': {
                           'revision': revision,
                           'parent_sha256': parent.get('sha256') if isinstance(parent, dict) else None}}
                ready = revision == 3 or (operation.options.get('gate_policy') == 'downstream_toolcall'
                                          and revision == 2)
                outputs.update(plan_status='ready' if ready else 'continue',
                               plan_revision=revision,
                               artifact_refs={operation.stage_id: ref, 'current_plan': ref})
        if operation.stage_id == 'contract_freeze':
            from modport.characterization import BehaviorEntry, CharacterizationContract
            path = root / 'artifacts' / 'functional-contract.lock.json'
            atomic_json(path, {'contract': CharacterizationContract(
                entries=(BehaviorEntry('value', 'source'),)).to_dict()})
            outputs['artifact_refs'] = {'functional_contract_lock': {
                'path': path.relative_to(root).as_posix(), 'sha256': file_digest(path)}}
        if 'regression_scope' in operation.payload:
            outputs['regression_scope'] = operation.payload['regression_scope']
            outputs['workspace'] = operation.options['workspace']
        if operation.stage_id == 'goal_prepare':
            path = root / 'artifacts' / (operation.task_id + '.json')
            atomic_json(path, {'task_id': operation.payload['development_task']['id']})
            outputs['artifact_refs'] = {'coder_goal': {'path': path.relative_to(root).as_posix(),
                                                       'sha256': file_digest(path)}}
        if operation.stage_id == 'parallel_review':
            outputs['parallel_decision'] = 'sequential'
        if operation.stage_id == 'source':
            path = root / 'artifacts/source.json'
            atomic_json(path, {'source_commit': 'a' * 40})
            outputs['artifact_refs'] = {'source_evidence': {'path': 'artifacts/source.json', 'sha256': file_digest(path)}}
        log = root / 'logs' / f'{operation.command_id}.log'
        log.parent.mkdir(exist_ok=True)
        log.write_text(f'{operation.command_id}\n', encoding='utf-8')
        outputs['log'] = log.relative_to(root).as_posix()
        if operation.stage_id in REVIEW_STAGES:
            rejected = operation.stage_id == self.reject_stage and fixture_attempt <= self.fail_count
            outputs.update(verdict='rejected' if rejected else 'approved',
                           prior_findings=[{'detail': 'repair this exact failure', 'execution_id': operation.command_id}] if rejected else [])
        if operation.stage_id == 'research_review' and self.resolve_research_gaps and outputs.get('verdict') == 'approved':
            outputs['approved_gap_resolutions'] = [
                {'gap_id': row.get('gap_id') or f"{row['skill']}:{row['index']}", 'project_status': 'resolved'}
                for row in operation.payload.get('project_research_gaps', [])]
        failed = operation.stage_id == self.fail_stage and fixture_attempt <= self.fail_count
        return OperationResult('failed' if failed else 'completed', run_id=operation.run_id,
            task_id=operation.task_id, stage_id=operation.stage_id, command_id=operation.command_id,
            outputs=outputs, error_code='fixture_failure' if failed else None, detail='fixture result')


def registry(**kwargs):
    return {f'modport.{stage}': FixtureHandler(**kwargs) for stage in STAGE_IDS}


class Clock:
    def __init__(self, now=100.0):
        self.now = now

    def __call__(self):
        return self.now


class SlowFixture:
    __execution_kernel_revision__ = 'modport-slow-fixture-v1'

    def __call__(self, operation):
        import subprocess
        import sys
        from modport.evidence import current_lock_fds
        child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'],
                                 pass_fds=current_lock_fds())
        (Path(operation.run_dir) / 'child-pid').write_text(str(child.pid))
        child.wait()
        raise AssertionError('timeout must stop the process tree')
