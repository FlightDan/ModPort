"""ModPort-owned business inputs and outcomes; these are not Kernel DTOs."""

from dataclasses import asdict, dataclass, field
import json
from pathlib import Path
import re
from typing import Any, Mapping


FORMAT_VERSION = 2


def json_copy(value: Any) -> Any:
    return json.loads(json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False))


@dataclass(frozen=True)
class OperationInput:
    run_id: str
    task_id: str
    stage_id: str
    command_id: str
    run_dir: str
    attempt: int = 1
    payload: Mapping[str, Any] = field(default_factory=dict)
    options: Mapping[str, Any] = field(default_factory=dict)
    upstream_results: Mapping[str, Any] = field(default_factory=dict)
    artifact_refs: Mapping[str, Any] = field(default_factory=dict)
    prior_findings: tuple[Mapping[str, Any], ...] = ()
    schema_version: int = FORMAT_VERSION

    def __post_init__(self):
        if self.schema_version != FORMAT_VERSION:
            raise ValueError("unsupported ModPort operation format; legacy Runs are retired")
        for name in ("run_id", "task_id", "stage_id", "command_id"):
            if not isinstance(getattr(self, name), str) or not re.fullmatch(r"[A-Za-z0-9_.:-]+", getattr(self, name)):
                raise ValueError(f"invalid {name}")
        if not Path(self.run_dir).is_absolute():
            raise ValueError("run_dir must be absolute")
        if type(self.attempt) is not int or self.attempt < 1:
            raise ValueError("attempt must be positive")
        for name in ("payload", "options", "upstream_results", "artifact_refs"):
            value = json_copy(dict(getattr(self, name)))
            object.__setattr__(self, name, value)
        findings = json_copy(list(self.prior_findings))
        if any(not isinstance(item, dict) for item in findings):
            raise ValueError("findings must be objects")
        object.__setattr__(self, "prior_findings", tuple(findings))

    def to_dict(self) -> dict:
        return json_copy(asdict(self))

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]):
        return cls(**dict(value))


@dataclass(frozen=True)
class OperationResult:
    status: str
    run_id: str = ""
    task_id: str = ""
    stage_id: str = ""
    command_id: str = ""
    outputs: Mapping[str, Any] = field(default_factory=dict)
    detail: str = ""
    error_code: str | None = None

    def __post_init__(self):
        if self.status not in {"completed", "failed", "blocked"}:
            raise ValueError("invalid business outcome")
        object.__setattr__(self, "outputs", json_copy(dict(self.outputs)))

    def to_dict(self) -> dict:
        return json_copy(asdict(self))

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]):
        return cls(**dict(value))

    def validate_for(self, command: OperationInput):
        for key in ("run_id", "task_id", "stage_id", "command_id"):
            if getattr(self, key) != getattr(command, key):
                raise ValueError(f"business result {key} mismatch")
