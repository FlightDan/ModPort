"""Safe, serializable evidence for two-turn agent assignments."""
from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
import subprocess
from typing import Any, Sequence


@dataclass(frozen=True)
class DialogueMetadata:
    turns: int
    thread_id: str | None
    planning_log: str
    execution_log: str
    plan_path: str
    planning_prompt_sha256: str
    execution_prompt_sha256: str
    schema_path: str | None = None
    diagnostics: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            'schema_version': 2,
            'transport': 'opencode',
            'turns': self.turns,
            'thread_id': self.thread_id,
            'planning_log': self.planning_log,
            'execution_log': self.execution_log,
            'plan_path': self.plan_path,
            'prompt_refs': {
                'plan': {'sha256': self.planning_prompt_sha256, 'turn': 1},
                'execute': {'sha256': self.execution_prompt_sha256, 'turn': 2},
            },
            'schema_path': self.schema_path,
            'diagnostics': list(self.diagnostics),
        }


class AgentDialogueError(RuntimeError):
    """A technical failure that prevents the required second turn."""

    def __init__(self, code: str, detail: str, metadata: DialogueMetadata,
                 *, completed: subprocess.CompletedProcess[str] | None = None):
        super().__init__(detail)
        self.code = code
        self.metadata = metadata.to_dict()
        self.completed = completed


def _safe_output_path(raw: Path | str, cwd: Path, label: str) -> Path:
    path = Path(raw)
    if not path.is_absolute():
        path = cwd / path
    path = path.absolute()
    current = Path(path.anchor)
    for part in path.parent.parts[1:]:
        current = current / part
        if current.exists() or current.is_symlink():
            if current.is_symlink() or not current.is_dir() or current.resolve() != current.absolute():
                raise ValueError(f'unsafe {label} parent')
        else:
            current.mkdir()
    if (path.is_symlink() or path.exists() and not path.is_file()
            or path.resolve() != path.absolute()):
        raise ValueError(f'unsafe {label} path')
    return path


def _planning_log(log: Path) -> Path:
    return log.with_name(log.stem + '.plan.log')


def _metadata(*, turns: int, thread_id: str | None, planning_log: Path,
              execution_log: Path, plan_path: Path, planning_prompt: str,
              execution_prompt: str, schema_path: str | None,
              diagnostics: Sequence[str] = ()) -> DialogueMetadata:
    return DialogueMetadata(
        turns=turns,
        thread_id=thread_id,
        planning_log=str(planning_log),
        execution_log=str(execution_log),
        plan_path=str(plan_path),
        planning_prompt_sha256=sha256(planning_prompt.encode('utf-8')).hexdigest(),
        execution_prompt_sha256=sha256(execution_prompt.encode('utf-8')).hexdigest(),
        schema_path=schema_path,
        diagnostics=tuple(diagnostics),
    )
