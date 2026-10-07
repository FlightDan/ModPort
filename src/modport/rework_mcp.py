"""A small stdio MCP bridge for reviewer-requested rework.

The server deliberately has no agent-launching capability.  It exchanges
immutable JSON files with the ModPort host, which remains responsible for
scheduling the requested target and publishing its response.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import stat
import sys
import threading
import time
from typing import Any, Mapping, TextIO
from uuid import uuid4

from .evidence import atomic_json


PROTOCOL_VERSION = "2024-11-05"
SUPPORTED_PROTOCOL_VERSIONS = frozenset(
    {PROTOCOL_VERSION, "2025-03-26", "2025-06-18"}
)
SERVER_NAME = "modport-rework"
SERVER_VERSION = "1.0"
MAX_RESPONSE_WAIT_SECONDS = 20 * 60
SETTLEMENT_ROOM_SECONDS = 60


class SessionError(ValueError):
    """The session descriptor or its filesystem boundary is invalid."""


class OperationalError(RuntimeError):
    """A tool call could not complete within the live session."""


def _regular_file(path: Path, *, label: str) -> Path:
    """Return an absolute, non-symlink regular file path."""

    if not path.is_absolute():
        raise SessionError(f"{label} path must be absolute")
    try:
        mode = path.lstat().st_mode
    except OSError as exc:
        raise SessionError(f"{label} is not a readable regular file") from exc
    if not stat.S_ISREG(mode) or path.is_symlink() or path.resolve() != path.absolute():
        raise SessionError(f"{label} is not a regular file at its declared path")
    return path


def _session_directory(path: Path) -> Path:
    directory = path.parent
    try:
        mode = directory.lstat().st_mode
    except OSError as exc:
        raise SessionError("session directory is unavailable") from exc
    if (not stat.S_ISDIR(mode) or directory.is_symlink()
            or directory.resolve() != directory.absolute()):
        raise SessionError("session directory must be a real directory")
    return directory


def _artifact_directory(session_dir: Path, name: str) -> Path:
    path = session_dir / name
    try:
        path.mkdir(mode=0o700)
    except FileExistsError:
        pass
    except OSError as exc:
        raise SessionError(f"cannot create session {name} directory") from exc
    try:
        mode = path.lstat().st_mode
    except OSError as exc:
        raise SessionError(f"session {name} directory is unavailable") from exc
    if not stat.S_ISDIR(mode) or path.is_symlink():
        raise SessionError(f"session {name} path must be a real directory")
    return path


def _read_json_file(path: Path, *, label: str) -> Any:
    _regular_file(path, label=label)
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
        try:
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                raise SessionError(f"{label} is not a regular file")
            with os.fdopen(descriptor, "r", encoding="utf-8") as stream:
                descriptor = -1
                return json.load(stream)
        finally:
            if descriptor >= 0:
                os.close(descriptor)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise SessionError(f"{label} is not valid UTF-8 JSON") from exc


def _required_text(value: Mapping[str, Any], key: str) -> str:
    item = value.get(key)
    if not isinstance(item, str) or not item.strip():
        raise SessionError(f"session {key} must be a non-empty string")
    return item


@dataclass(frozen=True)
class ReworkTarget:
    target_agent: str
    description: str
    routing: dict = field(default_factory=dict)

    def to_dict(self) -> dict[str, str]:
        return {"target_agent": self.target_agent, "description": self.description, **self.routing}


@dataclass(frozen=True)
class Session:
    descriptor_path: Path
    directory: Path
    requests_dir: Path
    responses_dir: Path
    run_id: str
    reviewer_execution_id: str
    deadline_epoch: float
    drain_pending_on_eof: bool
    targets: tuple[ReworkTarget, ...]
    workflow_version: int = 0
    repair_context: dict = field(default_factory=dict)

    @classmethod
    def load(cls, descriptor_path: Path) -> "Session":
        descriptor_path = _regular_file(descriptor_path, label="session descriptor")
        directory = _session_directory(descriptor_path)
        document = _read_json_file(descriptor_path, label="session descriptor")
        if not isinstance(document, dict):
            raise SessionError("session descriptor must be a JSON object")

        deadline = document.get("deadline_epoch")
        if (isinstance(deadline, bool) or not isinstance(deadline, (int, float))
                or not math.isfinite(deadline)):
            raise SessionError("session deadline_epoch must be finite")
        drain_pending_on_eof = document.get("drain_pending_on_eof", False)
        if not isinstance(drain_pending_on_eof, bool):
            raise SessionError("session drain_pending_on_eof must be boolean")
        workflow_version = document.get("workflow_version", 0)
        if type(workflow_version) is not int or workflow_version < 0:
            raise SessionError("session workflow_version must be a nonnegative integer")

        raw_targets = document.get("targets")
        if not isinstance(raw_targets, list):
            raise SessionError("session targets must be a list")
        targets: list[ReworkTarget] = []
        seen: set[str] = set()
        for raw in raw_targets:
            if not isinstance(raw, dict):
                raise SessionError("each session target must be an object")
            target_agent = _required_text(raw, "target_agent")
            description = raw.get("description")
            if not isinstance(description, str):
                raise SessionError("target description must be a string")
            if target_agent in seen:
                raise SessionError(f"duplicate session target: {target_agent}")
            seen.add(target_agent)
            routing = {key: raw[key] for key in ('stage', 'owned_paths', 'dependencies',
                       'goal_scope', 'recommended_issue_ids', 'recommended_issue_count')
                       if key in raw and 'owned_paths' in raw}
            targets.append(ReworkTarget(target_agent, description, routing))

        return cls(
            descriptor_path=descriptor_path,
            directory=directory,
            requests_dir=_artifact_directory(directory, "requests"),
            responses_dir=_artifact_directory(directory, "responses"),
            run_id=_required_text(document, "run_id"),
            reviewer_execution_id=_required_text(document, "reviewer_execution_id"),
            deadline_epoch=float(deadline),
            drain_pending_on_eof=drain_pending_on_eof,
            targets=tuple(targets),
            workflow_version=workflow_version,
            repair_context=document.get('repair_context', {}),
        )

    def target(self, name: str) -> ReworkTarget | None:
        return next((target for target in self.targets if target.target_agent == name), None)

    def is_closed(self) -> str | None:
        """Return a host-provided close reason, if the session is closed."""

        marker = self.directory / "closed.json"
        if marker.exists() or marker.is_symlink():
            document = _read_json_file(marker, label="session close marker")
            if isinstance(document, dict):
                reason = document.get("reason")
                if isinstance(reason, str) and reason:
                    return reason
            return "session closed"

        # A host may atomically replace the descriptor to close a session.
        document = _read_json_file(self.descriptor_path, label="session descriptor")
        if isinstance(document, dict) and (document.get("closed") is True
                                           or document.get("status") == "closed"):
            reason = document.get("close_reason")
            return reason if isinstance(reason, str) and reason else "session closed"
        return None


@dataclass
class PendingCall:
    rpc_id: Any
    cancelled: threading.Event = field(default_factory=threading.Event)
    request_id: str | None = None
    response_deadline_epoch: float | None = None
    cancel_reason: str = "cancelled by client"
    cancellation_written: bool = False
    state: str = "pending"
    lock: threading.Lock = field(default_factory=threading.Lock)
    finished: threading.Event = field(default_factory=threading.Event)


class ReworkServer:
    def __init__(self, session: Session, *, input_stream: TextIO = sys.stdin,
                 output_stream: TextIO = sys.stdout, poll_interval: float = 0.1):
        self.session = session
        self.input_stream = input_stream
        self.output_stream = output_stream
        self.poll_interval = poll_interval
        self._write_lock = threading.Lock()
        self._pending_lock = threading.Lock()
        self._pending: dict[str, PendingCall] = {}
        self._stop = threading.Event()

    @staticmethod
    def _rpc_key(rpc_id: Any) -> str:
        return json.dumps(rpc_id, ensure_ascii=False, separators=(",", ":"))

    def _send(self, message: Mapping[str, Any]) -> None:
        payload = json.dumps(message, ensure_ascii=False, separators=(",", ":"))
        with self._write_lock:
            try:
                self.output_stream.write(payload + "\n")
                self.output_stream.flush()
            except (BrokenPipeError, OSError, ValueError):
                self._stop.set()

    def _reply_result(self, rpc_id: Any, result: Mapping[str, Any]) -> None:
        self._send({"jsonrpc": "2.0", "id": rpc_id, "result": result})

    def _reply_error(self, rpc_id: Any, code: int, message: str) -> None:
        self._send({"jsonrpc": "2.0", "id": rpc_id,
                    "error": {"code": code, "message": message}})

    @staticmethod
    def _text_result(text: str, *, is_error: bool = False,
                     structured: Mapping[str, Any] | None = None) -> dict[str, Any]:
        result: dict[str, Any] = {"content": [{"type": "text", "text": text}]}
        if is_error:
            result["isError"] = True
        if structured is not None:
            result["structuredContent"] = dict(structured)
        return result

    @staticmethod
    def _failed_response_result(text: str, error: str) -> dict[str, Any]:
        """Keep both the target's raw output and the host failure diagnostic."""

        content = []
        if text:
            content.append({"type": "text", "text": text})
        content.append({"type": "text", "text": error})
        return {"content": content, "isError": True}

    @staticmethod
    def _tools() -> list[dict[str, Any]]:
        return [
            {
                "name": "list_rework_targets",
                "description": "List the agents this reviewer may ask to rework their output.",
                "inputSchema": {"type": "object", "properties": {},
                                "additionalProperties": False},
            },
            {
                "name": "request_rework",
                "description": (
                    "Send raw prose rework instructions to an allowed target and wait for "
                    "that target's raw response. Do not impose a report schema on the instructions."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "target_agent": {"type": "string", "minLength": 1},
                        "instructions": {"type": "string", "minLength": 1},
                    },
                    "required": ["target_agent", "instructions"],
                    "additionalProperties": False,
                },
            },
        ]

    def _write_cancellation(self, pending: PendingCall) -> None:
        with pending.lock:
            if pending.cancellation_written or pending.request_id is None:
                return
            atomic_json(
                self.session.requests_dir / f"{pending.request_id}.cancel.json",
                {
                    "request_id": pending.request_id,
                    "run_id": self.session.run_id,
                    "reviewer_execution_id": self.session.reviewer_execution_id,
                    "reason": pending.cancel_reason,
                    "cancelled_at": datetime.now(timezone.utc).isoformat(),
                },
            )
            pending.cancellation_written = True

    def _cancel(self, rpc_id: Any, reason: Any) -> None:
        with self._pending_lock:
            pending = self._pending.get(self._rpc_key(rpc_id))
        if pending is None:
            return
        with pending.lock:
            if pending.state != "pending":
                return
            if isinstance(reason, str) and reason:
                pending.cancel_reason = reason
            pending.state = "cancelled"
            pending.cancelled.set()
        self._write_cancellation(pending)

    @staticmethod
    def _claim_terminal(pending: PendingCall) -> bool:
        """Atomically resolve a pending call unless client cancellation won."""

        with pending.lock:
            if pending.state != "pending":
                return False
            pending.state = "terminal"
            return True

    def _expire_request(self, pending: PendingCall) -> dict[str, Any]:
        reason = "rework response wait deadline exceeded"
        with pending.lock:
            if pending.state == "pending":
                pending.cancel_reason = reason
                pending.state = "cancelled"
                pending.cancelled.set()
            else:
                reason = pending.cancel_reason
        self._write_cancellation(pending)
        return self._text_result(reason, is_error=True)

    def _response_text(self, path: Path, request_id: str) -> tuple[str, str | None]:
        document = _read_json_file(path, label="rework response")
        if isinstance(document, str):
            return document, None
        if not isinstance(document, dict):
            raise OperationalError("rework response must be a JSON object or string")
        response_id = document.get("request_id")
        if response_id is not None and response_id != request_id:
            raise OperationalError("rework response request_id does not match")
        status_value = document.get("status", "completed")
        text = ""
        for key in ("text", "response", "result"):
            value = document.get(key)
            if isinstance(value, str):
                text = value
                break
        if status_value in {"error", "failed", "cancelled", "session_closed", "closed"}:
            error = document.get("error", document.get("reason", status_value))
            return text, str(error)
        if text or any(isinstance(document.get(key), str)
                       for key in ("text", "response", "result")):
            return text, None
        raise OperationalError("rework response has no text")

    def _request_rework(self, pending: PendingCall, arguments: Any) -> dict[str, Any]:
        if not isinstance(arguments, dict):
            return self._text_result("request_rework arguments must be an object", is_error=True)
        target_agent = arguments.get("target_agent")
        instructions = arguments.get("instructions")
        if not isinstance(target_agent, str) or self.session.target(target_agent) is None:
            return self._text_result("unknown rework target", is_error=True)
        if not isinstance(instructions, str) or not instructions.strip():
            return self._text_result("rework instructions must be non-empty prose", is_error=True)

        closed = self.session.is_closed()
        if closed is not None:
            return self._text_result(closed, is_error=True)
        started = time.time()
        if started >= self.session.deadline_epoch:
            return self._text_result("rework session deadline exceeded", is_error=True)

        # Reserve time for the reviewer to consume the error and settle its
        # assignment. Short sessions reserve only a fraction of their budget.
        remaining = self.session.deadline_epoch - started
        settlement_room = min(SETTLEMENT_ROOM_SECONDS, remaining / 10)
        response_deadline = self.session.deadline_epoch - settlement_room
        if self.session.workflow_version < 26:
            response_deadline = min(started + MAX_RESPONSE_WAIT_SECONDS,
                                    response_deadline)

        if self._stop.is_set():
            return self._text_result("rework MCP session ended", is_error=True)
        request_id = str(uuid4())
        request = {
            "request_id": request_id,
            "run_id": self.session.run_id,
            "reviewer_execution_id": self.session.reviewer_execution_id,
            "target_agent": target_agent,
            "instructions": instructions,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "response_deadline_epoch": response_deadline,
        }
        # Publish the request before cancellation can publish its companion
        # marker, so the host never observes a marker without its request.
        with pending.lock:
            pending.request_id = request_id
            pending.response_deadline_epoch = response_deadline
            atomic_json(self.session.requests_dir / f"{request_id}.json", request)
        if pending.cancelled.is_set():
            self._write_cancellation(pending)

        response_path = self.session.responses_dir / f"{request_id}.json"
        while not self._stop.is_set():
            if pending.cancelled.is_set():
                self._write_cancellation(pending)
                return self._text_result(pending.cancel_reason, is_error=True)
            if time.time() >= response_deadline:
                return self._expire_request(pending)
            if response_path.exists() or response_path.is_symlink():
                try:
                    response, response_error = self._response_text(response_path, request_id)
                except (SessionError, OperationalError) as exc:
                    if not self._claim_terminal(pending):
                        self._write_cancellation(pending)
                        return self._text_result(pending.cancel_reason, is_error=True)
                    return self._text_result(str(exc), is_error=True)
                if not self._claim_terminal(pending):
                    self._write_cancellation(pending)
                    return self._text_result(pending.cancel_reason, is_error=True)
                if response_error is not None:
                    return self._failed_response_result(response, response_error)
                return self._text_result(response)
            try:
                closed = self.session.is_closed()
            except SessionError as exc:
                return self._text_result(str(exc), is_error=True)
            if closed is not None:
                if not self._claim_terminal(pending):
                    self._write_cancellation(pending)
                    return self._text_result(pending.cancel_reason, is_error=True)
                return self._text_result(closed, is_error=True)
            self._stop.wait(min(self.poll_interval, max(0.0, response_deadline - time.time())))
        return self._text_result("rework MCP session ended", is_error=True)

    def _run_tool_call(self, rpc_id: Any, pending: PendingCall, name: Any,
                       arguments: Any) -> None:
        try:
            if name == "request_rework":
                result = self._request_rework(pending, arguments)
            else:
                result = self._text_result(f"unknown tool: {name}", is_error=True)
            if not self._stop.is_set():
                self._reply_result(rpc_id, result)
        except Exception as exc:  # Keep malformed host artifacts from killing stdio.
            if not self._stop.is_set():
                self._reply_result(rpc_id, self._text_result(str(exc), is_error=True))
        finally:
            with self._pending_lock:
                key = self._rpc_key(rpc_id)
                if self._pending.get(key) is pending:
                    self._pending.pop(key, None)
            pending.finished.set()

    def _drain_pending(self) -> None:
        """Let published calls reach a response or their bounded wait deadline.

        Codex may close the MCP server's stdin while a tool call is still in
        flight.  Treating that EOF as cancellation lets the reviewer finish
        while the requested author is still running.  Keep the stdio process
        alive for the already-published calls instead; this also keeps one
        request identity, so a late host result is reconciled without a retry.
        """

        if not self.session.drain_pending_on_eof:
            return
        with self._pending_lock:
            pending_calls = list(self._pending.values())
        if not pending_calls:
            return

        fallback_deadline = self.session.deadline_epoch
        if self.session.workflow_version < 26:
            fallback_deadline = min(fallback_deadline,
                                    time.time() + MAX_RESPONSE_WAIT_SECONDS)
        latest_deadline = max((pending.response_deadline_epoch or fallback_deadline)
                              for pending in pending_calls)
        remaining = max(0.0, latest_deadline - time.time())
        shutdown_deadline = time.monotonic() + remaining + self.poll_interval * 2
        for pending in pending_calls:
            wait = shutdown_deadline - time.monotonic()
            if wait <= 0 or not pending.finished.wait(wait):
                break

        # This is only a local process teardown fallback after the bounded
        # wait deadline. The worker publishes the host cancellation marker.
        with self._pending_lock:
            unfinished = list(self._pending.values())
        if unfinished:
            self._stop.set()
            for pending in unfinished:
                pending.cancelled.set()

    def _dispatch(self, message: Any) -> None:
        if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
            self._reply_error(None, -32600, "Invalid Request")
            return
        method = message.get("method")
        rpc_id = message.get("id")
        has_id = "id" in message
        params = message.get("params", {})

        if method == "notifications/initialized":
            return
        if method == "notifications/cancelled":
            if isinstance(params, dict) and "requestId" in params:
                self._cancel(params["requestId"], params.get("reason"))
            return
        if not has_id:
            return
        if method == "initialize":
            requested = params.get("protocolVersion") if isinstance(params, dict) else None
            protocol = requested if requested in SUPPORTED_PROTOCOL_VERSIONS else PROTOCOL_VERSION
            self._reply_result(rpc_id, {
                "protocolVersion": protocol,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
            })
            return
        if method == "ping":
            self._reply_result(rpc_id, {})
            return
        if method == "tools/list":
            self._reply_result(rpc_id, {"tools": self._tools()})
            return
        if method != "tools/call":
            self._reply_error(rpc_id, -32601, "Method not found")
            return
        if not isinstance(params, dict):
            self._reply_error(rpc_id, -32602, "Invalid params")
            return
        name = params.get("name")
        arguments = params.get("arguments", {})
        if name == "list_rework_targets":
            targets = [target.to_dict() for target in self.session.targets]
            context = self.session.repair_context
            latest = self.session.directory / 'latest-context.json'
            if context and latest.is_file():
                try:
                    refreshed = _read_json_file(latest, label='latest repair context')
                    recommendations = {row['target_agent']: row for row in refreshed.get('targets', [])}
                    for target in targets:
                        row = recommendations.get(target['target_agent'], {})
                        target.update({key: row[key] for key in
                            ('recommended_issue_ids', 'recommended_issue_count') if key in row})
                    context = refreshed.get('repair_context', context)
                except (SessionError, ValueError, KeyError, TypeError):
                    # Original targets remain callable even if diagnostics are unavailable.
                    pass
            body = {"targets": targets, "repair_context": context} if context else targets
            text = json.dumps(body, ensure_ascii=False, separators=(",", ":"))
            self._reply_result(rpc_id, self._text_result(
                text, structured={"targets": targets, **({'repair_context': context} if context else {})}))
            return

        pending = PendingCall(rpc_id)
        with self._pending_lock:
            key = self._rpc_key(rpc_id)
            if key in self._pending:
                self._reply_error(rpc_id, -32600, "request id already in flight")
                return
            self._pending[key] = pending
        worker = threading.Thread(
            target=self._run_tool_call,
            args=(rpc_id, pending, name, arguments),
            name=f"rework-mcp-{rpc_id}",
            daemon=True,
        )
        worker.start()

    def serve(self) -> None:
        try:
            for line in self.input_stream:
                if not line.strip():
                    continue
                try:
                    message = json.loads(line)
                except json.JSONDecodeError:
                    self._reply_error(None, -32700, "Parse error")
                    continue
                self._dispatch(message)
        finally:
            self._drain_pending()
            self._stop.set()
            with self._pending_lock:
                pending_calls = list(self._pending.values())
            for pending in pending_calls:
                pending.cancelled.set()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Serve a bounded ModPort rework MCP session")
    parser.add_argument("--session", required=True, type=Path,
                        help="absolute path to the rework session JSON descriptor")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        session = Session.load(args.session)
    except SessionError as exc:
        print(f"rework MCP session error: {exc}", file=sys.stderr)
        return 2
    ReworkServer(session).serve()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
