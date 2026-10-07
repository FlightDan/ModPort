"""Bounded, read-only discovery of repair candidates in a source workspace.

The inventory is deliberately diagnostic.  Its findings are useful locations
for a repair agent, but neither a complete scan nor an empty issue list is
acceptance evidence.
"""

from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass
from hashlib import sha256
import heapq
import json
from .platform_files import file_os as os
from pathlib import Path, PurePosixPath
import re
import stat
from typing import Iterable, Mapping, Pattern
from urllib.parse import unquote, urlparse


MAX_FILES = 10_000
MAX_FILE_BYTES = 2 * 1024 * 1024
MAX_TOTAL_BYTES = 32 * 1024 * 1024
MAX_LOG_BYTES = 32 * 1024 * 1024
MAX_LOG_SOURCES = 1_000
MAX_LOG_TASKS = 256
MAX_ISSUES = 10_000
MAX_LOCATIONS = 20_000
MAX_EVIDENCE = 20_000
MAX_SKIPPED = 2_000
MAX_REPORT_BYTES = 12 * 1024 * 1024

EXCLUDED_DIRECTORIES = frozenset(
    {
        ".git",
        ".gradle",
        ".hg",
        ".idea",
        ".mypy_cache",
        ".modport",
        ".pytest_cache",
        ".ruff_cache",
        ".svn",
        ".tox",
        ".venv",
        "__pycache__",
        "build",
        "deployments",
        "dist",
        "node_modules",
        "out",
        "run",
        "runs",
        "target",
        "venv",
    }
)
EXCLUDED_DIRECTORY_PREFIXES = (".codex-",)


@dataclass(frozen=True, slots=True)
class InventoryRule:
    """One text rule used by the bounded scanner.

    Keeping rules as values makes the scanner straightforward to extend without
    coupling traversal and report construction to Minecraft-specific matching.
    """

    rule_id: str
    kind: str
    summary: str
    pattern: Pattern[str]
    file_kind: str


DEFAULT_RULES = (
    InventoryRule(
        rule_id="forge-package-reference",
        kind="source_reference",
        summary="Forge package reference remains in source",
        pattern=re.compile(r"\bnet\.minecraftforge(?:\.[A-Za-z_$][\w$]*)*"),
        file_kind="source",
    ),
    InventoryRule(
        rule_id="forge-build-dependency",
        kind="build_configuration",
        summary="Forge dependency or build plugin remains configured",
        pattern=re.compile(
            r"\bnet\.minecraftforge(?::forge|\.gradle(?::ForgeGradle)?)\b"
        ),
        file_kind="build",
    ),
)

_SOURCE_SUFFIXES = frozenset({".java", ".kt"})
_BUILD_FILENAMES = frozenset(
    {"build.gradle", "build.gradle.kts", "settings.gradle", "settings.gradle.kts",
     "gradle.properties", "libs.versions.toml"}
)
_BUILTIN_RULE_IDS = frozenset(
    {"forge-resource-path", "compiler-error", "missing-input", "caller-observation"}
)

_JAVAC_DIAGNOSTIC = re.compile(
    r"(?P<path>(?:file://)?(?:[A-Za-z]:)?[^\r\n]*?\.(?:java|kt|kts))"
    r":(?P<line>\d+)(?::(?P<column>\d+))?:\s*"
    r"(?P<message>(?:error\s*:|e\s*:).+)$",
    re.IGNORECASE,
)
_KOTLIN_DIAGNOSTIC = re.compile(
    r"(?:^|\s)e:\s*(?P<path>(?:file://)?(?:[A-Za-z]:)?[^\r\n]*?\.kt)"
    r":?\s*\((?P<line>\d+)\s*,\s*(?P<column>\d+)\)\s*:?[ \t]*"
    r"(?P<message>.+)$",
    re.IGNORECASE,
)
_KOTLIN_COLON_DIAGNOSTIC = re.compile(
    r"(?:^|\s)e:\s*(?P<path>(?:file://)?(?:[A-Za-z]:)?[^\r\n]*?\.kt)"
    r":(?P<line>\d+)(?::(?P<column>\d+))?\s*:?[ \t]*(?P<message>.+)$",
    re.IGNORECASE,
)
_GRADLE_TASK = re.compile(
    r"^\s*>\s*Task\s+(?P<task>:\S+?)(?:\s+(?P<status>FAILED|UP-TO-DATE|SKIPPED|NO-SOURCE|FROM-CACHE))?\s*$",
    re.IGNORECASE,
)
_BUILD_STATUS = re.compile(r"\bBUILD\s+(?P<status>SUCCESSFUL|FAILED)\b", re.IGNORECASE)
_COMPILER_TRUNCATION = (
    re.compile(r"\bonly showing the first\s+\d+\s+errors?,\s+of\s+\d+\s+total\b", re.IGNORECASE),
    re.compile(r"\btoo many errors emitted, stopping now\b", re.IGNORECASE),
    re.compile(r"\berror limit reached\b", re.IGNORECASE),
)


