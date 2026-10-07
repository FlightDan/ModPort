"""Tool-free prompt summaries through the managed OpenCode HTTP server."""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import copy_context
from dataclasses import replace
import hashlib
import json
import math
from pathlib import Path
import queue
import secrets
import tempfile
import threading
import time
from typing import Any, Iterator, Mapping

from .opencode_runtime import OpenCodeCleanupError, OpenCodeEventStop


MAX_OUTPUT_BYTES = 4 * 1024 * 1024
SUMMARY_AGENT = "modport-summary"


class SummaryTransportError(RuntimeError):
    """Sanitized OpenCode summary failure; request and provider content stay private."""

    def __init__(self, message: str, *, code: str = "transport_failed"):
        super().__init__(message)
        self.code = code


def _positive_timeout(value: Any, label: str) -> float:
    if (isinstance(value, bool) or not isinstance(value, (int, float))
            or not math.isfinite(value) or value <= 0):
        raise SummaryTransportError(f"summary {label} timeout must be positive and finite", code="invalid_request")
    return float(value)


def _usage(info: Mapping[str, Any]) -> dict[str, int]:
    value = info.get("tokens", info.get("usage", {}))
    if not isinstance(value, Mapping):
        return {}
    names = {
        "input": "input_tokens", "output": "output_tokens", "reasoning": "reasoning_tokens",
        "total": "total_tokens", "input_tokens": "input_tokens", "output_tokens": "output_tokens",
        "total_tokens": "total_tokens",
    }
    result = {}
    for key, target in names.items():
        number = value.get(key)
        if isinstance(number, int) and not isinstance(number, bool) and 0 <= number <= 2**63 - 1:
            result[target] = number
    return result


def _message_model(info: Mapping[str, Any]) -> tuple[str | None, str | None, str | None]:
    model = info.get("model")
    model = model if isinstance(model, Mapping) else {}
    provider_id = info.get("providerID", model.get("providerID"))
    model_id = info.get("modelID", model.get("modelID", model.get("id")))
    variant = info.get("variant", model.get("variant"))
    return (provider_id if isinstance(provider_id, str) else None,
            model_id if isinstance(model_id, str) else None,
            variant if isinstance(variant, str) else None)


