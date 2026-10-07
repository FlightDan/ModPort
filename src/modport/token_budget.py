"""Application-owned cumulative provider usage, independent of SDK storage.

OpenCode 1.18 normalizes input/output to exclude cache/reasoning respectively.
The limit is observed-usage admission, not a provider streaming hard cap. Calls
already admitted can overshoot, and interrupted calls remain explicitly unknown.
"""
from __future__ import annotations

from contextlib import closing, contextmanager
import json
import math
from pathlib import Path
import sqlite3
import time
from typing import Any, Mapping


class TokenBudgetExceeded(RuntimeError):
    """The frozen cumulative token allowance has been consumed."""

    code = "token_budget_exhausted"


_LEDGER = "token-budget.sqlite3"


def _limit(root: Path) -> int | None:
    path = root / "run.json"
    if not path.is_file():
        return None
    value = json.loads(path.read_text(encoding="utf-8"))
    return value.get("request", {}).get("budget", {}).get("max_tokens")


@contextmanager
def _transaction(root: Path):
    root.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(root / _LEDGER, timeout=30, isolation_level=None)
    try:
        connection.execute("PRAGMA synchronous=FULL")
        connection.execute("BEGIN IMMEDIATE")
        connection.execute("CREATE TABLE IF NOT EXISTS budget (id INTEGER PRIMARY KEY CHECK(id=1), token_limit INTEGER)")
        connection.execute("CREATE TABLE IF NOT EXISTS calls (session_id TEXT, message_id TEXT, status TEXT NOT NULL, started_at REAL NOT NULL, PRIMARY KEY(session_id,message_id))")
        connection.execute("CREATE TABLE IF NOT EXISTS usage (session_id TEXT, message_id TEXT, provider_id TEXT, model_id TEXT, tokens INTEGER, complete INTEGER NOT NULL, details TEXT NOT NULL, PRIMARY KEY(session_id,message_id))")
        connection.execute("CREATE TABLE IF NOT EXISTS gaps (gap_id TEXT PRIMARY KEY, reason TEXT NOT NULL)")
        yield connection
        connection.execute("COMMIT")
    except BaseException:
        if connection.in_transaction:
            connection.execute("ROLLBACK")
        raise
    finally:
        connection.close()


def initialize_token_budget(run_dir: Path | str, limit: int | None = None) -> None:
    """Freeze the allowance once; resumption may not silently reset or raise it."""
    root = Path(run_dir).resolve()
    if limit is not None and (type(limit) is not int or limit < 0):
        raise ValueError("max_tokens must be a non-negative integer or None")
    with _transaction(root) as connection:
        connection.execute("INSERT OR IGNORE INTO budget VALUES (1, ?)", (limit,))
        current = connection.execute("SELECT token_limit FROM budget WHERE id=1").fetchone()[0]
        if current != limit:
            raise ValueError("token budget differs from the frozen instance allowance")


def _snapshot(connection: sqlite3.Connection) -> dict[str, Any]:
    row = connection.execute("SELECT token_limit FROM budget WHERE id=1").fetchone()
    limit = row[0] if row else None
    used = connection.execute("SELECT COALESCE(SUM(tokens),0) FROM usage").fetchone()[0]
    unknown = connection.execute("SELECT COUNT(*) FROM usage WHERE complete=0").fetchone()[0]
    in_flight = connection.execute("SELECT COUNT(*) FROM calls WHERE status='in_flight'").fetchone()[0]
    unresolved = connection.execute("SELECT COUNT(*) FROM calls WHERE status='unknown'").fetchone()[0]
    gaps = connection.execute("SELECT reason FROM gaps ORDER BY gap_id").fetchall()
    reported = connection.execute("SELECT COUNT(*) FROM usage WHERE tokens IS NOT NULL").fetchone()[0]
    visible_used = used if reported or not (unknown or in_flight or unresolved or gaps) else None
    return {"used_tokens": visible_used, "reported_tokens": used, "limit": limit, "usage_complete": bool(row) and not (unknown or in_flight or unresolved or gaps),
            "exhausted": limit is not None and used >= limit,
            "in_flight_calls": in_flight, "unknown_calls": unresolved,
            "unknown_messages": unknown, "coverage_gaps": [item[0] for item in gaps],
            "overshoot_tokens": max(0, used - limit) if limit is not None else 0,
            "enforcement": "observed_usage_admission", "hard_stream_cap": False}


def read_token_budget(run_dir: Path | str) -> dict[str, Any]:
    """Read a UI snapshot without creating storage or touching SDK databases."""
    root = Path(run_dir).resolve()
    path = root / _LEDGER
    if not path.is_file():
        return {"used_tokens": None, "limit": _limit(root), "usage_complete": False,
                "exhausted": _limit(root) == 0, "in_flight_calls": 0, "unknown_calls": 0,
                "unknown_messages": 0, "coverage_gaps": ["token ledger has not been initialized"],
                "overshoot_tokens": None, "enforcement": "observed_usage_admission", "hard_stream_cap": False}
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=30)) as connection:
        with connection:
            connection.execute("BEGIN")
            return _snapshot(connection)


def admit_token_call(run_dir: Path | str, session_id: str, message_id: str) -> None:
    """Atomically admit a real HTTP call, before posting it to OpenCode."""
    root = Path(run_dir).resolve()
    initialize_token_budget(root, _limit(root))
    with _transaction(root) as connection:
        if _snapshot(connection)["exhausted"]:
            raise TokenBudgetExceeded("the instance total token allowance is exhausted")
        connection.execute("INSERT INTO calls VALUES (?,?,'in_flight',?) ON CONFLICT(session_id,message_id) DO UPDATE SET status='in_flight'", (session_id, message_id, time.time()))


