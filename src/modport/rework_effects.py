"""Effects specific to reviewer-requested author continuations."""
from dataclasses import dataclass
from pathlib import Path
import shutil

from .contracts import OperationInput


class CoderReworkHandler:
    def __call__(self, command):
        from .rework_coder import run_coder_rework
        original = OperationInput.from_dict(command.payload['rework_original_command'])
        return run_coder_rework(command, original,
            Path(command.run_dir) / command.payload['reviewer_workspace'],
            command.payload['reviewer_rework']['instructions'])


@dataclass
class AuthorReworkHandler:
    handler: object

    def __call__(self, command):
        result = self.handler(command)
        if (command.stage_id != 'test_design' or not command.payload.get('reviewer_rework')
                or result.status != 'completed'):
            return result
        # Refresh the suite inside the paused reviewer's existing read-only
        # workspace. Its Codex session and pristine product source stay in place.
        from .independent_tests import _contained, _safe_copy, _SUITE
        root = Path(command.run_dir)
        reviewer = _contained(root, command.payload['reviewer_workspace'])
        source = _contained(root, result.outputs['workspace']) / _SUITE
        current = _contained(reviewer, _SUITE)
        staging = _contained(reviewer, _SUITE + '.rework-' + command.command_id)
        _safe_copy(source, staging)
        backup = root / 'artifacts/executions' / command.command_id / 'previous-review-suite'
        try:
            if current.exists():
                backup.parent.mkdir(parents=True, exist_ok=True)
                current.rename(backup)
            staging.rename(current)
        except OSError:
            if backup.exists() and not current.exists():
                backup.rename(current)
            raise
        finally:
            if staging.exists():
                shutil.rmtree(staging)
        return result
