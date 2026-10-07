"""Deterministic, evidence-bound source transformations.

The kernel is deliberately independent from workflow orchestration.  A caller
must first create a :class:`CodemodPlan`, inspect its unified patch, and then
explicitly apply that exact plan.  Rules default to ``detect-only``; possessing
evidence never implicitly grants write capability.
"""

from __future__ import annotations

from .workspace import git_probe

from dataclasses import dataclass, field
import difflib
import fnmatch
from hashlib import sha256
import json
import os
from pathlib import Path, PurePosixPath
import stat
import subprocess
import tempfile
from typing import Any, Iterable, Literal, Mapping, Sequence


RuleMode = Literal["detect-only", "transform"]
RuleKind = Literal["java_import", "path_rename", "json_field"]

_EXCLUDED_DIRECTORIES = frozenset(
    {
        ".git",
        ".gradle",
        ".hg",
        ".idea",
        ".svn",
        ".venv",
        "__pycache__",
        "build",
        "node_modules",
        "out",
        "target",
        "venv",
    }
)
_HEX_DIGITS = frozenset("0123456789abcdef")


class CodemodError(ValueError):
    """Base class for safe planning and application failures."""


class CodemodSecurityError(CodemodError):
    """An unsafe path or symbolic link was encountered."""


class CodemodConflictError(CodemodError):
    """Two inputs or transformations cannot be reconciled safely."""


class StaleCodemodPlanError(CodemodError):
    """The candidate changed after its patch was planned."""


def _non_empty(name: str, value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise CodemodError(f"{name} must be a non-empty string")
    return value.strip()


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _digest_json(value: Any) -> str:
    return sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _digest_bytes(value: bytes) -> str:
    return sha256(value).hexdigest()


def _safe_relative(value: str) -> str:
    value = _non_empty("relative path", value)
    if "\\" in value or value.startswith("/"):
        raise CodemodSecurityError(f"path must be a portable relative path: {value!r}")
    raw_parts = value.split("/")
    if any(part in {"", ".", ".."} for part in raw_parts):
        raise CodemodSecurityError(f"path must not contain empty, dot, or parent parts: {value!r}")
    path = PurePosixPath(value)
    if path.is_absolute() or not path.parts:
        raise CodemodSecurityError(f"path must be relative: {value!r}")
    return path.as_posix()


@dataclass(frozen=True, slots=True)
class VersionIdentity:
    """One exact platform identity; ranges and aliases are intentionally invalid."""

    minecraft: str
    loader: str
    loader_version: str

    def __post_init__(self) -> None:
        for name in ("minecraft", "loader", "loader_version"):
            value = _non_empty(name, getattr(self, name))
            if any(char.isspace() for char in value):
                raise CodemodError(f"{name} must be an exact whitespace-free value")
            if any(char in value for char in "[](),+*"):
                raise CodemodError(f"{name} must not contain version range syntax")
            if value.lower() in {"latest", "recommended", "current", "*"}:
                raise CodemodError(f"{name} must be exact, not {value!r}")

    def to_dict(self) -> dict[str, str]:
        return {
            "minecraft": self.minecraft,
            "loader": self.loader,
            "loader_version": self.loader_version,
        }


@dataclass(frozen=True, slots=True)
class RuleEvidence:
    """A precise statement of what one source proves (and no more)."""

    source: str
    locator: str
    supports: str
    sha256: str | None = None

    def __post_init__(self) -> None:
        for name in ("source", "locator", "supports"):
            _non_empty(name, getattr(self, name))
        if not (self.source.startswith(("https://", "http://", "/"))):
            raise CodemodError("evidence source must be HTTP(S) or an absolute local path")
        if self.sha256 is not None:
            checksum = self.sha256.lower()
            if len(checksum) != 64 or any(char not in _HEX_DIGITS for char in checksum):
                raise CodemodError("evidence sha256 must contain 64 hexadecimal characters")

    def to_dict(self) -> dict[str, str]:
        result = {
            "source": self.source,
            "locator": self.locator,
            "supports": self.supports,
        }
        if self.sha256 is not None:
            result["sha256"] = self.sha256.lower()
        return result


@dataclass(frozen=True, slots=True)
class CodemodRule:
    """A small, explicit transformation contract.

    ``source_value`` and ``target_value`` are exact import names, relative
    paths, or JSON field names depending on ``kind``.  JSON ``object_path``
    contains exact dictionary keys/list indexes and never wildcards.
    """

    rule_id: str
    kind: RuleKind
    source_identity: VersionIdentity
    target_identity: VersionIdentity
    source_value: str
    target_value: str
    evidence: tuple[RuleEvidence, ...] = ()
    mode: RuleMode = "detect-only"
    files: tuple[str, ...] = ()
    object_path: tuple[str | int, ...] = ()
    limitations: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _non_empty("rule_id", self.rule_id)
        if self.kind not in {"java_import", "path_rename", "json_field"}:
            raise CodemodError(f"unsupported rule kind: {self.kind!r}")
        if self.mode not in {"detect-only", "transform"}:
            raise CodemodError(f"unsupported rule mode: {self.mode!r}")
        _non_empty("source_value", self.source_value)
        _non_empty("target_value", self.target_value)
        if self.source_value == self.target_value:
            raise CodemodError("source_value and target_value must differ")
        if self.mode == "transform" and not self.evidence:
            raise CodemodError("transform rules require explicit evidence")
        if self.kind == "path_rename":
            _safe_relative(self.source_value)
            _safe_relative(self.target_value)
            if self.files or self.object_path:
                raise CodemodError("path_rename does not accept files or object_path")
        elif self.kind == "java_import":
            if not _valid_java_name(self.source_value) or not _valid_java_name(self.target_value):
                raise CodemodError("java_import values must be exact qualified Java names")
            if self.object_path:
                raise CodemodError("java_import does not accept object_path")
            if not self.files:
                object.__setattr__(self, "files", ("*.java",))
        else:
            if not self.files:
                raise CodemodError("json_field rules require at least one file pattern")
            if any(not isinstance(part, (str, int)) or isinstance(part, bool)
                   for part in self.object_path):
                raise CodemodError("JSON object_path parts must be string keys or integer indexes")
        for pattern in self.files:
            _validate_pattern(pattern)
        for limitation in self.limitations:
            _non_empty("limitation", limitation)

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "id": self.rule_id,
            "kind": self.kind,
            "mode": self.mode,
            "source_identity": self.source_identity.to_dict(),
            "target_identity": self.target_identity.to_dict(),
            "source_value": self.source_value,
            "target_value": self.target_value,
            "evidence": [item.to_dict() for item in self.evidence],
            "limitations": list(self.limitations),
        }
        if self.files:
            result["files"] = list(self.files)
        if self.object_path:
            result["object_path"] = list(self.object_path)
        return result