def _relative_path(root: Path, path: Path) -> str | None:
    """Return a normalized workspace-relative path without following it."""

    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return None


def _is_resource_path(path: PurePosixPath) -> tuple[bool, str]:
    parts = path.parts
    for source_index, part in enumerate(parts):
        if part != "src":
            continue
        for resource_index in range(source_index + 1, len(parts) - 2):
            if parts[resource_index:resource_index + 3] == ("resources", "data", "forge"):
                return True, "data/forge"
    for index in range(len(parts) - 2):
        if parts[index] == "data" and parts[index + 2] == "forge":
            return True, f"data/{parts[index + 1]}/forge"
    return False, ""


def _file_kind(relative: str) -> str | None:
    path = PurePosixPath(relative)
    if path.suffix in _SOURCE_SUFFIXES:
        return "source"
    if path.name in _BUILD_FILENAMES:
        return "build"
    if path.parent.name == "gradle" and path.name.endswith(".versions.toml"):
        return "build"
    return None


def _issue_id(rule_id: str, path: str, symbol: str) -> str:
    identity = "\0".join((rule_id, path, symbol)).encode("utf-8")
    return "issue-" + sha256(identity).hexdigest()[:20]


def _excerpt(value: str) -> str:
    # Inspecting only a bounded prefix prevents a caller-controlled diagnostic
    # field from turning a short report value into an unbounded normalization.
    return re.sub(r"\s+", " ", value[:4096]).strip()[:240]