def finish_token_call(run_dir: Path | str, session_id: str, message_id: str, *, complete: bool) -> None:
    with _transaction(Path(run_dir)) as connection:
        connection.execute("UPDATE calls SET status=? WHERE session_id=? AND message_id=?", ("completed" if complete else "unknown", session_id, message_id))


def _normalized_usage(info: Mapping[str, Any]) -> tuple[int | None, bool, dict[str, Any]]:
    tokens = info.get("tokens")
    if not isinstance(tokens, Mapping):
        return None, False, {}
    def valid(value):
        return type(value) in (int, float) and math.isfinite(value) and value >= 0 and value == int(value)
    total = tokens.get("total")
    details = dict(tokens)
    if valid(total):
        return int(total), True, details
    cache = tokens.get("cache")
    categories = [tokens.get("input"), tokens.get("output"), tokens.get("reasoning"),
                  cache.get("read") if isinstance(cache, Mapping) else None,
                  cache.get("write") if isinstance(cache, Mapping) else None]
    available = [int(value) for value in categories if valid(value)]
    # OpenCode can normalize an omitted provider usage object to all zeros.
    # Without an explicit provider total, those zeros do not prove zero cost.
    return (sum(available) if available and sum(available) > 0 else None), all(valid(value) for value in categories) and sum(available) > 0, details


def record_token_message(run_dir: Path | str, info: Mapping[str, Any]) -> bool:
    """Upsert final assistant usage by actual session/message identity.

    Replayed SSE, message reads, retries and recovery all update one record.
    Partial updates cannot replace a complete observation or lower its total.
    """
    if info.get("role") != "assistant":
        return False
    session, message = info.get("sessionID"), info.get("id")
    if not isinstance(session, str) or not isinstance(message, str):
        mark_token_gap(run_dir, "message_identity_missing", "assistant usage omitted session or message ID")
        return False
    total, complete, details = _normalized_usage(info)
    times = info.get("time")
    complete = complete and isinstance(times, Mapping) and type(times.get("completed")) in (int, float)
    with _transaction(Path(run_dir)) as connection:
        connection.execute("INSERT INTO usage VALUES (?,?,?,?,?,?,?) ON CONFLICT(session_id,message_id) DO UPDATE SET tokens=CASE WHEN excluded.tokens IS NULL THEN usage.tokens ELSE MAX(COALESCE(usage.tokens,0),excluded.tokens) END, complete=MAX(usage.complete,excluded.complete), details=CASE WHEN usage.complete>excluded.complete THEN usage.details ELSE excluded.details END", (session, message, info.get("providerID"), info.get("modelID"), total, int(complete), json.dumps(details)))
    return complete


def mark_token_gap(run_dir: Path | str, gap_id: str, reason: str) -> None:
    with _transaction(Path(run_dir)) as connection:
        connection.execute("INSERT OR REPLACE INTO gaps VALUES (?,?)", (gap_id, reason))


def bind_token_budget(server: Any, run_dir: Path | str) -> None:
    """Bind a managed OpenCode transport to its application-owned instance."""
    root = Path(run_dir).resolve()
    initialize_token_budget(root, _limit(root))
    server._token_budget_root = root


def reconcile_token_session(server: Any, run_dir: Path | str, session_id: str,
                            *, cwd: Path, deadline: float,
                            message_id: str | None = None) -> bool:
    """Read parent and recursive child message usage through public HTTP APIs."""
    root = Path(run_dir).resolve()
    seen: set[str] = set()
    pending = [session_id]
    complete = True
    while pending:
        current = pending.pop()
        if current in seen:
            continue
        seen.add(current)
        try:
            messages = server.messages(current, cwd=cwd, deadline=deadline)
            infos = [item["info"] for item in messages if isinstance(item.get("info"), Mapping) and item["info"].get("role") == "assistant"]
            session_complete = bool(infos)
            for info in infos:
                session_complete = record_token_message(root, info) and session_complete
            complete = session_complete and complete
            if current == session_id and message_id is not None and not any(
                    info.get("parentID") == message_id for info in infos):
                complete = False
            if session_complete:
                settle_observed_token_calls(root, current, infos)
            children = server.children(current, cwd=cwd, deadline=deadline)
            pending.extend(child["id"] for child in children)
            with _transaction(root) as connection:
                connection.execute("DELETE FROM gaps WHERE gap_id=?", ("session:" + current,))
        except Exception as exc:
            mark_token_gap(root, "session:" + current, "session usage recovery unavailable: " + type(exc).__name__)
            complete = False
    return complete


def settle_observed_token_calls(run_dir: Path | str, session_id: str,
                                infos: list[Mapping[str, Any]]) -> None:
    """Recovery settles calls only when their own final usage was observed."""
    if not infos or not all(_normalized_usage(info)[1]
            and isinstance(info.get("time"), Mapping)
            and type(info["time"].get("completed")) in (int, float) for info in infos):
        return
    with _transaction(Path(run_dir)) as connection:
        for info in infos:
            parent = info.get("parentID")
            if isinstance(parent, str):
                connection.execute("UPDATE calls SET status='completed' WHERE session_id=? AND message_id=?", (session_id, parent))