def _validate_pattern(pattern: str) -> None:
    pattern = _non_empty("file pattern", pattern)
    if "\\" in pattern or pattern.startswith("/"):
        raise CodemodSecurityError(f"file pattern must be portable and relative: {pattern!r}")
    if any(part == ".." for part in pattern.split("/")):
        raise CodemodSecurityError(f"file pattern must not contain parent traversal: {pattern!r}")


def _valid_java_name(value: str) -> bool:
    parts = value.split(".")
    return len(parts) > 1 and all(
        part and (part[0].isalpha() or part[0] in "_$")
        and all(char.isalnum() or char in "_$" for char in part)
        for part in parts
    )


@dataclass(frozen=True, slots=True)
class InputState:
    path: str
    sha256: str | None

    def to_dict(self) -> dict[str, str | None]:
        return {"path": self.path, "sha256": self.sha256}


@dataclass(frozen=True, slots=True)
class CodemodSkip:
    rule_id: str
    reason: str
    path: str | None = None
    detail: str | None = None
    occurrences: int = 0

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "rule_id": self.rule_id,
            "reason": self.reason,
            "occurrences": self.occurrences,
        }
        if self.path is not None:
            result["path"] = self.path
        if self.detail is not None:
            result["detail"] = self.detail
        return result


@dataclass(frozen=True, slots=True)
class CodemodChange:
    kind: Literal["write", "rename"]
    path: str
    before_sha256: str
    after_sha256: str
    patch: str
    rule_ids: tuple[str, ...]
    target_path: str | None = None
    _before: bytes = field(default=b"", repr=False, compare=False)
    _after: bytes = field(default=b"", repr=False, compare=False)
    _mode: int = field(default=0o644, repr=False, compare=False)

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "kind": self.kind,
            "path": self.path,
            "before_sha256": self.before_sha256,
            "after_sha256": self.after_sha256,
            "rule_ids": list(self.rule_ids),
            "patch": self.patch,
        }
        if self.target_path is not None:
            result["target_path"] = self.target_path
        return result


@dataclass(frozen=True, slots=True)
class CodemodPlan:
    root: str
    candidate_revision: str | None
    source_identity: VersionIdentity
    target_identity: VersionIdentity
    rules_sha256: str
    input_sha256: str
    inputs: tuple[InputState, ...]
    changes: tuple[CodemodChange, ...]
    skipped: tuple[CodemodSkip, ...]
    _rules: tuple[CodemodRule, ...] = field(repr=False, compare=False)

    @property
    def patch(self) -> str:
        return "".join(change.patch for change in self.changes)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "root": self.root,
            "candidate_revision": self.candidate_revision,
            "source_identity": self.source_identity.to_dict(),
            "target_identity": self.target_identity.to_dict(),
            "rules_sha256": self.rules_sha256,
            "rules": [rule.to_dict() for rule in self._rules],
            "input_sha256": self.input_sha256,
            "inputs": [item.to_dict() for item in self.inputs],
            "changes": [item.to_dict() for item in self.changes],
            "skipped": [item.to_dict() for item in self.skipped],
            "patch": self.patch,
        }


