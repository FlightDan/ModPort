"""Reconcile uncertain native Codex dialogue turns from persisted history."""
from __future__ import annotations

from typing import Any


_TURN_STATUSES = {"completed", "interrupted", "failed", "inProgress"}


def _paginated_data(transport, method: str, params: dict[str, Any]) -> list[dict[str, Any]]:
    """Read every page, rejecting malformed or cyclic protocol responses."""
    rows: list[dict[str, Any]] = []
    cursor: str | None = None
    seen_cursors: set[str] = set()
    while True:
        request = dict(params)
        if cursor is not None:
            request["cursor"] = cursor
        response = transport.request(method, request)
        if not isinstance(response, dict) or not isinstance(response.get("data"), list):
            raise RuntimeError(f"{method} returned malformed pagination data")
        page = response["data"]
        if any(not isinstance(row, dict) for row in page):
            raise RuntimeError(f"{method} returned a malformed row")
        rows.extend(page)
        if "nextCursor" not in response:
            raise RuntimeError(f"{method} response omitted nextCursor")
        if "backwardsCursor" not in response:
            raise RuntimeError(f"{method} response omitted backwardsCursor")
        backwards_cursor = response["backwardsCursor"]
        if backwards_cursor is not None and not isinstance(backwards_cursor, str):
            raise RuntimeError(f"{method} returned a malformed backwardsCursor")
        next_cursor = response["nextCursor"]
        if next_cursor is None:
            return rows
        if not isinstance(next_cursor, str):
            raise RuntimeError(f"{method} returned a malformed nextCursor")
        if next_cursor in seen_cursors:
            raise RuntimeError(f"{method} repeated a pagination cursor")
        seen_cursors.add(next_cursor)
        cursor = next_cursor


def _turns(transport, thread_id: str) -> list[dict[str, Any]]:
    return _paginated_data(transport, "thread/turns/list", {
        "threadId": thread_id,
        "sortDirection": "asc",
        "itemsView": "notLoaded",
    })


def _items(transport, thread_id: str, turn_id: str | None = None) -> list[dict[str, Any]]:
    params = {"threadId": thread_id, "sortDirection": "asc"}
    if turn_id is not None:
        params["turnId"] = turn_id
    return _paginated_data(transport, "thread/items/list", params)


def _turn_snapshot(turns: list[dict[str, Any]]) -> tuple[tuple[Any, ...], ...]:
    snapshot = []
    for turn in turns:
        turn_id = turn.get("id")
        status = turn.get("status")
        if not isinstance(turn_id, str) or status not in _TURN_STATUSES:
            raise RuntimeError("thread/turns/list returned a malformed turn")
        snapshot.append((turn_id, status, turn.get("startedAt"), turn.get("completedAt")))
    return tuple(snapshot)


def _matching_entries(entries: list[dict[str, Any]], client_id: str) -> list[dict[str, Any]]:
    matches = []
    for entry in entries:
        turn_id = entry.get("turnId")
        item = entry.get("item")
        if not isinstance(turn_id, str) or not isinstance(item, dict):
            raise RuntimeError("thread/items/list returned a malformed item entry")
        if item.get("type") == "userMessage" and item.get("clientId") == client_id:
            if not isinstance(item.get("id"), str):
                raise RuntimeError("thread/items/list returned a malformed userMessage")
            matches.append(entry)
    if len(matches) > 1:
        raise RuntimeError("pending client user message matched multiple persisted items")
    return matches


def _matching_turn(turns: list[dict[str, Any]], turn_id: str) -> dict[str, Any]:
    matches = [turn for turn in turns if turn.get("id") == turn_id]
    if len(matches) != 1:
        raise RuntimeError("pending user message has no unique persisted turn")
    return matches[0]


def _agent_messages(entries: list[dict[str, Any]], turn_id: str) -> list[dict[str, Any]]:
    messages = []
    for entry in entries:
        if entry.get("turnId") != turn_id:
            raise RuntimeError("filtered thread/items/list returned an item from another turn")
        item = entry.get("item")
        if not isinstance(item, dict):
            raise RuntimeError("thread/items/list returned a malformed item entry")
        if item.get("type") != "agentMessage":
            continue
        if not isinstance(item.get("id"), str) or not isinstance(item.get("text"), str):
            raise RuntimeError("thread/items/list returned a malformed agentMessage")
        messages.append(dict(item))
    return messages


def reconcile_pending_turn(transport, thread_id: str,
                           pending: dict[str, Any]) -> dict[str, Any] | None:
    """Find the one turn correlated with a durable pending-turn intent.

    ``clientUserMessageId`` is a correlation field, not an idempotency key.
    Zero matches cannot prove that an attempted request was unapplied, so this
    helper raises for absence as well as malformed or ambiguous history.  It
    never converts an uncertain request into another model turn.
    """
    if not isinstance(thread_id, str) or not thread_id:
        raise ValueError("thread_id must be a non-empty string")
    client_id = pending.get("client_user_message_id") if isinstance(pending, dict) else None
    if not isinstance(client_id, str) or not client_id:
        raise ValueError("pending turn requires client_user_message_id")

    # The two list APIs do not promise a shared snapshot. Recheck absent items
    # across quiescent turn snapshots to allow a persisted response to become
    # visible. Absence remains uncertain and never authorizes another start.
    while True:
        before = _turns(transport, thread_id)
        before_snapshot = _turn_snapshot(before)
        entries = _items(transport, thread_id)
        matches = _matching_entries(entries, client_id)
        after = _turns(transport, thread_id)
        after_snapshot = _turn_snapshot(after)
        if matches:
            break
        if any(turn[1] == "inProgress" for turn in after_snapshot):
            raise RuntimeError("pending turn absence is uncertain while a turn is in progress")
        if before_snapshot == after_snapshot:
            # Confirm item visibility once more after the stable quiescent turn
            # boundary before reporting that its outcome remains uncertain.
            matches = _matching_entries(_items(transport, thread_id), client_id)
            if not matches:
                raise RuntimeError(
                    "pending turn outcome is uncertain: no correlated persisted user message")
            after = _turns(transport, thread_id)

    matched_turn_id = matches[0]["turnId"]
    expected_turn_id = pending.get("turn_id")
    if expected_turn_id is not None:
        if not isinstance(expected_turn_id, str) or expected_turn_id != matched_turn_id:
            raise RuntimeError("pending turn id conflicts with persisted client message")
    turn = _matching_turn(after, matched_turn_id)
    _turn_snapshot([turn])

    # Fetch this turn's full items after observing its status, then verify that
    # the status did not change during item pagination. A live completion after
    # the final read is delivered through the subscription established by the
    # caller's preceding thread/resume.
    while True:
        messages = _agent_messages(_items(transport, thread_id, matched_turn_id),
                                   matched_turn_id)
        refreshed = _matching_turn(_turns(transport, thread_id), matched_turn_id)
        _turn_snapshot([refreshed])
        if _turn_snapshot([turn]) == _turn_snapshot([refreshed]):
            return {"id": matched_turn_id, "status": refreshed["status"],
                    "items": messages}
        turn = refreshed