def _ruleset_identity(rules: Iterable[InventoryRule]) -> tuple[str, list[str]]:
    rows = [
        {
            "rule_id": rule.rule_id,
            "kind": rule.kind,
            "summary": rule.summary,
            "pattern": rule.pattern.pattern,
            "flags": rule.pattern.flags,
            "file_kind": rule.file_kind,
        }
        for rule in rules
    ]
    rows.extend(
        [
            {"rule_id": "forge-resource-path", "version": 1},
            {"rule_id": "compiler-error", "version": 3},
            {"rule_id": "missing-input", "version": 1},
            {"rule_id": "caller-observation", "version": 1},
        ]
    )
    rows.sort(key=lambda row: (str(row["rule_id"]), str(row.get("pattern", ""))))
    raw = json.dumps(rows, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return sha256(raw).hexdigest(), sorted(str(row["rule_id"]) for row in rows)


def _has_symlink_component(path: Path) -> bool:
    current = Path(path.anchor)
    try:
        for part in path.parts[1:]:
            current /= part
            if current.is_symlink():
                return True
    except OSError:
        return True
    return False


def _lines(text: str):
    start = 0
    while start < len(text):
        end = text.find("\n", start)
        if end < 0:
            yield text[start:].rstrip("\r")
            return
        yield text[start:end].rstrip("\r")
        start = end + 1


def _mark_truncated(coverage: dict, reason: str, scope: str) -> None:
    coverage["truncated"] = True
    coverage["scan_complete"] = False
    if scope == "source":
        coverage["source_complete"] = False
    elif scope == "logs":
        coverage["logs_complete"] = False
    coverage["truncation_reasons"].append(reason)


def _mark_incomplete(coverage: dict, reason: str, scope: str) -> None:
    coverage["scan_complete"] = False
    if scope == "source":
        coverage["source_complete"] = False
    elif scope == "logs":
        coverage["logs_complete"] = False
    coverage["incomplete_reasons"].append(reason)


class _Issues:
    def __init__(self, coverage: dict) -> None:
        self._coverage = coverage
        self._items: dict[tuple[str, str, str], dict] = {}
        self._location_count = 0
        self._evidence_count = 0

    def add(
        self,
        *,
        rule_id: str,
        kind: str,
        summary: str,
        path: str,
        line: int,
        symbol: str,
        excerpt: str,
        evidence_source: str,
        evidence_detail: str,
        scope: str,
    ) -> bool:
        key = (rule_id, path, symbol)
        issue = self._items.get(key)
        location = {"path": path, "line": line, "symbol": symbol, "excerpt": excerpt}
        if issue is None:
            if len(self._items) >= MAX_ISSUES:
                self._coverage["issues_omitted"] += 1
                _mark_truncated(self._coverage, "issue_limit", scope)
                return False
            if self._location_count >= MAX_LOCATIONS:
                self._coverage["locations_omitted"] += 1
                _mark_truncated(self._coverage, "location_limit", scope)
                return False
            if self._evidence_count >= MAX_EVIDENCE:
                self._coverage["evidence_omitted"] += 1
                _mark_truncated(self._coverage, "evidence_limit", scope)
                return False
            issue = {
                "issue_id": _issue_id(*key),
                "kind": kind,
                "rule_id": rule_id,
                "summary": summary,
                "locations": [],
                "evidence": [],
                "status": "open",
            }
            self._items[key] = issue
            issue["locations"].append(location)
            self._location_count += 1
        if location not in issue["locations"]:
            if self._location_count >= MAX_LOCATIONS:
                self._coverage["locations_omitted"] += 1
                _mark_truncated(self._coverage, "location_limit", scope)
                return False
            issue["locations"].append(location)
            self._location_count += 1
        evidence = {"source": evidence_source, "detail": evidence_detail}
        if evidence not in issue["evidence"]:
            if self._evidence_count >= MAX_EVIDENCE:
                self._coverage["evidence_omitted"] += 1
                _mark_truncated(self._coverage, "evidence_limit", scope)
                return False
            issue["evidence"].append(evidence)
            self._evidence_count += 1
        return True

    def result(self) -> list[dict]:
        values = list(self._items.values())
        for issue in values:
            issue["locations"].sort(
                key=lambda value: (value["path"], value["line"], value["symbol"], value["excerpt"])
            )
            issue["evidence"].sort(key=lambda value: (value["source"], value["detail"]))
        values.sort(key=lambda value: value["issue_id"])
        return values


def _record_skip(
    coverage: dict, path: str, reason: str, *, incomplete: bool, scope: str = "source"
) -> None:
    coverage["skipped_total"] += 1
    if len(coverage["skipped"]) < MAX_SKIPPED:
        coverage["skipped"].append({"path": path, "reason": reason})
    else:
        coverage["skipped_omitted"] += 1
        _mark_truncated(coverage, "skipped_entry_limit", scope)
    if incomplete:
        coverage["scan_complete"] = False
        if scope == "source":
            coverage["source_complete"] = False
        elif scope == "logs":
            coverage["logs_complete"] = False


def _read_text(path: Path, size: int) -> tuple[str | None, str | None]:
    """Read a regular file without following a final symlink."""

    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
        try:
            current = os.fstat(descriptor)
            if not stat.S_ISREG(current.st_mode):  # pragma: no cover - guarded by traversal
                return None, "not_regular_file"
            if current.st_size != size:
                return None, "file_changed_during_scan"
            data = b""
            while len(data) <= MAX_FILE_BYTES:
                chunk = os.read(descriptor, min(64 * 1024, MAX_FILE_BYTES + 1 - len(data)))
                if not chunk:
                    break
                data += chunk
        finally:
            os.close(descriptor)
    except OSError:
        return None, "read_failed"
    if len(data) > MAX_FILE_BYTES:
        return None, "file_byte_limit"
    if b"\0" in data:
        return None, "binary_file"
    try:
        return data.decode("utf-8"), None
    except UnicodeDecodeError:
        return None, "non_utf8_file"


def _scan_text(
    issues: _Issues,
    relative: str,
    text: str,
    file_kind: str,
    rules: Iterable[InventoryRule],
) -> None:
    lines = text.splitlines()
    newlines = [index for index, character in enumerate(text) if character == "\n"]
    for rule in rules:
        if rule.file_kind != file_kind:
            continue
        for match in rule.pattern.finditer(text):
            line = bisect_right(newlines, match.start()) + 1
            symbol = _excerpt(match.group(0))
            source_line = lines[line - 1] if line <= len(lines) else symbol
            if not issues.add(
                rule_id=rule.rule_id,
                kind=rule.kind,
                summary=rule.summary,
                path=relative,
                line=line,
                symbol=symbol,
                excerpt=_excerpt(source_line),
                evidence_source="workspace_scan",
                evidence_detail=f"text match for {rule.rule_id}",
                scope="source",
            ):
                break


def _walk_workspace(
    root: Path,
    issues: _Issues,
    coverage: dict,
    rules: tuple[InventoryRule, ...],
    scoped_files: dict[str, set[str]],
) -> None:
    pending = [root]
    stop = False
    while pending and not stop:
        directory = pending.pop()
        try:
            with os.scandir(directory) as iterator:
                entries = sorted(iterator, key=lambda entry: entry.name, reverse=True)
        except OSError:
            relative = _relative_path(root, directory) or "."
            _record_skip(coverage, relative, "directory_read_failed", incomplete=True)
            continue
        for entry in entries:
            path = Path(entry.path)
            relative = _relative_path(root, path)
            if relative is None:
                _record_skip(coverage, str(path), "outside_workspace", incomplete=True)
                continue
            try:
                if entry.is_symlink():
                    _record_skip(coverage, relative, "symbolic_link", incomplete=True)
                    continue
                if entry.is_dir(follow_symlinks=False):
                    if (entry.name in EXCLUDED_DIRECTORIES
                            or entry.name.startswith(EXCLUDED_DIRECTORY_PREFIXES)):
                        _record_skip(coverage, relative, "excluded_directory", incomplete=False)
                    else:
                        pending.append(path)
                    continue
                if not entry.is_file(follow_symlinks=False):
                    _record_skip(coverage, relative, "not_regular_file", incomplete=True)
                    continue
                stat = entry.stat(follow_symlinks=False)
            except OSError:
                _record_skip(coverage, relative, "metadata_read_failed", incomplete=True)
                continue

            resource, resource_symbol = _is_resource_path(PurePosixPath(relative))
            file_kind = _file_kind(relative)
            if file_kind is None and not resource:
                continue
            if coverage["files_scanned"] >= MAX_FILES:
                _mark_truncated(coverage, "file_count_limit", "source")
                _record_skip(coverage, relative, "file_count_limit", incomplete=True)
                stop = True
                break
            coverage["files_scanned"] += 1

            if resource:
                issues.add(
                    rule_id="forge-resource-path",
                    kind="resource_path",
                    summary="Forge resource path remains in the workspace",
                    path=relative,
                    line=1,
                    symbol=resource_symbol,
                    excerpt=relative,
                    evidence_source="workspace_path",
                    evidence_detail="path matches a Forge resource layout",
                    scope="source",
                )
                scoped_files["resource_files"].add(relative)
            if file_kind is None:
                continue
            if stat.st_size > MAX_FILE_BYTES:
                _mark_truncated(coverage, "file_byte_limit", "source")
                _record_skip(coverage, relative, "file_byte_limit", incomplete=True)
                continue
            if coverage["bytes_scanned"] + stat.st_size > MAX_TOTAL_BYTES:
                _mark_truncated(coverage, "total_byte_limit", "source")
                _record_skip(coverage, relative, "total_byte_limit", incomplete=True)
                stop = True
                break
            text, error = _read_text(path, stat.st_size)
            if error is not None:
                _record_skip(coverage, relative, error, incomplete=True)
                continue
            coverage["bytes_scanned"] += stat.st_size
            scoped_files[file_kind + "_files"].add(relative)
            _scan_text(issues, relative, text or "", file_kind, rules)


def _log_path(root: Path, raw: str) -> tuple[str | None, Path | None]:
    value = raw.strip()
    if value.startswith("file:"):
        parsed = urlparse(value)
        value = unquote(parsed.path)
    value = value.replace("\\", "/")
    candidate = Path(value)
    if candidate.is_absolute():
        # Sandboxed build tools commonly expose the candidate root as
        # /workspace.  Remap that stable virtual prefix to the supplied root.
        if candidate.parts[:2] == ("/", "workspace"):
            absolute = Path(os.path.abspath(root.joinpath(*candidate.parts[2:])))
        else:
            absolute = Path(os.path.abspath(candidate))
    else:
        if ".." in PurePosixPath(value).parts:
            return None, None
        absolute = Path(os.path.abspath(root / candidate))
    relative = _relative_path(root, absolute)
    return (relative, absolute) if relative is not None else (None, None)


def _source_excerpt(root: Path, relative: str, line: int, fallback: str) -> str:
    path = root / relative
    try:
        if _has_symlink_component(root):
            return _excerpt(fallback)
        current = root
        for part in PurePosixPath(relative).parts:
            current = current / part
            if current.is_symlink():
                return _excerpt(fallback)
        if not path.is_file() or path.stat().st_size > MAX_FILE_BYTES:
            return _excerpt(fallback)
        text, error = _read_text(path, path.stat().st_size)
        lines = text.splitlines() if error is None and text is not None else []
        return _excerpt(lines[line - 1] if 0 < line <= len(lines) else fallback)
    except OSError:
        return _excerpt(fallback)


def _diagnostic_symbol(message: str, declared_symbol: str | None = None) -> str:
    value = re.sub(r"^(?:error|e)\s*:\s*", "", message.strip(), flags=re.IGNORECASE)
    value = _excerpt(value).lower() or "compiler error"
    if declared_symbol:
        value += ": " + _excerpt(declared_symbol).lower()
    return value


def _bounded_log_prefix(text: str, available: int) -> tuple[str, int, bool]:
    if available <= 0:
        return "", 0, bool(text)
    prefix = text[:available]
    raw = prefix.encode("utf-8", errors="replace")
    truncated = len(prefix) < len(text) or len(raw) > available
    raw = raw[:available]
    bounded = raw.decode("utf-8", errors="ignore")
    if truncated and bounded and not bounded.endswith(("\n", "\r")):
        newline = bounded.rfind("\n")
        bounded = bounded[:newline + 1] if newline >= 0 else ""
    return bounded, len(raw), truncated


def _emit_diagnostic(root: Path, issues: _Issues, source: str, diagnostic: dict) -> bool:
    declared = diagnostic.get("declared_symbol")
    message = diagnostic["message"]
    symbol = _diagnostic_symbol(message, declared)
    summary = _excerpt(message + (f" ({declared})" if declared else ""))
    detail = diagnostic["raw"] + (f"; symbol: {declared}" if declared else "")
    return issues.add(
        rule_id="compiler-error",
        kind="compilation_error",
        summary=summary,
        path=diagnostic["path"],
        line=diagnostic["line"],
        symbol=symbol,
        excerpt=_source_excerpt(root, diagnostic["path"], diagnostic["line"], diagnostic["raw"]),
        evidence_source=f"log:{source}",
        evidence_detail=_excerpt(detail),
        scope="logs",
    )


def _scan_log_text(root: Path, issues: _Issues, source: str, text: str) -> bool:
    pending: dict | None = None
    for raw_line in _lines(text):
        diagnostic_line = re.sub(r"^(?:\[[A-Z]+\]\s*|>\s*)", "", raw_line)
        match = (_KOTLIN_DIAGNOSTIC.search(diagnostic_line)
                 or _KOTLIN_COLON_DIAGNOSTIC.search(diagnostic_line)
                 or _JAVAC_DIAGNOSTIC.search(diagnostic_line))
        if match is not None:
            if pending is not None:
                if not _emit_diagnostic(root, issues, source, pending):
                    return False
                pending = None
            relative, _ = _log_path(root, match.group("path"))
            if relative is None:
                continue
            message = match.group("message")
            diagnostic = {
                "path": relative,
                "line": int(match.group("line")),
                "message": message,
                "raw": raw_line,
                "following_lines": 0,
            }
            if _diagnostic_symbol(message) == "cannot find symbol":
                pending = diagnostic
            else:
                if not _emit_diagnostic(root, issues, source, diagnostic):
                    return False
            continue
        if pending is None:
            continue
        symbol_match = re.match(r"\s*symbol\s*:\s*(.+?)\s*$", raw_line, re.IGNORECASE)
        if symbol_match:
            pending["declared_symbol"] = symbol_match.group(1)
            if not _emit_diagnostic(root, issues, source, pending):
                return False
            pending = None
            continue
        pending["following_lines"] += 1
        if pending["following_lines"] >= 6:
            if not _emit_diagnostic(root, issues, source, pending):
                return False
            pending = None
    if pending is not None:
        return _emit_diagnostic(root, issues, source, pending)
    return True


def _execution_scope(text: str) -> tuple[dict, bool, bool]:
    tasks: dict[str, str] = {}
    tasks_truncated = False
    compiler_truncated = False
    build_status = "unknown"
    for raw_line in _lines(text):
        status_match = _BUILD_STATUS.search(raw_line)
        if status_match:
            build_status = status_match.group("status").lower()
        task_match = _GRADLE_TASK.match(raw_line)
        if task_match:
            task = _excerpt(task_match.group("task"))
            status = (task_match.group("status") or "executed").lower().replace("-", "_")
            if task in tasks or len(tasks) < MAX_LOG_TASKS:
                tasks[task] = status
            else:
                tasks_truncated = True
        if any(pattern.search(raw_line) for pattern in _COMPILER_TRUNCATION):
            compiler_truncated = True
    scope = {
        "build_status": build_status,
        "execution_scope_known": build_status == "successful" or bool(tasks),
        "tasks": [
            {"task": task, "status": tasks[task]}
            for task in sorted(tasks)
        ],
        "tasks_truncated": tasks_truncated,
    }
    return scope, compiler_truncated, tasks_truncated


def _scan_logs(
    root: Path, issues: _Issues, coverage: dict, log_texts: Mapping[str, str]
) -> tuple[list[str], list[str], list[dict]]:
    selected = heapq.nsmallest(
        MAX_LOG_SOURCES + 1, log_texts.items(), key=lambda item: str(item[0])
    )
    if len(selected) > MAX_LOG_SOURCES:
        _mark_truncated(coverage, "log_source_limit", "logs")
        selected = selected[:MAX_LOG_SOURCES]
    declared_sources = [_excerpt(str(item[0])) for item in selected]
    scanned_sources: list[str] = []
    execution_scopes: list[dict] = []
    for raw_source, text in selected:
        source = _excerpt(str(raw_source))
        if not isinstance(text, str):
            _record_skip(
                coverage, f"log:{source}", "invalid_log_text", incomplete=True, scope="logs"
            )
            continue
        available = MAX_LOG_BYTES - coverage["log_bytes_scanned"]
        bounded, byte_count, truncated = _bounded_log_prefix(text, available)
        coverage["log_bytes_scanned"] += byte_count
        scanned_sources.append(source)
        execution_scope, compiler_truncated, tasks_truncated = _execution_scope(bounded)
        execution_scopes.append({"source": source, **execution_scope})
        if compiler_truncated:
            _mark_truncated(coverage, "compiler_diagnostics_truncated", "logs")
        if tasks_truncated:
            _mark_truncated(coverage, "log_task_scope_limit", "logs")
        if not execution_scope["execution_scope_known"]:
            _mark_incomplete(coverage, "compiler_execution_scope_unknown", "logs")
        log_complete = _scan_log_text(root, issues, source, bounded)
        if truncated:
            _mark_truncated(coverage, "log_byte_limit", "logs")
            break
        if not log_complete:
            break
    return declared_sources, scanned_sources, execution_scopes


def _observation_evidence(value) -> str:
    if isinstance(value, str):
        return _excerpt(value)
    if isinstance(value, Mapping):
        parts = []
        for index, key in enumerate(heapq.nsmallest(9, value, key=lambda item: str(item))):
            if index == 8:
                parts.append("...")
                break
            item = value[key]
            rendered = item if isinstance(item, (str, int, float, bool, type(None))) else type(item).__name__
            parts.append(f"{_excerpt(str(key))}={_excerpt(str(rendered))}")
        return _excerpt(", ".join(parts))
    if isinstance(value, (list, tuple)):
        parts = [
            _excerpt(str(item if isinstance(item, (str, int, float, bool, type(None)))
                         else type(item).__name__))
            for item in value[:8]
        ]
        if len(value) > 8:
            parts.append("...")
        return _excerpt(", ".join(parts))
    return _excerpt(str(value))


def _add_observations(
    root: Path, issues: _Issues, coverage: dict, observations: list[dict]
) -> list[str]:
    categories: set[str] = set()
    for index, observation in enumerate(observations):
        if not isinstance(observation, dict):
            _record_skip(
                coverage, f"observation:{index}", "invalid_observation", incomplete=True,
                scope="observations",
            )
            continue
        category = _excerpt(str(observation.get("category") or observation.get("kind")
                                or "observation"))
        summary = _excerpt(str(observation.get("summary") or "Caller supplied observation"))
        symbol = _excerpt(str(observation.get("symbol") or summary))
        raw_path = str(observation.get("path") or "")
        if raw_path:
            path, _ = _log_path(root, raw_path)
            if path is None:
                _record_skip(
                    coverage, f"observation:{index}", "observation_path_outside_workspace",
                    incomplete=True, scope="observations",
                )
                continue
        else:
            path = ""
        raw_line = observation.get("line", 0)
        line = raw_line if type(raw_line) is int and raw_line >= 0 else 0
        rule_id = _excerpt(str(observation.get("rule_id") or "caller-observation"))
        evidence = _observation_evidence(observation.get("evidence", "caller supplied"))
        categories.add(category)
        if not issues.add(
            rule_id=rule_id,
            kind=category,
            summary=summary,
            path=path,
            line=line,
            symbol=symbol,
            excerpt=_excerpt(str(observation.get("excerpt") or summary)),
            evidence_source="caller_observation",
            evidence_detail=evidence,
            scope="observations",
        ):
            break
    return sorted(categories)


def _serialized_size(value: dict) -> int:
    return len(json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8"))


def _update_report_size(report: dict) -> int:
    coverage = report["coverage"]
    coverage["report_bytes"] = 0
    while True:
        size = _serialized_size(report)
        if coverage["report_bytes"] == size:
            return size
        coverage["report_bytes"] = size


def _fit_report(report: dict) -> dict:
    """Keep the final artifact below the loader ceiling without hiding loss."""

    size = _update_report_size(report)
    if size <= MAX_REPORT_BYTES:
        return report
    coverage = report["coverage"]
    coverage["report_bytes_before_truncation"] = size
    coverage["scope_paths_omitted"] = 0
    _mark_truncated(coverage, "report_byte_limit", "source")
    if report["scan_scope"]["logs_provided"]:
        coverage["logs_complete"] = False
    coverage["complete"] = False
    report["scan_scope"]["source_complete"] = False
    report["scan_scope"]["logs_complete"] = coverage["logs_complete"]

    while size > MAX_REPORT_BYTES:
        changed = False
        skipped = coverage["skipped"]
        if len(skipped) > 1:
            kept = max(1, len(skipped) // 2)
            coverage["skipped_omitted"] += len(skipped) - kept
            del skipped[kept:]
            changed = True
        for key in ("source_files", "build_files", "resource_files"):
            values = report["scan_scope"][key]
            if len(values) > 1:
                kept = max(1, len(values) // 2)
                coverage["scope_paths_omitted"] += len(values) - kept
                del values[kept:]
                changed = True
        executions = report["scan_scope"]["log_scope"].get("executions", [])
        for execution in executions:
            tasks = execution["tasks"]
            if len(tasks) > 1:
                kept = max(1, len(tasks) // 2)
                coverage["log_scope_entries_omitted"] += len(tasks) - kept
                del tasks[kept:]
                execution["tasks_truncated"] = True
                changed = True
        if len(executions) > 1:
            kept = max(1, len(executions) // 2)
            coverage["log_scope_entries_omitted"] += len(executions) - kept
            del executions[kept:]
            changed = True
        for issue in report["issues"]:
            locations = issue["locations"]
            if len(locations) > 1:
                kept = max(1, len(locations) // 2)
                coverage["locations_omitted"] += len(locations) - kept
                del locations[kept:]
                changed = True
            evidence = issue["evidence"]
            if len(evidence) > 1:
                kept = max(1, len(evidence) // 2)
                coverage["evidence_omitted"] += len(evidence) - kept
                del evidence[kept:]
                changed = True
        issues = report["issues"]
        if len(issues) > 1:
            kept = max(1, len(issues) // 2)
            removed = issues[kept:]
            coverage["issues_omitted"] += len(removed)
            coverage["locations_omitted"] += sum(len(issue["locations"]) for issue in removed)
            coverage["evidence_omitted"] += sum(len(issue["evidence"]) for issue in removed)
            del issues[kept:]
            changed = True
        size = _update_report_size(report)
        if not changed:
            # All variable collections are already minimal.  With bounded text
            # fields and filesystem path limits this branch is not expected,
            # but an explicit failure is safer than publishing an unloadable
            # diagnostic artifact.
            raise ValueError("repair inventory cannot fit the report byte limit")

    coverage["issues_returned"] = len(report["issues"])
    coverage["locations_returned"] = sum(
        len(issue["locations"]) for issue in report["issues"]
    )
    coverage["evidence_returned"] = sum(
        len(issue["evidence"]) for issue in report["issues"]
    )
    if _update_report_size(report) > MAX_REPORT_BYTES:
        return _fit_report(report)
    return report


def collect_inventory(
    workspace: Path,
    *,
    candidate_id: str | None = None,
    execution_id: str | None = None,
    log_texts: dict[str, str] | None = None,
    missing_inputs: list[str] | None = None,
    rules: tuple[InventoryRule, ...] | None = None,
    observations: list[dict] | None = None,
) -> dict:
    """Collect stable, actionable repair candidates without executing the project."""

    root = Path(os.path.abspath(os.fspath(workspace)))
    effective_rules = DEFAULT_RULES + tuple(rules or ())
    if any(not isinstance(rule, InventoryRule) for rule in effective_rules):
        raise TypeError("rules must contain InventoryRule values")
    rule_ids = [rule.rule_id for rule in effective_rules]
    if len(rule_ids) != len(set(rule_ids)):
        raise ValueError("inventory rule IDs must be unique")
    if set(rule_ids) & _BUILTIN_RULE_IDS:
        raise ValueError("inventory text rule IDs must not replace built-in rules")
    ruleset, scoped_rule_ids = _ruleset_identity(effective_rules)
    coverage = {
        "scan_complete": True,
        "source_complete": True,
        "logs_complete": log_texts is not None,
        "truncated": False,
        "files_scanned": 0,
        "bytes_scanned": 0,
        "log_bytes_scanned": 0,
        "issues_omitted": 0,
        "locations_omitted": 0,
        "evidence_omitted": 0,
        "skipped_total": 0,
        "skipped_omitted": 0,
        "scope_paths_omitted": 0,
        "log_scope_entries_omitted": 0,
        "limits": {
            "max_files": MAX_FILES,
            "max_file_bytes": MAX_FILE_BYTES,
            "max_total_bytes": MAX_TOTAL_BYTES,
            "max_log_bytes": MAX_LOG_BYTES,
            "max_log_sources": MAX_LOG_SOURCES,
            "max_log_tasks": MAX_LOG_TASKS,
            "max_issues": MAX_ISSUES,
            "max_locations": MAX_LOCATIONS,
            "max_evidence": MAX_EVIDENCE,
            "max_skipped": MAX_SKIPPED,
            "max_report_bytes": MAX_REPORT_BYTES,
        },
        "excluded_directory_names": sorted(EXCLUDED_DIRECTORIES),
        "excluded_directory_prefixes": list(EXCLUDED_DIRECTORY_PREFIXES),
        "skipped": [],
        "truncation_reasons": [],
        "incomplete_reasons": [],
        "zero_issues_proves_absence": False,
    }
    issues = _Issues(coverage)
    scoped_files = {"source_files": set(), "build_files": set(), "resource_files": set()}
    try:
        valid_root = root.exists() and root.is_dir() and not _has_symlink_component(root)
    except OSError:
        valid_root = False
    if valid_root:
        _walk_workspace(root, issues, coverage, effective_rules, scoped_files)
    else:
        reason = "workspace_symlink_component" if _has_symlink_component(root) else "workspace_not_readable_directory"
        _record_skip(coverage, ".", reason, incomplete=True)

    declared_log_sources: list[str] = []
    scanned_log_sources: list[str] = []
    log_execution_scopes: list[dict] = []
    if log_texts is not None:
        declared_log_sources, scanned_log_sources, log_execution_scopes = _scan_logs(
            root, issues, coverage, log_texts
        )
    observation_categories = _add_observations(root, issues, coverage, observations or [])
    for value in missing_inputs or []:
        name = _excerpt(str(value))
        if not name:
            continue
        if not issues.add(
            rule_id="missing-input",
            kind="missing_input",
            summary=f"Required input is missing: {name}",
            path="",
            line=0,
            symbol=name,
            excerpt=name,
            evidence_source="missing_inputs",
            evidence_detail="reported by the caller",
            scope="inputs",
        ):
            break

    coverage["skipped"].sort(key=lambda value: (value["path"], value["reason"]))
    coverage["truncation_reasons"] = sorted(set(coverage["truncation_reasons"]))
    coverage["incomplete_reasons"] = sorted(set(coverage["incomplete_reasons"]))
    coverage["complete"] = coverage["scan_complete"]
    scan_scope = {
        "ruleset": ruleset,
        "rule_ids": scoped_rule_ids,
        "source_files": sorted(scoped_files["source_files"]),
        "build_files": sorted(scoped_files["build_files"]),
        "resource_files": sorted(scoped_files["resource_files"]),
        "source_complete": coverage["source_complete"],
        "logs_provided": log_texts is not None,
        "log_sources": declared_log_sources,
        "scanned_log_sources": scanned_log_sources,
        "log_scope": {
            "parser": "compiler-diagnostics-v3",
            "sources": declared_log_sources,
            "executions": log_execution_scopes,
        },
        "logs_complete": coverage["logs_complete"],
        "observations_provided": observations is not None,
        "observation_categories": observation_categories,
    }
    issue_rows = issues.result()
    coverage["issues_returned"] = len(issue_rows)
    coverage["locations_returned"] = sum(len(issue["locations"]) for issue in issue_rows)
    coverage["evidence_returned"] = sum(len(issue["evidence"]) for issue in issue_rows)
    report = {
        "schema_version": 1,
        "kind": "repair_inventory",
        "acceptance_evidence": False,
        "candidate_id": candidate_id,
        "execution_id": execution_id,
        "issues": issue_rows,
        "coverage": coverage,
        "scan_scope": scan_scope,
    }
    return _fit_report(report)