class OpenCodeSummaryScope:
    """One isolated project and managed server reused across one compression."""

    def __init__(self, *, command: Any, root: Path, worktree: Path):
        self.command = command
        self.root = Path(root)
        self.worktree = Path(worktree)
        self._temporary: tempfile.TemporaryDirectory[str] | None = None
        self.cwd: Path | None = None
        self.server = None
        self._catalog: Mapping[str, Any] | None = None
        self._catalog_deadline: float | None = None
        self._catalog_queries: list[dict[str, Any]] = []
        self._catalog_retries: set[str] = set()
        self._event_lock = threading.Lock()
        self._active_session: str | None = None
        self._active_signals: queue.Queue | None = None
        self._events_ready = threading.Event()
        self._events_stop = OpenCodeEventStop()
        self._events_thread: threading.Thread | None = None

    def __enter__(self) -> "OpenCodeSummaryScope":
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.close()

    def _ensure_server(self, *, deadline: float | None = None):
        if self.server is not None:
            return self.server
        if deadline is None:
            from .prompt_compressor import _summary_timeout
            remaining = _summary_timeout(self.command)
            deadline = time.monotonic() + (remaining if remaining is not None else 30.0)
        try:
            from .opencode_runtime import OpenCodeConfig, OpenCodeServer
            from .opencode_provider import managed_provider_config
        except ImportError:
            raise SummaryTransportError("OpenCode runtime is unavailable", code="opencode_unavailable") from None
        temporary = tempfile.TemporaryDirectory(prefix="modport-opencode-summary-")
        self._temporary = temporary
        base = Path(temporary.name)
        self.cwd = base / "project"
        self.cwd.mkdir()
        summary_permission = {"*": "deny"}
        config = OpenCodeConfig(
            mcp={}, permission=summary_permission,
            provider=managed_provider_config(),
            agent={SUMMARY_AGENT: {
                "description": "Summarize supplied historical text without tools",
                "mode": "primary", "permission": summary_permission,
            }},
            default_agent=SUMMARY_AGENT,
            extra={"plugin": [], "instructions": []},
        )
        try:
            self.server = OpenCodeServer.start(
                cwd=self.cwd, config=config, xdg_root=base / "xdg", deadline=deadline)
            from .token_budget import bind_token_budget
            bind_token_budget(self.server, self.root)
        except OpenCodeCleanupError as exc:
            try:
                self._record_cleanup_failure(exc.cleanup_diagnostic,
                                             event_reader_stopped=True)
            finally:
                self._cleanup_temp()
            raise SummaryTransportError("OpenCode summary cleanup is unconfirmed",
                                        code="cleanup_unconfirmed") from None
        except TimeoutError:
            self._cleanup_temp()
            raise SummaryTransportError("OpenCode summary timed out", code="timeout") from None
        except Exception:
            self._cleanup_temp()
            raise SummaryTransportError("OpenCode server failed to start", code="opencode_unavailable") from None
        return self.server

    def _resolved_model(self, model: str) -> str:
        try:
            from .opencode_runtime import resolve_model_id
            return resolve_model_id(model, provider_id="openai")
        except SummaryTransportError:
            raise
        except Exception:
            raise SummaryTransportError("OpenCode model could not be resolved", code="model_unavailable") from None

    @staticmethod
    def _catalog_identity(value: Mapping[str, Any]) -> tuple[str, int, int]:
        """Hash only public model identifiers and context/output limits."""
        providers = value.get("all")
        safe_rows = []
        provider_count = 0
        model_count = 0
        if isinstance(providers, list):
            for provider in providers:
                if not isinstance(provider, Mapping):
                    continue
                provider_id = provider.get("id")
                models = provider.get("models")
                if not isinstance(provider_id, str) or not isinstance(models, Mapping):
                    continue
                provider_count += 1
                for model_id, model in models.items():
                    if not isinstance(model_id, str) or not isinstance(model, Mapping):
                        continue
                    model_count += 1
                    limits = model.get("limit")
                    limits = limits if isinstance(limits, Mapping) else {}
                    row = {"model": f"{provider_id}/{model_id}"}
                    for key, value_key in (("context", "context"), ("output", "output")):
                        number = limits.get(value_key)
                        if type(number) is int and number > 0:
                            row[key] = number
                    safe_rows.append(row)
        safe_rows.sort(key=lambda row: row["model"])
        payload = json.dumps(safe_rows, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return hashlib.sha256(payload).hexdigest(), provider_count, model_count

    def _record_catalog_query(self, value: Mapping[str, Any] | None, *,
                              target_model: str, attempt: int, status: str) -> None:
        query: dict[str, Any] = {
            "attempt": attempt,
            "target_model": target_model,
            "status": status,
        }
        if value is not None:
            digest, provider_count, model_count = self._catalog_identity(value)
            target_provider = target_model.split("/", 1)[0]
            providers = value.get("all")
            providers = providers if isinstance(providers, list) else []
            target_rows: list[dict[str, Any]] = []
            target_provider_present = False
            for provider in providers:
                if not isinstance(provider, Mapping) or provider.get("id") != target_provider:
                    continue
                target_provider_present = True
                models = provider.get("models")
                if not isinstance(models, Mapping):
                    continue
                for model_id, model in models.items():
                    if not isinstance(model_id, str) or not isinstance(model, Mapping):
                        continue
                    limits = model.get("limit")
                    limits = limits if isinstance(limits, Mapping) else {}
                    row: dict[str, Any] = {"model": model_id}
                    for key in ("context", "output"):
                        number = limits.get(key)
                        if type(number) is int and number > 0:
                            row[key] = number
                    target_rows.append(row)
            target_rows.sort(key=lambda row: row["model"])
            target_payload = json.dumps(target_rows, sort_keys=True, separators=(",", ":")).encode("utf-8")
            from .prompt_compressor import _catalog_entries
            target_present = any(
                entry.get("slug") == target_model or entry.get("id") == target_model
                or entry.get("model") == target_model
                for entry in _catalog_entries(value)
            )
            server_version = getattr(self.server, "version", None)
            executable_sha256 = getattr(self.server, "executable_sha256", None)
            query.update({
                "target_present": target_present,
                "catalog_sha256": digest,
                "provider_count": provider_count,
                "model_count": model_count,
                "target_provider_present": target_provider_present,
                "target_provider_model_count": len(target_rows),
                "target_provider_catalog_sha256": hashlib.sha256(target_payload).hexdigest(),
                "catalog_identity": {
                    "source": "OpenCode GET /provider",
                    "server_version": server_version if isinstance(server_version, str) else None,
                    "server_executable_sha256": (executable_sha256 if isinstance(executable_sha256, str)
                                                   and len(executable_sha256) == 64
                                                   and all(c in "0123456789abcdef" for c in executable_sha256)
                                                   else None),
                },
            })
        self._catalog_queries.append(query)

    def model_catalog_diagnostics(self) -> dict[str, Any]:
        """Return bounded metadata only; raw provider payloads are never persisted."""
        return {"schema_version": 1, "queries": [dict(query) for query in self._catalog_queries]}

    def _providers(self, *, target_model: str, refresh: bool = False) -> Mapping[str, Any]:
        if self._catalog is not None and not refresh:
            return self._catalog
        server = self._ensure_server()
        if self._catalog_deadline is None:
            from .prompt_compressor import _summary_timeout
            remaining = _summary_timeout(self.command)
            self._catalog_deadline = time.monotonic() + (remaining if remaining is not None else 30.0)
        attempt = len(self._catalog_queries) + 1
        try:
            value = server.providers(cwd=self.cwd, deadline=self._catalog_deadline)
        except TimeoutError:
            self._record_catalog_query(None, target_model=target_model, attempt=attempt, status="timeout")
            raise SummaryTransportError("OpenCode provider catalog timed out", code="timeout") from None
        except Exception:
            self._record_catalog_query(None, target_model=target_model, attempt=attempt, status="unavailable")
            raise SummaryTransportError("OpenCode provider catalog is unavailable", code="model_catalog_unavailable") from None
        if not isinstance(value, Mapping):
            self._record_catalog_query(None, target_model=target_model, attempt=attempt, status="invalid")
            raise SummaryTransportError("OpenCode provider catalog is invalid", code="model_catalog_unavailable")
        self._catalog = value
        self._record_catalog_query(value, target_model=target_model, attempt=attempt, status="ok")
        return value

    def model_profile(self, model: str):
        from .prompt_compressor import PromptCompressionError, _catalog_entries, load_model_profile

        resolved = self._resolved_model(model)
        try:
            catalog = self._providers(target_model=resolved)
        except SummaryTransportError as exc:
            raise PromptCompressionError(
                f"OpenCode provider catalog is unavailable for {resolved!r} ({exc.code})"
            ) from None
        queries = 1
        while True:
            try:
                profile = load_model_profile(resolved, catalog=catalog)
                break
            except PromptCompressionError:
                # Metadata for a present model is authoritative: fail closed
                # rather than looking for a more convenient profile.
                present = any(
                    entry.get("slug") == resolved or entry.get("id") == resolved
                    or entry.get("model") == resolved
                    for entry in _catalog_entries(catalog)
                )
                if present or resolved in self._catalog_retries:
                    raise
                if queries >= 3:
                    self._catalog_retries.add(resolved)
                    raise PromptCompressionError(
                        f"model {resolved!r} is absent from the OpenCode provider catalog after {queries} queries"
                    ) from None
                delay = 0.5 if queries == 1 else 1.0
                if self._catalog_deadline is not None and time.monotonic() + delay >= self._catalog_deadline:
                    self._catalog_retries.add(resolved)
                    raise PromptCompressionError(
                        f"model {resolved!r} is absent from the OpenCode provider catalog after {queries} queries"
                    ) from None
                time.sleep(delay)
                try:
                    catalog = self._providers(target_model=resolved, refresh=True)
                except SummaryTransportError as exc:
                    self._catalog_retries.add(resolved)
                    raise PromptCompressionError(
                        f"model {resolved!r} was absent from the OpenCode provider catalog; "
                        f"requery failed ({exc.code})"
                    ) from None
                queries += 1
        return replace(profile, model=resolved, source=f"OpenCode provider catalog: {resolved}")

    def _start_event_reader(self) -> None:
        if self._events_thread is not None:
            return
        server = self._ensure_server()

        def read_events() -> None:
            while not self._events_stop.is_set():
                try:
                    end = time.monotonic() + 86400.0
                    for event in server.events(cwd=self.cwd, deadline=end,
                                               stop_event=self._events_stop):
                        if self._events_stop.is_set():
                            return
                        self._events_ready.set()
                        if not isinstance(event, Mapping):
                            continue
                        with self._event_lock:
                            session_id = self._active_session
                            signals = self._active_signals
                        if session_id and signals and self._event_session(event) == session_id:
                            try:
                                signals.put_nowait(("event", event, time.monotonic()))
                            except queue.Full:
                                continue
                except Exception:
                    if not self._events_stop.is_set():
                        time.sleep(0.1)

        captured = copy_context()
        self._events_thread = threading.Thread(
            target=captured.run, args=(read_events,), name="modport-opencode-summary-events", daemon=True)
        self._events_thread.start()

    @staticmethod
    def _event_session(event: Mapping[str, Any]) -> str | None:
        properties = event.get("properties")
        if not isinstance(properties, Mapping):
            return None
        session_id = properties.get("sessionID")
        if not isinstance(session_id, str):
            part = properties.get("part")
            session_id = part.get("sessionID") if isinstance(part, Mapping) else None
        return session_id if isinstance(session_id, str) else None

    def summarize(self, request: Any, *, log_path: Path) -> str:
        timeout = getattr(request, "timeout", None)
        if timeout is not None:
            timeout = _positive_timeout(timeout, "total")
        idle_value = getattr(request, "idle_timeout", None)
        idle_timeout = _positive_timeout(60.0 if idle_value is None else idle_value, "idle")
        target = getattr(request, "target_tokens", None)
        if isinstance(target, bool) or not isinstance(target, int) or target <= 0:
            raise SummaryTransportError("summary target must be a positive integer", code="invalid_request")
        output_limit = getattr(request, "output_byte_limit", None)
        if output_limit is None:
            output_limit = target
        if isinstance(output_limit, bool) or not isinstance(output_limit, int) or output_limit <= 0:
            raise SummaryTransportError("summary output byte limit must be positive", code="invalid_request")
        output_limit = min(MAX_OUTPUT_BYTES, output_limit)
        prompt = request.prompt
        model = self._resolved_model(request.model)
        provider_id, model_id = model.split("/", 1)
        started = time.monotonic()
        deadline = None if timeout is None else started + timeout
        audit = {
            "transport": "opencode_http", "model": model,
            "reasoning_variant": request.reasoning_effort, "agent": SUMMARY_AGENT,
            "tools_enabled": 0, "tools_disabled_count": None,
            "prompt_bytes": len(prompt.encode("utf-8")),
            "prompt_sha256": hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
            "target_tokens": target, "output_byte_ceiling": output_limit,
            "output_bound_kind": "host_enforced_utf8_bytes",
            "server_output_token_cap": False,
            "total_timeout_seconds": timeout, "idle_timeout_seconds": idle_timeout,
            "time_to_first_server_event_seconds": None, "time_to_first_text_seconds": None,
            "output_bytes": 0, "usage": {}, "error_code": None, "status": "failed",
        }
        session_id = None
        sender = None
        try:
            server = self._ensure_server(deadline=deadline)
            audit["opencode_version"] = getattr(server, "version", "unknown")
            executable_digest = getattr(server, "executable_sha256", None)
            if isinstance(executable_digest, str):
                audit["opencode_executable_sha256"] = executable_digest
            try:
                try:
                    server.require_model(cwd=self.cwd, model=model,
                                         variant=request.reasoning_effort, deadline=deadline)
                except TimeoutError:
                    raise SummaryTransportError("OpenCode summary model check timed out",
                                                code="timeout") from None
                except Exception:
                    raise SummaryTransportError(
                        "OpenCode provider, model, or reasoning variant is unavailable",
                        code="model_unavailable") from None
                tool_ids = server.tool_ids(cwd=self.cwd, deadline=deadline)
                tools = {tool_id: False for tool_id in tool_ids if isinstance(tool_id, str)}
                audit["tools_disabled_count"] = len(tools)
                self._start_event_reader()
                readiness_timeout = 5.0 if deadline is None else min(
                    5.0, max(0.0, deadline - time.monotonic()))
                if readiness_timeout <= 0:
                    raise SummaryTransportError("OpenCode summary timed out", code="timeout")
                if not self._events_ready.wait(timeout=readiness_timeout):
                    if deadline is not None and time.monotonic() >= deadline:
                        raise SummaryTransportError("OpenCode summary timed out", code="timeout")
                    raise SummaryTransportError(
                        "OpenCode event stream did not become ready", code="event_stream_unavailable")
                if deadline is not None and time.monotonic() >= deadline:
                    raise SummaryTransportError("OpenCode summary timed out", code="timeout")
                session = server.create_session(
                    cwd=self.cwd, title="ModPort historical summary", model=model,
                    variant=request.reasoning_effort, deadline=deadline)
                session_id = session.get("id") if isinstance(session, Mapping) else None
                if not isinstance(session_id, str) or not session_id:
                    raise SummaryTransportError("OpenCode created no summary session", code="invalid_session")
                signals: queue.Queue = queue.Queue(maxsize=4096)
                with self._event_lock:
                    self._active_session = session_id
                    self._active_signals = signals
                message_id = "msg_" + secrets.token_hex(16)

                def abort_active_session() -> None:
                    try:
                        audit["abort_succeeded"] = (
                            server.abort_session(session_id, cwd=self.cwd,
                                                 deadline=time.monotonic() + 2.0) is True)
                    except Exception:
                        audit["abort_succeeded"] = False

                def send() -> None:
                    try:
                        response_value = server.send_message(
                            session_id, prompt, cwd=self.cwd, model=model,
                            variant=request.reasoning_effort, agent=SUMMARY_AGENT,
                            tools=tools, deadline=deadline or time.monotonic() + 86400.0,
                            message_id=message_id)
                        signals.put_nowait(("response", response_value, time.monotonic()))
                    except Exception as exc:
                        try:
                            signals.put_nowait(("error", exc, time.monotonic()))
                        except queue.Full:
                            pass

                captured = copy_context()
                sender = threading.Thread(
                    target=captured.run, args=(send,), name="modport-opencode-summary-request", daemon=True)
                sender.start()
                idle_deadline = time.monotonic() + idle_timeout
                response = None
                assistant_ids: set[str] = set()
                streamed_part_bytes: dict[str, int] = {}
                while response is None:
                    now = time.monotonic()
                    if deadline is not None and now >= deadline:
                        abort_active_session()
                        raise SummaryTransportError("OpenCode summary timed out", code="timeout")
                    if now >= idle_deadline:
                        abort_active_session()
                        raise SummaryTransportError(
                            "OpenCode summary stream idle timed out", code="stream_idle")
                    wait_until = min(idle_deadline, deadline) if deadline is not None else idle_deadline
                    try:
                        response_kind, response_value, event_time = signals.get(
                            timeout=max(0.001, wait_until - now))
                    except queue.Empty:
                        continue
                    if response_kind == "error":
                        from .token_budget import TokenBudgetExceeded
                        if isinstance(response_value, TokenBudgetExceeded):
                            raise SummaryTransportError(str(response_value), code=response_value.code) from response_value
                        if isinstance(response_value, BaseException):
                            audit["failure_type"] = type(response_value).__name__
                            status = getattr(response_value, "status", None)
                            provider_error = getattr(response_value, "error", None)
                            if isinstance(provider_error, Mapping):
                                name = provider_error.get("name")
                                if isinstance(name, str):
                                    audit["provider_error_name"] = name[:80]
                                data = provider_error.get("data")
                                if isinstance(data, Mapping):
                                    status = data.get("statusCode", data.get("status", status))
                            if isinstance(status, int) and not isinstance(status, bool) and 100 <= status <= 599:
                                audit["http_status"] = status
                            if ((isinstance(status, int) and status in {401, 403})
                                    or isinstance(provider_error, Mapping)
                                    and 'auth' in str(provider_error.get('name', '')).lower()):
                                raise SummaryTransportError(
                                    "OpenCode summary authentication failed", code="auth_failed")
                            if isinstance(response_value, TimeoutError):
                                raise SummaryTransportError(
                                    "OpenCode summary timed out", code="timeout")
                        raise SummaryTransportError(
                            "OpenCode summary request failed", code="provider_request_failed")
                    if response_kind == "response":
                        response = response_value
                        continue
                    event = response_value
                    if not isinstance(event, Mapping):
                        continue
                    event_type = event.get("type")
                    if not isinstance(event_type, str):
                        continue
                    if audit["time_to_first_server_event_seconds"] is None:
                        audit["time_to_first_server_event_seconds"] = event_time - started
                    properties = event.get("properties", {})
                    if not isinstance(properties, Mapping):
                        properties = {}
                    info = properties.get("info", properties.get("message"))
                    if isinstance(info, Mapping):
                        if info.get("role") == "assistant" and isinstance(info.get("id"), str):
                            assistant_ids.add(info["id"])
                    part = properties.get("part")
                    message_id_in_event = properties.get("messageID")
                    if isinstance(part, Mapping):
                        message_id_in_event = part.get("messageID", message_id_in_event)
                        part_type = part.get("type")
                    # This is a fresh, single-request summary session. A
                    # streamed assistant part can arrive before its
                    # message.updated event, so accept any non-user ID in
                    # this session as the current assistant message.
                    assistant_event = (message_id_in_event in assistant_ids or
                                       isinstance(message_id_in_event, str) and
                                       message_id_in_event != message_id)
                    if isinstance(part, Mapping):
                        if assistant_event:
                            if part_type in ("tool", "tool-invocation", "tool-call"):
                                abort_active_session()
                                raise SummaryTransportError(
                                    "OpenCode summary attempted tool use", code="tool_use")
                            if part_type == "text":
                                output_text = part.get("text")
                                if isinstance(output_text, str):
                                    part_id = part.get("id", part.get("partID"))
                                    key = part_id if isinstance(part_id, str) else "_text"
                                    streamed_part_bytes[key] = len(output_text.encode("utf-8"))
                                    output_bytes = sum(streamed_part_bytes.values())
                                    audit["output_bytes"] = max(audit["output_bytes"], output_bytes)
                                    if audit["time_to_first_text_seconds"] is None and output_text:
                                        audit["time_to_first_text_seconds"] = event_time - started
                                    if output_bytes > output_limit:
                                        abort_active_session()
                                        raise SummaryTransportError(
                                            "OpenCode summary exceeded its byte ceiling", code="output_limit")
                    if event_type == "message.part.delta" and assistant_event:
                        part_id = properties.get("partID")
                        field = properties.get("field")
                        delta = properties.get("delta")
                        if isinstance(part_id, str) and field == "text" and isinstance(delta, str):
                            streamed_part_bytes[part_id] = streamed_part_bytes.get(part_id, 0) + len(delta.encode("utf-8"))
                            output_bytes = sum(streamed_part_bytes.values())
                            audit["output_bytes"] = max(audit["output_bytes"], output_bytes)
                            if audit["time_to_first_text_seconds"] is None and delta:
                                audit["time_to_first_text_seconds"] = event_time - started
                            if output_bytes > output_limit:
                                abort_active_session()
                                raise SummaryTransportError(
                                    "OpenCode summary exceeded its byte ceiling", code="output_limit")
                    if (event_type == "session.status"
                            or (event_type.startswith("message.")
                                and (message_id_in_event is None or
                                     message_id_in_event == message_id or
                                     assistant_event))):
                        idle_deadline = event_time + idle_timeout
            except SummaryTransportError:
                raise
            except TimeoutError:
                raise SummaryTransportError("OpenCode summary timed out", code="timeout") from None
            except Exception as exc:
                from .token_budget import TokenBudgetExceeded
                if isinstance(exc, TokenBudgetExceeded):
                    raise SummaryTransportError(str(exc), code=exc.code) from exc
                audit["failure_type"] = type(exc).__name__
                raise SummaryTransportError("OpenCode summary request failed", code="provider_request_failed") from None
            elapsed = time.monotonic() - started
            if deadline is not None and elapsed > timeout:
                raise SummaryTransportError("OpenCode summary timed out", code="timeout")
            info = response.get("info") if isinstance(response, Mapping) else None
            parts = response.get("parts") if isinstance(response, Mapping) else None
            if not isinstance(info, Mapping) or not isinstance(parts, list):
                raise SummaryTransportError("OpenCode returned an invalid summary response", code="invalid_response")
            actual_provider, actual_model, actual_variant = _message_model(info)
            if actual_provider != provider_id or actual_model != model_id:
                raise SummaryTransportError("OpenCode summary model identity changed", code="model_identity_mismatch")
            if actual_variant != request.reasoning_effort:
                raise SummaryTransportError("OpenCode summary variant was not confirmed", code="variant_mismatch")
            if info.get("finish") != "stop":
                raise SummaryTransportError("OpenCode summary did not finish normally", code="incomplete_response")
            text_parts = []
            for part in parts:
                if not isinstance(part, Mapping):
                    raise SummaryTransportError("OpenCode summary contained an invalid part", code="invalid_response")
                kind = part.get("type")
                if kind == "text":
                    value = part.get("text")
                    if not isinstance(value, str):
                        raise SummaryTransportError("OpenCode summary text was invalid", code="invalid_response")
                    text_parts.append(value)
                elif kind in ("reasoning", "step-start", "step-finish"):
                    continue
                elif kind in ("tool", "tool-invocation", "tool-call"):
                    raise SummaryTransportError("OpenCode summary attempted tool use", code="tool_use")
                else:
                    raise SummaryTransportError("OpenCode summary contained unsupported output", code="unsupported_output")
            result = "".join(text_parts).strip()
            result_bytes = len(result.encode("utf-8"))
            if not result:
                raise SummaryTransportError("OpenCode returned an empty summary", code="empty_response")
            if result_bytes > output_limit:
                raise SummaryTransportError("OpenCode summary exceeded its byte ceiling", code="output_limit")
            audit.update(status="completed", output_bytes=result_bytes, usage=_usage(info))
            return result
        except SummaryTransportError as exc:
            audit["error_code"] = exc.code
            raise
        finally:
            audit["elapsed_seconds"] = time.monotonic() - started
            with self._event_lock:
                if self._active_session == session_id:
                    self._active_session = None
                    self._active_signals = None
            if sender is not None and sender.is_alive():
                if session_id and self.server is not None:
                    abort_active_session()
                sender.join(timeout=0.25)
            if session_id and self.server is not None:
                if sender is None or not sender.is_alive():
                    try:
                        audit["session_deleted"] = self.server.delete_session(
                            session_id, cwd=self.cwd,
                            deadline=time.monotonic() + 2.0) is True
                    except Exception:
                        audit["session_deleted"] = False
                else:
                    audit["session_deleted"] = False
                    audit["request_thread_stopped"] = False
            path = Path(log_path)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(audit, sort_keys=True) + "\n", encoding="utf-8")

    def close(self) -> None:
        self._events_stop.set()
        server = self.server
        diagnostic = None
        if server is not None:
            try:
                diagnostic = server.close(cleanup_reason="summary_scope_closed")
            except Exception as exc:
                diagnostic = {"classification": "unknown", "cleanup_confirmed": False,
                              "error_type": type(exc).__name__}
        confirmed = (server is None or isinstance(diagnostic, Mapping)
                     and diagnostic.get("cleanup_confirmed",
                                        diagnostic.get("returncode") is not None) is True)
        reader_stopped = True
        if self._events_thread is not None:
            self._events_thread.join(timeout=1)
            reader_stopped = not self._events_thread.is_alive()
            if reader_stopped:
                self._events_thread = None
        if not confirmed or not reader_stopped:
            # The XDG root contains provider configuration. Keep only a small
            # allowlisted process result in the Run, then remove that root.
            try:
                self._record_cleanup_failure(diagnostic, event_reader_stopped=reader_stopped)
            finally:
                self._cleanup_temp()
            raise SummaryTransportError("OpenCode summary cleanup is unconfirmed",
                                        code="cleanup_unconfirmed")
        self.server = None
        self._cleanup_temp()

    def _record_cleanup_failure(self, diagnostic: Mapping[str, Any] | None, *,
                                event_reader_stopped: bool) -> None:
        from .evidence import atomic_json
        record = {"schema_version": 1, "run_id": getattr(self.command, "run_id", None),
                  "task_id": getattr(self.command, "task_id", None),
                  "stage_id": getattr(self.command, "stage_id", None),
                  "cleanup_confirmed": False,
                  "event_reader_stopped": event_reader_stopped}
        if isinstance(diagnostic, Mapping):
            record["server"] = {key: diagnostic.get(key) for key in (
                "classification", "returncode", "target_pid", "target_birth",
                "error_type", "group_exit_wait_seconds") if key in diagnostic}
        directory = self.root / "logs"
        directory.mkdir(parents=True, exist_ok=True)
        atomic_json(directory / ("summary-cleanup-" + secrets.token_hex(8) + ".json"),
                    record)

    def _cleanup_temp(self) -> None:
        temporary = self._temporary
        self._temporary = None
        if temporary is not None:
            temporary.cleanup()


@contextmanager
def compression_scope(*, command: Any, root: Path, worktree: Path) -> Iterator[OpenCodeSummaryScope]:
    scope = OpenCodeSummaryScope(command=command, root=root, worktree=worktree)
    try:
        yield scope
    finally:
        scope.close()


def summarize(request: Any, *, command: Any, root: Path, worktree: Path, log_path: Path) -> str:
    """Run one isolated OpenCode summary when no compression scope is active."""
    with compression_scope(command=command, root=root, worktree=worktree) as scope:
        return scope.summarize(request, log_path=log_path)