@dataclass(frozen=True, slots=True)
class CodemodApplyResult:
    input_sha256: str
    applied_changes: int
    changed_paths: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "input_sha256": self.input_sha256,
            "applied_changes": self.applied_changes,
            "changed_paths": list(self.changed_paths),
        }


def _workspace_root(root: str | os.PathLike[str]) -> Path:
    path = Path(root).absolute()
    if not path.exists() or not path.is_dir():
        raise CodemodSecurityError("codemod root must be an existing directory")
    if path.is_symlink() or path.resolve() != path:
        raise CodemodSecurityError("codemod root must not be or traverse a symbolic link")
    return path


def _clean_candidate_revision(root: Path) -> str | None:
    """Return HEAD for a clean Git root, or ``None`` for a non-Git directory."""

    environment = {**os.environ, 'GIT_CONFIG_NOSYSTEM': '1', 'GIT_CONFIG_GLOBAL': '/dev/null',
                   'GIT_NO_REPLACE_OBJECTS': '1', 'GIT_TERMINAL_PROMPT': '0'}
    git_command = ['git', '-c', 'core.hooksPath=/dev/null', '-c', 'core.fsmonitor=false',
                   '-C', str(root)]
    probe = git_probe(
        [*git_command, "rev-parse", "--show-toplevel", "HEAD"],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        check=False,
        timeout=30, env=environment,
    )
    if probe.returncode:
        return None
    lines = probe.stdout.splitlines()
    if len(lines) != 2:
        raise CodemodSecurityError("cannot establish the Git candidate identity")
    top_level = Path(lines[0]).resolve()
    if top_level != root:
        raise CodemodSecurityError("codemod root must be the Git worktree root")
    revision = lines[1].strip()
    if len(revision) != 40 or any(char not in _HEX_DIGITS for char in revision.lower()):
        raise CodemodSecurityError("Git candidate HEAD is not an exact commit identity")
    status = git_probe(
        [*git_command, "status", "--porcelain=v1", "--untracked-files=all"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
        timeout=30, env=environment,
    )
    if status.returncode:
        raise CodemodSecurityError("cannot inspect Git candidate cleanliness")
    dirty = status.stdout.splitlines()
    if dirty:
        first = dirty[0][:200]
        raise CodemodConflictError(f"Git candidate is dirty: {first}")
    return revision.lower()


def _checked_path(root: Path, relative: str, *, allow_missing: bool = False) -> Path:
    relative = _safe_relative(relative)
    candidate = root.joinpath(*PurePosixPath(relative).parts)
    current = root
    parts = PurePosixPath(relative).parts
    for index, part in enumerate(parts):
        current = current / part
        try:
            metadata = current.lstat()
        except FileNotFoundError:
            if allow_missing:
                break
            raise CodemodError(f"path does not exist: {relative}") from None
        if stat.S_ISLNK(metadata.st_mode):
            raise CodemodSecurityError(f"symbolic links are not accepted: {relative}")
        if index < len(parts) - 1 and not stat.S_ISDIR(metadata.st_mode):
            raise CodemodSecurityError(f"path ancestor is not a directory: {relative}")
    if candidate.resolve(strict=False).is_relative_to(root) is False:
        raise CodemodSecurityError(f"path escapes codemod root: {relative}")
    return candidate


def _walk_regular_files(root: Path) -> tuple[str, ...]:
    result: list[str] = []

    def visit(directory: Path, prefix: PurePosixPath) -> None:
        try:
            entries = sorted(os.scandir(directory), key=lambda item: item.name)
        except OSError as error:
            raise CodemodError(f"cannot scan {prefix.as_posix() or '.'}: {error}") from error
        for entry in entries:
            from .local_workspace_sandbox import is_sensitive_name
            if is_sensitive_name(entry.name):
                continue
            relative = prefix / entry.name
            if entry.is_symlink():
                raise CodemodSecurityError(
                    f"symbolic links are not accepted in codemod scope: {relative.as_posix()}"
                )
            if entry.is_dir(follow_symlinks=False):
                if entry.name not in _EXCLUDED_DIRECTORIES:
                    visit(Path(entry.path), relative)
            elif entry.is_file(follow_symlinks=False):
                result.append(relative.as_posix())
            else:
                raise CodemodSecurityError(
                    f"non-regular filesystem entry is not accepted: {relative.as_posix()}"
                )

    visit(root, PurePosixPath())
    return tuple(result)


def _read_input(root: Path, relative: str) -> tuple[bytes, int]:
    path = _checked_path(root, relative)
    metadata = path.stat()
    if not stat.S_ISREG(metadata.st_mode):
        raise CodemodSecurityError(f"codemod inputs must be regular files: {relative}")
    return path.read_bytes(), stat.S_IMODE(metadata.st_mode)


def _decode_text(relative: str, data: bytes) -> str:
    if b"\0" in data:
        raise CodemodError(f"text input contains NUL bytes: {relative}")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as error:
        raise CodemodError(f"text input is not UTF-8: {relative}") from error


def _matches(relative: str, patterns: Sequence[str]) -> bool:
    return any(fnmatch.fnmatchcase(relative, pattern) for pattern in patterns)


def _mask_java_non_code(source: str) -> str:
    """Mask comments, strings, chars, and text blocks without changing offsets."""

    masked = list(source)
    index = 0
    state = "code"
    while index < len(source):
        char = source[index]
        following = source[index:index + 3]
        pair = source[index:index + 2]
        if state == "code":
            if pair == "//":
                masked[index:index + 2] = "  "
                index += 2
                state = "line"
                continue
            if pair == "/*":
                masked[index:index + 2] = "  "
                index += 2
                state = "block"
                continue
            if following == '\"\"\"':
                masked[index:index + 3] = "   "
                index += 3
                state = "text"
                continue
            if char == '"':
                masked[index] = " "
                index += 1
                state = "string"
                continue
            if char == "'":
                masked[index] = " "
                index += 1
                state = "char"
                continue
            index += 1
            continue
        if state == "line":
            if char in "\r\n":
                state = "code"
            else:
                masked[index] = " "
            index += 1
            continue
        if state == "block":
            if pair == "*/":
                masked[index:index + 2] = "  "
                index += 2
                state = "code"
            else:
                if char not in "\r\n":
                    masked[index] = " "
                index += 1
            continue
        if state == "text":
            if following == '\"\"\"':
                masked[index:index + 3] = "   "
                index += 3
                state = "code"
            elif char == "\\" and index + 1 < len(source):
                masked[index] = " "
                if source[index + 1] not in "\r\n":
                    masked[index + 1] = " "
                index += 2
            else:
                if char not in "\r\n":
                    masked[index] = " "
                index += 1
            continue
        quote = '"' if state == "string" else "'"
        if char == "\\" and index + 1 < len(source):
            masked[index] = " "
            if source[index + 1] not in "\r\n":
                masked[index + 1] = " "
            index += 2
        else:
            if char not in "\r\n":
                masked[index] = " "
            index += 1
            if char == quote:
                state = "code"
    return "".join(masked)


def _java_import_spans(source: str) -> list[tuple[str, int, int]]:
    import re

    masked = _mask_java_non_code(source)
    pattern = re.compile(
        r"(?m)^[ \t]*import[ \t]+(?:static[ \t]+)?"
        r"(?P<name>[A-Za-z_$][A-Za-z0-9_$]*(?:\.[A-Za-z_$][A-Za-z0-9_$]*)+)"
        r"[ \t]*;"
    )
    return [
        (match.group("name"), match.start("name"), match.end("name"))
        for match in pattern.finditer(masked)
    ]


def _unified_patch(path: str, before: str, after: str) -> str:
    lines = difflib.unified_diff(
        before.splitlines(),
        after.splitlines(),
        fromfile=f"a/{path}",
        tofile=f"b/{path}",
        lineterm="",
    )
    patch = "\n".join(lines)
    return patch + ("\n" if patch else "")


def _rename_patch(source: str, target: str) -> str:
    return (
        f"diff --git a/{source} b/{target}\n"
        "similarity index 100%\n"
        f"rename from {source}\n"
        f"rename to {target}\n"
    )


def _json_no_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise CodemodConflictError(f"JSON object contains duplicate field {key!r}")
        result[key] = value
    return result


def _json_container(document: Any, object_path: Sequence[str | int]) -> dict[str, Any] | None:
    current = document
    for part in object_path:
        if isinstance(part, str) and isinstance(current, Mapping) and part in current:
            current = current[part]
        elif isinstance(part, int) and isinstance(current, list) and -len(current) <= part < len(current):
            current = current[part]
        else:
            return None
    return current if isinstance(current, dict) else None


def _rules_digest(rules: Sequence[CodemodRule]) -> str:
    return _digest_json([rule.to_dict() for rule in rules])


def _validate_rule_set(rules: Sequence[CodemodRule]) -> None:
    ids: set[str] = set()
    value_graphs: dict[str, dict[str, str]] = {
        "java_import": {},
        "json_field": {},
    }
    rename_sources: set[str] = set()
    rename_targets: set[str] = set()
    for rule in rules:
        if rule.rule_id in ids:
            raise CodemodConflictError(f"duplicate rule id: {rule.rule_id}")
        ids.add(rule.rule_id)
        if rule.kind in value_graphs and rule.mode == "transform":
            graph = value_graphs[rule.kind]
            previous = graph.setdefault(rule.source_value, rule.target_value)
            if previous != rule.target_value:
                raise CodemodConflictError(
                    f"conflicting {rule.kind} targets for {rule.source_value!r}"
                )
        if rule.kind == "path_rename" and rule.mode == "transform":
            source = _safe_relative(rule.source_value)
            target = _safe_relative(rule.target_value)
            if source in rename_sources or target in rename_targets:
                raise CodemodConflictError("path rename sources and targets must be unique")
            rename_sources.add(source)
            rename_targets.add(target)
    if rename_sources & rename_targets:
        raise CodemodConflictError("path rename chains are not supported in one plan")
    for kind, graph in value_graphs.items():
        sources = set(graph)
        targets = list(graph.values())
        if sources & set(targets):
            raise CodemodConflictError(f"{kind} transform chains are not supported in one plan")
        if len(targets) != len(set(targets)):
            raise CodemodConflictError(
                f"multiple {kind} transforms must not converge on one target"
            )


def plan_codemod(
    root: str | os.PathLike[str],
    *,
    source_identity: VersionIdentity,
    target_identity: VersionIdentity,
    rules: Iterable[CodemodRule] | None = None,
) -> CodemodPlan:
    """Create a deterministic patch without modifying ``root``."""

    workspace = _workspace_root(root)
    candidate_revision = _clean_candidate_revision(workspace)
    selected_rules = eventbus_import_rules() if rules is None else tuple(rules)
    ordered_rules = tuple(sorted(tuple(selected_rules), key=lambda rule: rule.rule_id))
    _validate_rule_set(ordered_rules)
    skipped: list[CodemodSkip] = []
    active: list[CodemodRule] = []
    for rule in ordered_rules:
        if (rule.source_identity != source_identity
                or rule.target_identity != target_identity):
            skipped.append(CodemodSkip(rule.rule_id, "identity_mismatch"))
        else:
            active.append(rule)

    scanned_files: tuple[str, ...] = ()
    if any(rule.kind in {"java_import", "json_field"} for rule in active):
        scanned_files = _walk_regular_files(workspace)

    inputs: dict[str, InputState] = {}
    changes: list[CodemodChange] = []
    matched_rules: set[str] = set()
    changed_paths: set[str] = set()

    java_rules = [rule for rule in active if rule.kind == "java_import"]
    java_files = [
        relative for relative in scanned_files
        if any(_matches(relative, rule.files) for rule in java_rules)
    ]
    for relative in java_files:
        before, mode = _read_input(workspace, relative)
        inputs[relative] = InputState(relative, _digest_bytes(before))
        text = _decode_text(relative, before)
        imports = _java_import_spans(text)
        imported_names = {name for name, _, _ in imports}
        replacements: list[tuple[int, int, str, str]] = []
        for rule in java_rules:
            if not _matches(relative, rule.files):
                continue
            spans = [(start, end) for name, start, end in imports if name == rule.source_value]
            if not spans:
                continue
            matched_rules.add(rule.rule_id)
            if rule.mode == "detect-only":
                skipped.append(CodemodSkip(
                    rule.rule_id, "detect_only", relative, occurrences=len(spans)
                ))
                continue
            if rule.target_value in imported_names:
                skipped.append(CodemodSkip(
                    rule.rule_id,
                    "target_import_present",
                    relative,
                    "replacement would create a duplicate import",
                    len(spans),
                ))
                continue
            replacements.extend(
                (start, end, rule.target_value, rule.rule_id) for start, end in spans
            )
        if replacements:
            after_text = text
            for start, end, target, _ in sorted(replacements, reverse=True):
                after_text = after_text[:start] + target + after_text[end:]
            after = after_text.encode("utf-8")
            rule_ids = tuple(sorted({item[3] for item in replacements}))
            changes.append(CodemodChange(
                kind="write",
                path=relative,
                before_sha256=_digest_bytes(before),
                after_sha256=_digest_bytes(after),
                patch=_unified_patch(relative, text, after_text),
                rule_ids=rule_ids,
                _before=before,
                _after=after,
                _mode=mode,
            ))
            changed_paths.add(relative)

    json_rules = [rule for rule in active if rule.kind == "json_field"]
    json_files = [
        relative for relative in scanned_files
        if any(_matches(relative, rule.files) for rule in json_rules)
    ]
    for relative in json_files:
        if relative in changed_paths:
            raise CodemodConflictError(f"multiple transform kinds target {relative}")
        before, mode = _read_input(workspace, relative)
        inputs[relative] = InputState(relative, _digest_bytes(before))
        before_text = _decode_text(relative, before)
        try:
            document = json.loads(before_text, object_pairs_hook=_json_no_duplicates)
        except (json.JSONDecodeError, CodemodConflictError) as error:
            for rule in json_rules:
                if _matches(relative, rule.files):
                    skipped.append(CodemodSkip(
                        rule.rule_id, "invalid_json", relative, str(error)
                    ))
            continue
        applied: list[str] = []
        for rule in json_rules:
            if not _matches(relative, rule.files):
                continue
            container = _json_container(document, rule.object_path)
            if container is None:
                skipped.append(CodemodSkip(rule.rule_id, "precondition_failed", relative))
                continue
            if rule.source_value not in container:
                reason = "already_applied" if rule.target_value in container else "source_not_found"
                skipped.append(CodemodSkip(rule.rule_id, reason, relative))
                continue
            matched_rules.add(rule.rule_id)
            if rule.target_value in container:
                raise CodemodConflictError(
                    f"JSON field conflict in {relative}: both {rule.source_value!r} "
                    f"and {rule.target_value!r} exist"
                )
            if rule.mode == "detect-only":
                skipped.append(CodemodSkip(rule.rule_id, "detect_only", relative, occurrences=1))
                continue
            renamed = {
                (rule.target_value if key == rule.source_value else key): value
                for key, value in container.items()
            }
            container.clear()
            container.update(renamed)
            applied.append(rule.rule_id)
        if applied:
            after_text = json.dumps(document, ensure_ascii=False, indent=2) + "\n"
            after = after_text.encode("utf-8")
            changes.append(CodemodChange(
                kind="write",
                path=relative,
                before_sha256=_digest_bytes(before),
                after_sha256=_digest_bytes(after),
                patch=_unified_patch(relative, before_text, after_text),
                rule_ids=tuple(sorted(applied)),
                _before=before,
                _after=after,
                _mode=mode,
            ))
            changed_paths.add(relative)

    path_rules = [rule for rule in active if rule.kind == "path_rename"]
    for rule in path_rules:
        source = _safe_relative(rule.source_value)
        target = _safe_relative(rule.target_value)
        source_path = _checked_path(workspace, source, allow_missing=True)
        target_path = _checked_path(workspace, target, allow_missing=True)
        source_exists = source_path.exists()
        target_exists = target_path.exists()
        source_data: bytes | None = None
        source_mode = 0o644
        if source_exists:
            source_data, source_mode = _read_input(workspace, source)
            inputs[source] = InputState(source, _digest_bytes(source_data))
        else:
            inputs[source] = InputState(source, None)
        if target_exists:
            target_data, _ = _read_input(workspace, target)
            inputs[target] = InputState(target, _digest_bytes(target_data))
        else:
            inputs[target] = InputState(target, None)
        if source_exists and target_exists:
            raise CodemodConflictError(
                f"rename destination already exists while source remains: {target}"
            )
        if not source_exists:
            skipped.append(CodemodSkip(
                rule.rule_id,
                "already_applied" if target_exists else "source_not_found",
                source,
            ))
            continue
        matched_rules.add(rule.rule_id)
        if rule.mode == "detect-only":
            skipped.append(CodemodSkip(rule.rule_id, "detect_only", source, occurrences=1))
            continue
        if source in changed_paths or target in changed_paths:
            raise CodemodConflictError(f"rename overlaps another change: {source} -> {target}")
        assert source_data is not None
        checksum = _digest_bytes(source_data)
        changes.append(CodemodChange(
            kind="rename",
            path=source,
            target_path=target,
            before_sha256=checksum,
            after_sha256=checksum,
            patch=_rename_patch(source, target),
            rule_ids=(rule.rule_id,),
            _before=source_data,
            _after=source_data,
            _mode=source_mode,
        ))
        changed_paths.update({source, target})

    for rule in active:
        if rule.rule_id not in matched_rules and not any(
            item.rule_id == rule.rule_id for item in skipped
        ):
            skipped.append(CodemodSkip(rule.rule_id, "source_not_found"))

    ordered_inputs = tuple(inputs[path] for path in sorted(inputs))
    input_sha256 = _digest_json([item.to_dict() for item in ordered_inputs])
    ordered_changes = tuple(sorted(changes, key=lambda item: (item.path, item.target_path or "")))
    ordered_skips = tuple(sorted(
        skipped,
        key=lambda item: (item.rule_id, item.path or "", item.reason, item.detail or ""),
    ))
    return CodemodPlan(
        root=str(workspace),
        candidate_revision=candidate_revision,
        source_identity=source_identity,
        target_identity=target_identity,
        rules_sha256=_rules_digest(ordered_rules),
        input_sha256=input_sha256,
        inputs=ordered_inputs,
        changes=ordered_changes,
        skipped=ordered_skips,
        _rules=ordered_rules,
    )


def _change_fingerprint(changes: Sequence[CodemodChange]) -> str:
    return _digest_json([
        {
            "kind": item.kind,
            "path": item.path,
            "target_path": item.target_path,
            "before_sha256": item.before_sha256,
            "after_sha256": item.after_sha256,
            "rule_ids": item.rule_ids,
            "patch": item.patch,
        }
        for item in changes
    ])


def _atomic_write(path: Path, data: bytes, mode: int) -> None:
    descriptor, temporary_name = tempfile.mkstemp(prefix=".modport-codemod-", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _ensure_parent(root: Path, relative: str) -> list[Path]:
    path = _checked_path(root, relative, allow_missing=True)
    missing: list[Path] = []
    current = path.parent
    while current != root and not current.exists():
        missing.append(current)
        current = current.parent
    if current.is_symlink() or not current.is_dir():
        raise CodemodSecurityError(f"unsafe destination parent for {relative}")
    for directory in reversed(missing):
        directory.mkdir()
    return missing


def apply_codemod(
    root: str | os.PathLike[str],
    plan: CodemodPlan,
) -> CodemodApplyResult:
    """Apply a previously inspected plan after a full deterministic preflight."""

    workspace = _workspace_root(root)
    if str(workspace) != plan.root:
        raise StaleCodemodPlanError("plan belongs to a different codemod root")
    current = plan_codemod(
        workspace,
        source_identity=plan.source_identity,
        target_identity=plan.target_identity,
        rules=plan._rules,
    )
    if current.rules_sha256 != plan.rules_sha256:
        raise StaleCodemodPlanError("rule set changed after planning")
    if current.candidate_revision != plan.candidate_revision:
        raise StaleCodemodPlanError("Git candidate revision changed after planning")
    if current.input_sha256 != plan.input_sha256:
        raise StaleCodemodPlanError("codemod input changed after planning")
    if _change_fingerprint(current.changes) != _change_fingerprint(plan.changes):
        raise StaleCodemodPlanError("planned patch no longer matches the candidate")

    # Never trust private candidate bytes carried by a caller-supplied plan.
    # The freshly recomputed changes are the authenticated apply payload.
    changes = current.changes

    completed: list[CodemodChange] = []
    created_directories: list[Path] = []
    try:
        for change in changes:
            before, _ = _read_input(workspace, change.path)
            if _digest_bytes(before) != change.before_sha256:
                raise StaleCodemodPlanError(
                    f"codemod input changed immediately before apply: {change.path}"
                )
            if change.kind == "write":
                path = _checked_path(workspace, change.path)
                _atomic_write(path, change._after, change._mode)
            else:
                assert change.target_path is not None
                source = _checked_path(workspace, change.path)
                target = _checked_path(workspace, change.target_path, allow_missing=True)
                if target.exists():
                    raise CodemodConflictError(
                        f"rename destination appeared after planning: {change.target_path}"
                    )
                created_directories.extend(_ensure_parent(workspace, change.target_path))
                os.replace(source, target)
            completed.append(change)
    except BaseException:
        for change in reversed(completed):
            if change.kind == "write":
                path = _checked_path(workspace, change.path)
                _atomic_write(path, change._before, change._mode)
            else:
                assert change.target_path is not None
                source = _checked_path(workspace, change.path, allow_missing=True)
                target = _checked_path(workspace, change.target_path)
                if not source.exists():
                    os.replace(target, source)
        for directory in reversed(created_directories):
            try:
                directory.rmdir()
            except OSError:
                pass
        raise

    changed_paths = tuple(
        change.target_path if change.kind == "rename" else change.path
        for change in changes
        if (change.target_path if change.kind == "rename" else change.path) is not None
    )
    return CodemodApplyResult(
        input_sha256=plan.input_sha256,
        applied_changes=len(changes),
        changed_paths=changed_paths,
    )


FORGE_1_20_1 = VersionIdentity("1.20.1", "forge", "47.0.1")
NEOFORGE_26_1_2 = VersionIdentity("26.1.2", "neoforge", "26.1.2.106")
_BUS_8_0_5_SHA256 = "705e24f37752809499a5cd0c7dd31eaabf3d933c5d3b2060168e94e9ef62593a"
_BUS_8_0_5_URL = (
    "https://maven.neoforged.net/releases/net/neoforged/bus/8.0.5/bus-8.0.5.jar"
)


def eventbus_import_rules(*, mode: RuleMode = "detect-only") -> tuple[CodemodRule, ...]:
    """Return the three audited EventBus import mappings.

    The locked jar proves only that the target classes exist.  It does not
    establish event timing, cancellation, posting, threading, or other API
    semantics.  Callers must pass ``mode="transform"`` explicitly to write.
    """

    mappings = (
        ("IEventBus", "net/neoforged/bus/api/IEventBus.class"),
        ("SubscribeEvent", "net/neoforged/bus/api/SubscribeEvent.class"),
        ("EventPriority", "net/neoforged/bus/api/EventPriority.class"),
    )
    limitations = (
        "bus 8.0.5 class existence does not prove source/target API semantics",
        "event timing, cancellation, posting, and threading require separate verification",
    )
    rules: list[CodemodRule] = []
    for simple_name, class_entry in mappings:
        evidence = (
            RuleEvidence(
                source="https://neoforged.net/news/20.2eventbus-changes/",
                locator="Event Bus package migration announced for NeoForge 20.2",
                supports=(
                    "the EventBus API package moved from net.minecraftforge.eventbus.api "
                    "to net.neoforged.bus.api"
                ),
            ),
            RuleEvidence(
                source=_BUS_8_0_5_URL,
                locator=f"locked bus-8.0.5.jar entry {class_entry}",
                supports=f"target class {simple_name} exists in the locked EventBus jar",
                sha256=_BUS_8_0_5_SHA256,
            ),
        )
        rules.append(CodemodRule(
            rule_id=f"eventbus-import-{simple_name.lower()}",
            kind="java_import",
            source_identity=FORGE_1_20_1,
            target_identity=NEOFORGE_26_1_2,
            source_value=f"net.minecraftforge.eventbus.api.{simple_name}",
            target_value=f"net.neoforged.bus.api.{simple_name}",
            evidence=evidence,
            mode=mode,
            limitations=limitations,
        ))
    return tuple(rules)


def audited_import_rules(*, mode: RuleMode = 'detect-only') -> tuple[CodemodRule, ...]:
    """v20 exact imports; symbol existence is not behavioral equivalence."""
    rules = list(eventbus_import_rules(mode=mode))
    forge_url = 'https://maven.minecraftforge.net/net/minecraftforge/'
    neo_url = 'https://maven.neoforged.net/releases/'
    source_fml = (forge_url + 'javafmllanguage/1.20.1-47.0.1/javafmllanguage-1.20.1-47.0.1.jar',
                  '85483c09fc7037efa9c8d85932b05ed9782e1b8f1df12ad0765f148b982ccf99')
    target_fml = (neo_url + 'net/neoforged/fancymodloader/loader/11.0.15/loader-11.0.15.jar',
                  'dad0947efdfbc1b09f52c7779c86c3660ed6d385fd5c07b3b649332f6f75a429')
    source_forge = (forge_url + 'forge/1.20.1-47.0.1/forge-1.20.1-47.0.1-universal.jar',
                    '3ab522e1ecd4b7768f7d8c29cd76783ca85cae519517331a155b3d8d63e765f3')
    target_neo = (neo_url + 'net/neoforged/neoforge/26.1.2.106/neoforge-26.1.2.106-universal.jar',
                  'b37e097292d6631cf2ff6c3d6d1ad63dc9ce704eae198d49f1bd48baeb5e7776')
    mappings = (
        ('fml.common.Mod', 'fml.common.Mod', source_fml, target_fml),
        ('fml.event.lifecycle.FMLCommonSetupEvent', 'fml.event.lifecycle.FMLCommonSetupEvent', source_forge, target_fml),
        ('event.entity.player.PlayerEvent', 'neoforge.event.entity.player.PlayerEvent', source_forge, target_neo),
        ('registries.DeferredRegister', 'neoforge.registries.DeferredRegister', source_forge, target_neo),
        ('registries.RegisterEvent', 'neoforge.registries.RegisterEvent', source_forge, target_neo),
    )
    for old, new, source_jar, target_jar in mappings:
        old, new = 'net.minecraftforge.' + old, 'net.neoforged.' + new
        evidence = tuple(RuleEvidence(source=url, locator=name.replace('.', '/') + '.class',
            supports='Exact version artifact contains this declared class; method compatibility is not established.',
            sha256=digest) for name, (url, digest) in ((old, source_jar), (new, target_jar)))
        rules.append(CodemodRule(
            rule_id='audited-import-' + old.rsplit('.', 1)[-1].lower(), kind='java_import',
            source_identity=FORGE_1_20_1, target_identity=NEOFORGE_26_1_2,
            source_value=old, target_value=new, evidence=evidence, mode=mode,
            limitations=('Only explicit imports are mapped; constructors, generics, registry keys, '
                         'event lifecycle and threading require semantic migration and validation.',)))
    return tuple(rules)


__all__ = [
    "audited_import_rules",
    "CodemodApplyResult",
    "CodemodChange",
    "CodemodConflictError",
    "CodemodError",
    "CodemodPlan",
    "CodemodRule",
    "CodemodSecurityError",
    "CodemodSkip",
    "FORGE_1_20_1",
    "InputState",
    "NEOFORGE_26_1_2",
    "RuleEvidence",
    "StaleCodemodPlanError",
    "VersionIdentity",
    "apply_codemod",
    "eventbus_import_rules",
    "plan_codemod",
]
