"""Best-effort, append-only run audit; never accesses the dispatcher's database.

Public output is redacted before persistence. Token counts are provider observations,
not estimates: null means unavailable; cached input is a subset of input.
"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
import csv
import errno
from . import platform_files as fcntl
import hashlib
import json
from .platform_files import file_os as os
from pathlib import Path
import re
import selectors
import signal
import shutil
import sqlite3
import stat
import subprocess
import sys
import time
import tempfile
import threading
from typing import Any, Mapping, Sequence
import uuid
import warnings

from .audit_storage import load_data

_CONTEXT: ContextVar[dict] = ContextVar("modport_audit", default={})


def _sdk_activity_call(method, *args, **kwargs):
    """Capture facts through the public SDK; observation never decides execution."""
    from .execution_budget import current_sdk_context
    context = current_sdk_context()
    if context is None:
        return None
    try:
        return getattr(context.activity, method)(*args, **kwargs)
    except Exception as exc:
        try:
            warnings.warn("SDK activity capture unavailable: " + type(exc).__name__,
                          RuntimeWarning)
        except Exception:
            pass
        return None


def report_sdk_bytes(stream: str, chunk: bytes):
    """Report actual received bytes without retaining model or tool contents."""
    return _sdk_activity_call("report_bytes", stream, chunk, retain_tail=False)


def report_sdk_byte_count(stream: str, byte_count: int):
    """Report an adapter's measured byte count, without synthetic byte chunks."""
    return _sdk_activity_call("report_byte_count", stream, byte_count)


def report_sdk_model(kind: str = "event"):
    return _sdk_activity_call("model", kind)


def report_sdk_tool(kind: str = "activity"):
    return _sdk_activity_call("tool", kind)


@contextmanager
def sdk_activity_wait(reason: str, *, target=None):
    """Observe an explicit tool wait while preserving exceptions from its body."""
    waiting = _sdk_activity_call("wait", reason, target=target)
    entered = False
    if waiting is not None:
        try:
            waiting.__enter__()
            entered = True
        except Exception:
            pass
    try:
        yield
    finally:
        if entered:
            try:
                waiting.__exit__(None, None, None)
            except Exception:
                pass


class OpenCodeActivity:
    """Count observed OpenCode lifecycle transitions; never report useful progress."""

    def __init__(self):
        self._lock = threading.Lock()
        self._tools = {}
        self._tool_observations = {}
        self._text_sizes = {}
        self._roles = {}
        self._seen_events = {}

    def observe(self, event):
        try:
            with self._lock:
                self._observe(event)
        except Exception as exc:
            try:
                warnings.warn("OpenCode activity capture unavailable: " + type(exc).__name__,
                              RuntimeWarning)
            except Exception:
                pass

    def _observe(self, event):
        if not isinstance(event, Mapping):
            return
        event_id = event.get("id")
        if isinstance(event_id, str):
            if event_id in self._seen_events:
                return
            self._seen_events[event_id] = None
            if len(self._seen_events) > 4096:
                self._seen_events.pop(next(iter(self._seen_events)))
        kind = event.get("type")
        properties = event.get("properties")
        if not isinstance(properties, Mapping):
            return
        info = properties.get("info")
        if kind == "message.updated" and isinstance(info, Mapping):
            session = info.get("sessionID", properties.get("sessionID"))
            message = info.get("id")
            if isinstance(session, str) and isinstance(message, str):
                self._roles[(session, message)] = info.get("role")
                if len(self._roles) > 4096:
                    self._roles.pop(next(iter(self._roles)))
            if info.get("role") == "assistant":
                report_sdk_model("event")
        if kind not in {"message.part.updated", "message.part.delta"}:
            return
        part = properties.get("part")
        part = part if isinstance(part, Mapping) else {}
        session_id = part.get("sessionID", properties.get("sessionID"))
        part_id = part.get("id", properties.get("partID"))
        if not isinstance(part_id, str) or not isinstance(session_id, str):
            return
        key = (session_id, part_id)
        message_id = part.get("messageID", properties.get("messageID"))
        assistant = (isinstance(message_id, str)
                     and self._roles.get((session_id, message_id)) == "assistant")
        if part.get("type") == "tool":
            state = part.get("state")
            status = state.get("status") if isinstance(state, Mapping) else None
            prior = self._tools.get(key)
            if prior is False:
                return
            if status in {"pending", "running", "completed", "error"}:
                output = state.get("output")
                size = len(output.encode("utf-8")) if isinstance(output, str) else 0
                observation = (status, size)
                previous = self._tool_observations.get(key)
                if previous is None or status != previous[0] or size > previous[1]:
                    # A model-emitted tool transition is response activity, not progress.
                    report_sdk_model("tool_activity")
                self._tool_observations[key] = observation
            if status in {"pending", "running"} and prior is None:
                if key not in self._tools:
                    report_sdk_tool("request")
                waiting = sdk_activity_wait("tool_response", target=part_id)
                waiting.__enter__()
                self._tools[key] = waiting
            elif status in {"completed", "error"} and prior is not False:
                # A terminal-only observation establishes a response, not a request.
                report_sdk_tool("response")
                if prior is not None:
                    prior.__exit__(None, None, None)
                self._tools[key] = False
        if kind == "message.part.delta" and assistant:
            delta = properties.get("delta")
            if properties.get("field") == "text" and isinstance(delta, str) and delta:
                report_sdk_model("text")
                self._text_sizes[key] = self._text_sizes.get(key, 0) + len(delta.encode("utf-8"))
        elif assistant and part.get("type") == "text" and isinstance(part.get("text"), str):
            size = len(part["text"].encode("utf-8"))
            if size > self._text_sizes.get(key, 0):
                report_sdk_model("text")
            self._text_sizes[key] = size
        # Only opaque IDs and lengths are retained, with bounded completed history.
        if len(self._text_sizes) > 4096:
            self._text_sizes.pop(next(iter(self._text_sizes)))
        if len(self._tools) > 4096:
            for completed_key, waiting in tuple(self._tools.items()):
                if waiting is False:
                    del self._tools[completed_key]
                    self._tool_observations.pop(completed_key, None)
                    break

    def close(self):
        # End observed wait scopes on stream loss, retaining lifecycle identity
        # so a reconnect cannot invent another request or terminal response.
        with self._lock:
            for key, waiting in self._tools.items():
                if waiting is not False and waiting is not None:
                    waiting.__exit__(None, None, None)
                    self._tools[key] = None

_SECRET_KEY = re.compile(r"(?:api[_-]?key|secret|password|authorization|access[_-]?token|refresh[_-]?token|credential|(?:^|[_-])token(?:$|[_-])|private[_-]?key|cookie)", re.I)
_TOKEN_FIELDS = ("input_tokens", "cached_input_tokens", "output_tokens", "reasoning_output_tokens")
_POST_KILL_DRAIN_SECONDS = 0.25
_HTML_EVENT_LIMIT = 100
_HTML_USAGE_LIMIT = 100
_HTML_METADATA_PREVIEW_CHARS = 160
_HTML_PAYLOAD_PREVIEW_CHARS = 512
_HTML_EVENT_FIELDS = (
    "event_id", "timestamp", "kind", "run_id", "stage_id", "task_id", "command_id",
    "attempt", "invocation_id", "operation_invocation_id", "agent_id", "requested_model",
    "reported_model", "provider", "api", "reasoning_effort", "status",
    "usage_availability",
)
_REPORT_EXTENSIONS = ("json", "csv", "md", "html")
_REPORT_GENERATIONS = ".audit-generations"
_REPORT_CURRENT = ".audit-current"


def _now():
    return datetime.now(timezone.utc).isoformat()


def public_last_message(stdout: str) -> str:
    """Extract public Codex message text before writing the compatibility log."""
    message = None
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except (TypeError, ValueError):
            continue
        if not isinstance(event, dict) or event.get("type") != "item.completed":
            continue
        item = event.get("item")
        if isinstance(item, dict) and item.get("type") == "agent_message" and isinstance(item.get("text"), str):
            message = item["text"]
    return redact(message, _secrets()) if message is not None else "No final agent message was reported; see the invocation log."


def redact(value: Any, secrets: Sequence[str] = ()) -> Any:
    """Remove common credentials and private reasoning without collecting env."""
    if isinstance(value, Mapping):
        if isinstance(value.get("type"), str) and ("reasoning" in value["type"] or value["type"] == "thinking"):
            return {"type": value["type"], "content": "[PRIVATE REASONING OMITTED]"}
        return {str(k): ("[REDACTED]" if _SECRET_KEY.search(str(k)) or str(k) in {"reasoning", "encrypted_content", "thinking"} else redact(v, secrets)) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        result = []
        hide_next = False
        for item in value:
            result.append("[REDACTED]" if hide_next else redact(item, secrets))
            hide_next = isinstance(item, str) and item.startswith("-") and bool(_SECRET_KEY.search(item)) and "=" not in item
        return result
    if not isinstance(value, str):
        return value
    for secret in sorted(set(secrets), key=len, reverse=True):
        if len(secret) >= 4:
            value = value.replace(secret, "[REDACTED]")
    value = re.sub(r"(?i)(https?://)[^\s/@]+:[^\s/@]+@", r"\1[REDACTED]@", value)
    value = re.sub(r"(?i)(\b(?:authorization)\s*[:=]\s*)(?:bearer\s+|basic\s+)?[^\s,;\"']+", r"\1[REDACTED]", value)
    value = re.sub(r"(?i)(\b(?:api[_-]?key|secret|password|access[_-]?token|refresh[_-]?token|token)\s*[=:]\s*)[^\s&;,\"']+", r"\1[REDACTED]", value)
    return re.sub(r"\bsk-[A-Za-z0-9_-]{10,}", "[REDACTED]", value)


def _secrets(env=None):
    return tuple(str(v) for k, v in {**os.environ, **dict(env or {})}.items() if _SECRET_KEY.search(k) and v)


def _connect(root):
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(root / "audit.sqlite3", timeout=30)
    connection.execute("CREATE TABLE IF NOT EXISTS events (event_id TEXT PRIMARY KEY, timestamp TEXT NOT NULL, kind TEXT NOT NULL, data TEXT NOT NULL)")
    return connection


def record_event(root, event_id: str, kind: str, payload=None, *, strict=False, **identity) -> bool:
    """Persist once by stable ID; strict SDK projections fail before cursor ACK."""
    try:
        context = {k: v for k, v in _CONTEXT.get().items() if k != "root"}
        data = redact({**context, **identity, "payload": payload or {}}, _secrets())
        timestamp = data.pop("timestamp", None) or _now()
        if kind == "sdk.event":
            from .audit_storage import store_data
            serialized = store_data(root, data)
        else:
            serialized = json.dumps(data, ensure_ascii=False, default=str)
        with _connect(root) as connection:
            cursor = connection.execute("INSERT OR IGNORE INTO events VALUES (?,?,?,?)",
                (str(event_id), timestamp, str(kind), serialized))
            inserted = cursor.rowcount == 1
        connection.close()
        return inserted
    except Exception as exc:
        if strict:
            raise
        warnings.warn(f"audit write unavailable: {type(exc).__name__}", RuntimeWarning)
        return False


@contextmanager
def operation_context(operation):
    invocation = str(uuid.uuid4())
    options = dict(operation.options)
    identity = {k: getattr(operation, k) for k in ("run_id", "task_id", "stage_id", "command_id", "attempt")}
    identity.update(root=str(operation.run_dir), invocation_id=invocation,
                    agent_id=operation.payload.get("agent_id", operation.stage_id),
                    requested_model=options.get("model"), reported_model=None,
                    reasoning_effort=options.get("reasoning_effort"), provider=options.get("provider"), api=None)
    token = _CONTEXT.set(identity)
    record_event(operation.run_dir, invocation + ":started", "operation.started", status="started")
    try:
        from .workspace import workspace_context
        with workspace_context(operation.run_dir):
            yield identity
    except BaseException as exc:
        record_event(operation.run_dir, invocation + ":finished", "operation.finished", {"error_type": type(exc).__name__}, status="failed" if isinstance(exc, Exception) else "interrupted")
        raise
    else:
        record_event(operation.run_dir, invocation + ":finished", "operation.finished", status="returned")
    finally:
        _CONTEXT.reset(token)


def _usage(event):
    kind = event.get("type", "")
    response = event.get("response") if isinstance(event.get("response"), dict) else event
    usage = response.get("usage")
    if not isinstance(usage, dict) or kind not in {"turn.completed", "response.completed", "response.incomplete", "response.failed"}:
        return None
    def count(value):
        return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None
    inputs = usage.get("input_tokens_details")
    outputs = usage.get("output_tokens_details")
    inputs = inputs if isinstance(inputs, dict) else {}
    outputs = outputs if isinstance(outputs, dict) else {}
    result = {"input_tokens": count(usage.get("input_tokens")),
              "cached_input_tokens": count(usage.get("cached_input_tokens", inputs.get("cached_tokens"))),
              "output_tokens": count(usage.get("output_tokens")),
              "reasoning_output_tokens": count(usage.get("reasoning_output_tokens", outputs.get("reasoning_tokens")))}
    result.update(granularity="turn_aggregate" if kind == "turn.completed" else "api_response",
                  reported_model=response.get("model"), response_id=response.get("id") if kind != "turn.completed" else None,
                  availability="reported" if all(result[k] is not None for k in _TOKEN_FIELDS[:3]) else "partial")
    return result


def run_process(args: Sequence[str], *, cwd: Path, log: Path, timeout=None, env=None, pass_fds=(), combine_output=True, input_text=None):
    """Stream public stdout/stderr to invocation files and a compatible combined log.

    The returned stdout remains unredacted for the caller's parsing. Persisted copies
    are redacted. Timeouts retain usage already observed and re-raise TimeoutExpired.
    """
    if not args or any(not isinstance(item, str) or "\x00" in item for item in args):
        raise ValueError("invalid subprocess argument")
    if input_text is not None and not isinstance(input_text, str):
        raise ValueError('subprocess input must be text')
    context = _CONTEXT.get()
    root = Path(context.get("root", Path(log).parent))
    from .workspace import git_command
    args, env = git_command(args, cwd=cwd, environment={**os.environ, **dict(env or {})},
                            root=context.get('root'))
    invocation = str(uuid.uuid4())
    identity = {"invocation_id": invocation, "operation_invocation_id": context.get("invocation_id")}
    secrets = _secrets(env)
    folder = root / "audit-logs" / invocation
    files = {}
    try:
        folder.mkdir(parents=True, exist_ok=True)
        for name in ("stdout", "stderr", "combined"):
            files[name] = (folder / (name + ".log")).open("w", encoding="utf-8")
        if input_text is not None:
            (folder / 'stdin.log').write_text(redact(input_text, secrets), encoding='utf-8')
    except OSError:
        warnings.warn("audit stream logs unavailable", RuntimeWarning)
    record_event(root, invocation + ":started", "process.started", {"args": redact(list(args), secrets), "cwd": str(cwd), "logs": str(folder), "timeout": timeout}, **identity, status="started")
    chunks = []
    stdout_chunks = []
    errors = []
    pending = {"stdout": b"", "stderr": b""}
    counter = 0
    models = {}
    def line(name, raw):
        nonlocal counter
        decoded = raw.decode("utf-8", errors="replace")
        public = decoded
        if name == "stdout":
            try:
                event = json.loads(decoded)
            except (ValueError, TypeError):
                event = None
            if isinstance(event, dict):
                # Private reasoning item content must never enter the persistent log.
                item = event.get("item", {})
                if isinstance(item, dict) and item.get("type") in {"reasoning", "thinking"}:
                    event = {**event, "item": {"type": item["type"], "content": "[PRIVATE REASONING OMITTED]"}}
                counter += 1
                if isinstance(event.get("model"), str):
                    models["reported_model"] = event["model"]
                public = json.dumps(redact(event, secrets), ensure_ascii=False) + "\n"
                record_event(root, f"{invocation}:stream:{event.get('event_id', counter)}", "model.event", redact(event, secrets), **identity, **models)
                usage = _usage(event)
                if usage is not None:
                    reported_model = usage.pop("reported_model") or models.get("reported_model")
                    record_event(root, f"{invocation}:usage:{usage.get('response_id') or event.get('event_id', counter)}", "usage", usage, **identity, reported_model=reported_model, api="codex_exec" if usage["granularity"] == "turn_aggregate" else "responses")
        public = redact(public, secrets)
        for destination in (name, "combined"):
            if destination in files:
                try:
                    files[destination].write(public)
                    files[destination].flush()
                except OSError:
                    pass
    process = None
    sdk_wait = None
    input_stream = None
    status = "failed"
    drain_incomplete = False
    launch_error = None
    memory_before = None
    birth = None
    started = time.monotonic()
    try:
        if input_text is not None:
            # A temporary file avoids pipe deadlocks when the child emits logs
            # before consuming a large prompt, and never places it in argv.
            input_stream = tempfile.TemporaryFile()
            input_stream.write(input_text.encode('utf-8'))
            input_stream.seek(0)
        try:
            if os.name == 'nt':
                from .platform_runtime import spawn_trusted
                process = spawn_trusted(args, cwd=cwd, environment=env, stdin=input_stream)
            else:
                process = subprocess.Popen(list(args), cwd=cwd, env=env, stdin=input_stream, stdout=subprocess.PIPE, stderr=subprocess.PIPE, pass_fds=pass_fds, start_new_session=True)
        except OSError as exc:
            if exc.errno != errno.E2BIG:
                raise
            # Report the OS decision, without guessing a portable size limit or
            # including environment values, argv contents or the stdin prompt.
            launch_error = ("E2BIG: process arguments or environment exceed OS size limits; "
                            "reduce their size. Stdin prompt content is not part of argv.")
            raise OSError(errno.E2BIG, launch_error) from None
        executable = Path(args[0]).name.lower()
        report_sdk_tool("request")
        _sdk_activity_call("enable_stream", "stdout")
        _sdk_activity_call("enable_stream", "stderr")
        sdk_wait = sdk_activity_wait("tool_response")
        sdk_wait.__enter__()
        if executable.startswith("codex") and "exec" in args[1:]:
            from .execution_progress import mark_current_execution_progress
            mark_current_execution_progress("model_started")
        try:
            from .process_diagnostics import read_process_memory_events
            from .run_monitor import process_birth
            birth = process_birth(process.pid)
            memory_before = read_process_memory_events(process.pid)
        except (OSError, ValueError, TypeError):
            # Diagnostic visibility must not change process execution semantics.
            memory_before = None
        if os.name == 'nt':
            from .platform_runtime import capture_started_process
            def stream_chunk(name, data):
                report_sdk_bytes(name, data)
                chunks.append(data)
                (errors if name == 'stderr' else stdout_chunks).append(data)
                pending[name] += data
                while b'\n' in pending[name]:
                    raw, pending[name] = pending[name].split(b'\n', 1)
                    line(name, raw + b'\n')
            capture = capture_started_process(process, timeout=timeout,
                max_output_bytes=0, on_chunk=stream_chunk)
            timed_out = capture.timed_out
            drain_incomplete = capture.drain_incomplete
            returncode = capture.returncode
        else:
            with selectors.DefaultSelector() as selector:
                for pipe, name in ((process.stdout, "stdout"), (process.stderr, "stderr")):
                    selector.register(pipe, selectors.EVENT_READ, name)
                timed_out = False
                drain_deadline = None
                while selector.get_map():
                    now = time.monotonic()
                    if timeout is not None and now - started >= timeout and not timed_out:
                        timed_out = True
                        drain_deadline = now + _POST_KILL_DRAIN_SECONDS
                        try:
                            os.killpg(process.pid, signal.SIGKILL)
                        except ProcessLookupError:
                            pass
                    if drain_deadline is not None and now >= drain_deadline:
                        # Detached descendants may retain these pipes after the
                        # group dies. SDK containment owns descendant cleanup;
                        # local timeout reporting must not wait for their EOF.
                        drain_incomplete = True
                        break
                    wait = 0.05 if drain_deadline is None else min(0.05, max(0.0, drain_deadline - now))
                    for key, _ in selector.select(wait):
                        data = os.read(key.fileobj.fileno(), 65536)
                        name = key.data
                        if not data:
                            selector.unregister(key.fileobj)
                            key.fileobj.close()
                            if pending[name]:
                                line(name, pending[name])
                                pending[name] = b""
                            continue
                        report_sdk_bytes(name, data)
                        chunks.append(data)
                        if name == "stderr":
                            errors.append(data)
                        else:
                            stdout_chunks.append(data)
                        pending[name] += data
                        while b"\n" in pending[name]:
                            raw, pending[name] = pending[name].split(b"\n", 1)
                            line(name, raw + b"\n")
                remaining = None if timeout is None else max(0.0, timeout - (time.monotonic() - started))
                try:
                    returncode = process.wait(timeout=remaining)
                except subprocess.TimeoutExpired:
                    timed_out = True
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    returncode = process.wait()
        output = b"".join(chunks if combine_output else stdout_chunks).decode("utf-8", errors="replace")
        stderr = b"".join(errors).decode("utf-8", errors="replace")
        if timed_out:
            status = "timeout"
            raise subprocess.TimeoutExpired(args, timeout, output=output, stderr=stderr)
        status = "completed" if returncode == 0 else "failed"
        return subprocess.CompletedProcess(list(args), returncode, output, stderr)
    except BaseException:
        if process is not None and process.poll() is None:
            if os.name == 'nt':
                from .platform_runtime import terminate_tree
                terminate_tree(process)
            else:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.wait()
        if status != "timeout":
            status = "interrupted" if not isinstance(sys.exc_info()[1], Exception) else "failed"
        raise
    finally:
        if sdk_wait is not None:
            sdk_wait.__exit__(None, None, None)
        if process is not None and process.returncode is not None:
            report_sdk_tool("response")
        if input_stream is not None:
            input_stream.close()
        if process is not None:
            for pipe in (process.stdout, process.stderr):
                if pipe is not None and not pipe.closed:
                    pipe.close()
        for name, raw in pending.items():
            if raw:
                line(name, raw)
        for file in files.values():
            try:
                file.close()
            except OSError:
                pass
        completion = {"returncode": process.returncode if process else None,
                      "pid": process.pid if process else None, "birth": birth,
                      "elapsed_seconds": time.monotonic() - started,
                      "output_drain_incomplete": drain_incomplete}
        from .process_diagnostics import diagnose_process_exit, read_memory_events
        try:
            memory_after = (read_memory_events(memory_before.scope)
                            if memory_before is not None and memory_before.scope else None)
            completion['process_diagnostic'] = diagnose_process_exit(
                process.returncode if process else None, before=memory_before,
                after=memory_after, timed_out=status == 'timeout').to_dict()
            completion['pid'] = process.pid if process else None
            completion['memory_before'] = memory_before.to_dict() if memory_before else None
            completion['memory_after'] = memory_after.to_dict() if memory_after else None
        except (OSError, ValueError, TypeError) as error:
            completion['process_diagnostic'] = {
                'classification': 'unknown', 'collection_error': type(error).__name__}
        if launch_error is not None:
            completion["launch_error"] = launch_error
        record_event(root, invocation + ":finished", "process.finished", completion, **identity, status=status)
        try:
            Path(log).parent.mkdir(parents=True, exist_ok=True)
            if input_text is not None:
                Path(str(log) + '.stdin.txt').write_text(redact(input_text, secrets), encoding='utf-8')
            body = (folder / "combined.log").read_text(encoding="utf-8") if (folder / "combined.log").exists() else "[audit log unavailable]"
            if drain_incomplete:
                body += "\nOutput drain stopped after timeout; descendant pipes remained open.\n"
            if launch_error is not None:
                body += "\n" + launch_error + "\n"
            Path(log).write_text("$ " + " ".join(redact(list(args), secrets)) + "\n" + body + (f"\nTIMEOUT after {timeout}s\n" if status == "timeout" else "") + f"\nstatus={status}\n", encoding="utf-8")
        except OSError:
            warnings.warn("audit compatibility log unavailable", RuntimeWarning)


def record_sdk_events(root, events) -> int:
    """Copy public SDK events with stable IDs; replay does not duplicate entries."""
    count = 0
    for event in events:
        if is_dataclass(event):
            data = asdict(event)
        elif isinstance(event, Mapping):
            data = dict(event)
        elif hasattr(event, "to_dict"):
            data = event.to_dict()
        else:
            data = dict(vars(event))
        stable = data.get("event_id", data.get("id"))
        if stable is None:
            checksum = hashlib.sha256()
            for chunk in json.JSONEncoder(sort_keys=True, default=str).iterencode(data):
                checksum.update(chunk.encode())
            stable = checksum.hexdigest()
        count += record_event(root, "sdk:" + str(stable), "sdk.event", data, strict=True,
                              **{k: data[k] for k in ("run_id", "task_id", "stage_id", "command_id", "attempt", "timestamp") if k in data})
    return count


def _summary(events):
    groups = {}
    for event in events:
        _add_summary(groups, event)
    return list(groups.values())


def _add_summary(groups, event):
    """Add one event to an in-memory summary whose size is independent of payloads."""
    if event["kind"] != "usage":
        return
    usage = event["payload"]
    key = (event.get("agent_id"), event.get("requested_model"), event.get("reported_model"),
           usage.get("granularity"), event.get("provider"), event.get("api"),
           event.get("reasoning_effort"))
    group = groups.setdefault(key, {
        "agent_id": key[0], "requested_model": key[1], "reported_model": key[2],
        "granularity": key[3], "provider": key[4], "api": key[5],
        "reasoning_effort": key[6], "observations": 0,
        **{name: None for name in _TOKEN_FIELDS},
        "missing_observations": {name: 0 for name in _TOKEN_FIELDS},
    })
    group["observations"] += 1
    for field in _TOKEN_FIELDS:
        value = usage.get(field)
        if value is None:
            group["missing_observations"][field] += 1
        else:
            group[field] = (group[field] or 0) + value


def _snapshot_state(connection, root):
    """Resolve cross-event state once, inside the export's read transaction."""
    usage_invocations = set()
    finished = set()
    for kind, serialized in connection.execute(
            "SELECT kind,data FROM events WHERE kind='usage' OR kind LIKE '%.finished'"):
        invocation_id = load_data(root, serialized).get("invocation_id")
        (usage_invocations if kind == "usage" else finished).add(invocation_id)
    return usage_invocations, finished


def _iter_snapshot_events(connection, root, usage_invocations, finished):
    """Yield resolved events one database row at a time."""
    cursor = connection.execute(
        "SELECT event_id,timestamp,kind FROM events ORDER BY timestamp,event_id"
    )
    for event_id, timestamp, kind in cursor:
        # Sort only metadata. Including data makes SQLite materialize multi-GB
        # payloads in its sorter before the first row can be streamed.
        data, = connection.execute("SELECT data FROM events WHERE event_id=?",
                                   (event_id,)).fetchone()
        event = load_data(root, data)
        event.update(event_id=event_id, timestamp=timestamp, kind=kind)
        invocation_id = event.get("invocation_id")
        if kind == "process.started":
            event["usage_availability"] = (
                "reported" if invocation_id in usage_invocations else "unavailable"
            )
        if kind.endswith(".started") and invocation_id not in finished:
            event["status"] = "interrupted_or_unknown"
        yield event


def _write_csv_row(file, columns, event):
    usage = event.get("payload", {}) if event["kind"] == "usage" else {}
    row = {**event, **{name: usage.get(name)
                      for name in (*_TOKEN_FIELDS, "granularity", "availability")}}
    encoder = json.JSONEncoder(ensure_ascii=False)
    for index, name in enumerate(columns):
        if index:
            file.write(",")
        file.write('"')
        if name == "payload":
            chunks = encoder.iterencode(event.get("payload"))
        else:
            value = row.get(name)
            value = "" if value is None else str(value)
            if value.startswith(("=", "+", "-", "@")):
                value = "'" + value
            chunks = (value,)
        for chunk in chunks:
            # CSV's C writer buffers a whole escaped row, which can multiply
            # a large event's memory use. Quote bounded chunks directly.
            for offset in range(0, len(chunk), 65536):
                file.write(chunk[offset:offset + 65536].replace('"', '""'))
        file.write('"')
    file.write("\r\n")


def _html_scalar(value):
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if not isinstance(value, str):
        return f"[{type(value).__name__} omitted]"
    if len(value) > _HTML_METADATA_PREVIEW_CHARS:
        return value[:_HTML_METADATA_PREVIEW_CHARS] + "…"
    return value


def _html_usage(group):
    fields = ("agent_id", "requested_model", "reported_model", "granularity", "provider",
              "api", "reasoning_effort", "observations", *_TOKEN_FIELDS)
    result = {key: _html_scalar(group.get(key)) for key in fields}
    result["missing_observations"] = {
        key: group["missing_observations"].get(key, 0) for key in _TOKEN_FIELDS
    }
    return result


def _html_event(event):
    """Create a fixed-size timeline entry; full payloads stay in JSON and CSV."""
    preview = {key: _html_scalar(event[key]) for key in _HTML_EVENT_FIELDS if key in event}
    digest = hashlib.sha256()
    byte_count = 0
    character_count = 0
    payload_preview = []
    preview_characters = 0
    encoder = json.JSONEncoder(ensure_ascii=False, separators=(",", ":"))
    for chunk in encoder.iterencode(event.get("payload")):
        encoded = chunk.encode("utf-8")
        digest.update(encoded)
        byte_count += len(encoded)
        character_count += len(chunk)
        remaining = _HTML_PAYLOAD_PREVIEW_CHARS - preview_characters
        if remaining > 0:
            piece = chunk[:remaining]
            payload_preview.append(piece)
            preview_characters += len(piece)
    preview["payload_bytes"] = byte_count
    preview["payload_sha256"] = digest.hexdigest()
    preview["payload_preview"] = "".join(payload_preview) + (
        "…" if character_count > _HTML_PAYLOAD_PREVIEW_CHARS else ""
    )
    return preview


def _file_digest(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _flush_file(file):
    file.flush()
    os.fsync(file.fileno())


def _fsync_directory(path):
    if os.name == 'nt':
        # Native file publication uses flushed files and write-through moves.
        from .platform_files import assert_no_reparse
        assert_no_reparse(Path(path).absolute())
        return
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _regular_file(path):
    try:
        return stat.S_ISREG(path.lstat().st_mode)
    except OSError:
        return False


def _seal_generation(directory, generation):
    artifacts = {}
    for extension in _REPORT_EXTENSIONS:
        path = directory / ("audit." + extension)
        if not _regular_file(path):
            raise OSError("audit generation is missing " + path.name)
        artifacts[path.name] = {"sha256": _file_digest(path), "size": path.stat().st_size}
    manifest = {"schema_version": 1, "generation": generation, "artifacts": artifacts}
    path = directory / "manifest.json"
    with path.open("w", encoding="utf-8") as file:
        json.dump(manifest, file, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        file.write("\n")
        _flush_file(file)
    _fsync_directory(directory)


def _current_generation(destination):
    if os.name == 'nt':
        pointer = destination / '.audit-current.json'
        try:
            from .platform_files import safe_open
            descriptor = safe_open(destination.absolute(), pointer.name)
            with os.fdopen(descriptor, 'r', encoding='utf-8') as source:
                document = json.load(source)
            name = document.get('generation')
            if not isinstance(name, str) or not re.fullmatch(r'generation-[0-9a-f]{32}', name):
                return None
            directory = destination / _REPORT_GENERATIONS / name
            return directory if directory.is_dir() and not directory.is_symlink() else None
        except (OSError, ValueError, TypeError):
            return None
    pointer = destination / _REPORT_CURRENT
    if not pointer.is_symlink():
        return None
    try:
        target = os.readlink(pointer)
    except OSError:
        return None
    match = re.fullmatch(r"\.audit-generations/(generation-[0-9a-f]{32})", target)
    if match is None:
        return None
    directory = destination / _REPORT_GENERATIONS / match.group(1)
    manifest_path = directory / "manifest.json"
    if not _regular_file(manifest_path):
        return None
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if (not isinstance(manifest, dict) or manifest.get("schema_version") != 1
            or manifest.get("generation") != match.group(1)):
        return None
    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, dict):
        return None
    for extension in _REPORT_EXTENSIONS:
        name = "audit." + extension
        entry = artifacts.get(name)
        path = directory / name
        if (not isinstance(entry, dict) or not _regular_file(path)
                or entry.get("size") != path.stat().st_size
                or not isinstance(entry.get("sha256"), str)):
            return None
    return directory


def _replace_symlink(path, target):
    temporary = path.with_name("." + path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        os.symlink(target, temporary)
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _archive_existing_report(destination, paths, generations):
    if not all(_regular_file(paths[extension]) for extension in _REPORT_EXTENSIONS):
        return None
    name = "generation-" + uuid.uuid4().hex
    directory = generations / name
    directory.mkdir()
    try:
        for extension in _REPORT_EXTENSIONS:
            os.link(paths[extension], directory / paths[extension].name,
                    follow_symlinks=False)
        _seal_generation(directory, name)
        _fsync_directory(generations)
        _replace_symlink(destination / _REPORT_CURRENT,
                         f"{_REPORT_GENERATIONS}/{name}")
        _fsync_directory(destination)
        return directory
    except BaseException:
        shutil.rmtree(directory, ignore_errors=True)
        raise


def _publish_generation(destination, staging, paths):
    generations = destination / _REPORT_GENERATIONS
    generations.mkdir(mode=0o700, exist_ok=True)
    if generations.is_symlink() or not generations.is_dir():
        raise OSError("audit generation store must be a real directory")
    generation = "generation-" + uuid.uuid4().hex
    _seal_generation(staging, generation)
    ready = generations / generation
    os.replace(staging, ready)
    _fsync_directory(generations)

    previous = _current_generation(destination)
    if os.name == 'nt':
        from .platform_files import atomic_write
        # Return immutable generation paths. Windows does not require symlink
        # privilege, and opening one report cannot mix generations.
        atomic_write(destination / '.audit-current.json',
            (json.dumps({'generation': generation}) + '\n').encode())
        redirect = ('<!doctype html><meta charset="utf-8"><meta http-equiv="refresh" '
                    'content="0;url=' + _REPORT_GENERATIONS + '/' + generation + '/audit.html">'
                    '<a href="' + _REPORT_GENERATIONS + '/' + generation + '/audit.html">Open audit report</a>')
        atomic_write(paths['html'], redirect.encode())
        for extension in _REPORT_EXTENSIONS:
            paths[extension] = ready / ('audit.' + extension)
        keep = {ready, previous}
        for path in generations.iterdir():
            if (path not in keep and re.fullmatch(r'generation-[0-9a-f]{32}', path.name)
                    and path.is_dir() and not path.is_symlink()):
                shutil.rmtree(path)
        return
    if previous is None:
        previous = _archive_existing_report(destination, paths, generations)

    # Stable public paths always resolve through the one current pointer. If
    # no trustworthy prior report exists, remove the HTML completion view
    # first; the aliases remain dangling until the new pointer is published.
    order = _REPORT_EXTENSIONS if previous is not None else ("html", "json", "csv", "md")
    for extension in order:
        path = paths[extension]
        expected = f"{_REPORT_CURRENT}/{path.name}"
        if not path.is_symlink() or os.readlink(path) != expected:
            _replace_symlink(path, expected)
    _fsync_directory(destination)

    keep = {ready}
    if previous is not None:
        keep.add(previous)
    for path in generations.iterdir():
        if (path not in keep and re.fullmatch(r"generation-[0-9a-f]{32}", path.name)
                and path.is_dir() and not path.is_symlink()):
            shutil.rmtree(path)
    _fsync_directory(generations)

    # This is the generation commit. Every public alias changes together when
    # this single relative symlink is atomically replaced.
    _replace_symlink(destination / _REPORT_CURRENT,
                     f"{_REPORT_GENERATIONS}/{generation}")
    _fsync_directory(destination)


def export_report(root, outdir=None):
    destination = Path(outdir) if outdir else Path(root) / "audit-report"
    destination.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(destination / ".audit-publish.lock",
                         os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        # Lock before taking the snapshot: a slower old export must not
        # publish after a newer export and move the current report backwards.
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        return _export_report_locked(root, destination)
    finally:
        os.close(descriptor)


def _export_report_locked(root, outdir=None):
    """Stream a consistent snapshot to complete JSON/CSV and a bounded HTML view."""
    generated_at = _now()
    token_semantics = ("Cached input is included in input. Null means unavailable. "
                       "Separate granularities must not be added together. Totals sum reported "
                       "observations only; missing observations remain explicit.")
    destination = Path(outdir) if outdir else Path(root) / "audit-report"
    destination.mkdir(parents=True, exist_ok=True)
    paths = {extension: destination / ("audit." + extension) for extension in _REPORT_EXTENSIONS}
    columns = ["event_id", "timestamp", "kind", "run_id", "stage_id", "task_id", "command_id", "attempt", "invocation_id", "operation_invocation_id", "agent_id", "requested_model", "reported_model", "provider", "api", "reasoning_effort", "status", "usage_availability", "granularity", "availability", *_TOKEN_FIELDS, "payload"]
    staging = Path(tempfile.mkdtemp(prefix=".audit-export-", dir=destination))
    try:
        staged = {extension: staging / path.name for extension, path in paths.items()}
        groups = {}
        html_events = []
        event_count = 0
        connection = _connect(root)
        try:
            # A read transaction pins every output to one SQLite snapshot even if
            # append-only writers commit while a large export is in progress.
            connection.execute("BEGIN")
            usage_invocations, finished = _snapshot_state(connection, root)
            with staged["json"].open("w", encoding="utf-8") as json_file, \
                    staged["csv"].open("w", newline="", encoding="utf-8") as csv_file:
                json_file.write('{"schema_version":1,"generated_at":')
                json.dump(generated_at, json_file, ensure_ascii=False)
                json_file.write(',"token_semantics":')
                json.dump(token_semantics, json_file, ensure_ascii=False)
                json_file.write(',"events":[')
                writer = csv.DictWriter(csv_file, fieldnames=columns, extrasaction="ignore")
                writer.writeheader()
                first = True
                for event in _iter_snapshot_events(connection, root, usage_invocations, finished):
                    if not first:
                        json_file.write(",")
                    first = False
                    json.dump(event, json_file, ensure_ascii=False, separators=(",", ":"))
                    _write_csv_row(csv_file, columns, event)
                    _add_summary(groups, event)
                    if len(html_events) < _HTML_EVENT_LIMIT:
                        html_events.append(_html_event(event))
                    event_count += 1
                usage_summary = list(groups.values())
                json_file.write('],"usage_summary":')
                json.dump(usage_summary, json_file, ensure_ascii=False, separators=(",", ":"))
                json_file.write("}\n")
                _flush_file(json_file)
                _flush_file(csv_file)
            connection.rollback()
        finally:
            connection.close()

        hashes = {name: _file_digest(staged[name]) for name in ("json", "csv")}
        report = {
            "schema_version": 1, "generated_at": generated_at,
            "token_semantics": token_semantics, "event_count": event_count,
            "events": html_events, "omitted_events": max(0, event_count - len(html_events)),
            "usage_group_count": len(usage_summary),
            "usage_summary": [_html_usage(group) for group in usage_summary[:_HTML_USAGE_LIMIT]],
            "omitted_usage_groups": max(0, len(usage_summary) - _HTML_USAGE_LIMIT),
            "artifacts": {
                "json": {"href": "audit.json", "sha256": hashes["json"], "complete": True},
                "csv": {"href": "audit.csv", "sha256": hashes["csv"], "complete": True},
            },
        }
        markdown = ("# ModPort run audit\n\n" + token_semantics + "\n\n" +
                    f"Recorded events: {event_count}\n\n" +
                    f"Complete JSON: `audit.json` (SHA-256 `{hashes['json']}`)\n\n" +
                    f"Complete CSV: `audit.csv` (SHA-256 `{hashes['csv']}`)\n\n" +
                    "```json\n" + json.dumps(usage_summary, indent=2, ensure_ascii=False).replace("```", "` ` `") + "\n```\n")
        with staged["md"].open("w", encoding="utf-8") as file:
            file.write(markdown)
            _flush_file(file)
        with staged["html"].open("w", encoding="utf-8") as file:
            file.write(_html(report))
            _flush_file(file)

        _publish_generation(destination, staging, paths)
    finally:
        if staging.exists():
            shutil.rmtree(staging)
    return paths


def _html(report):
    data = json.dumps(report, ensure_ascii=False).replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026").replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")
    links = ('<p id="complete"><a href="audit.json">Complete JSON</a> — SHA-256 '
             + report["artifacts"]["json"]["sha256"]
             + ' <a href="audit.csv">Complete CSV</a> — SHA-256 '
             + report["artifacts"]["csv"]["sha256"] + '</p>')
    sample = (f'<p id="sample">Showing {len(report["events"])} of {report["event_count"]} events. '
              'Use the complete JSON or CSV for the full timeline.</p>')
    usage_sample = (f'<p>Showing {len(report["usage_summary"])} of '
                    f'{report["usage_group_count"]} usage groups.</p>')
    return '''<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>ModPort run audit</title>
<style>:root{--bg:#f8fafc;--ink:#172554;--muted:#475569;--line:#dbeafe;--blue:#1e40af}*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:16px/1.5 system-ui,sans-serif}main{max-width:1440px;padding:32px;margin:auto}h1{margin:0}p{color:var(--muted)}.filters,.cards{display:flex;gap:16px;flex-wrap:wrap;margin:24px 0}label{display:grid;gap:6px}select,input{font:inherit;min-height:44px;border:1px solid var(--line);border-radius:6px;padding:8px;background:white;max-width:100%}:focus-visible{outline:3px solid var(--blue);outline-offset:3px}.card{background:white;padding:20px;border:1px solid var(--line);border-radius:8px;flex:1;min-width:180px}.card strong{display:block;font-size:28px}.scroll{overflow:auto;background:white;border:1px solid var(--line)}table{border-collapse:collapse;width:100%;font-size:14px}th,td{text-align:left;padding:12px;border-bottom:1px solid var(--line);vertical-align:top}th{background:#e9eef6}tr:hover{background:#f1f5f9}pre{white-space:pre-wrap;overflow-wrap:anywhere;max-width:650px}summary{cursor:pointer;min-height:44px}small{color:var(--muted)}@media(max-width:600px){main{padding:16px}.filters>*{width:100%}}</style>
<main><small>MODPORT / RUN EVIDENCE</small><h1>Run audit</h1><p id="semantics"></p>''' + links + '''<div class="filters"><label>Agent<select id="agent"><option value="">All agents</option></select></label><label>Model (requested or reported)<select id="model"><option value="">All models</option></select></label><label>Search timeline sample<input id="search" type="search" placeholder="Stage, command, status…"></label></div><div id="cards" class="cards" aria-live="polite"></div><h2>Token usage by model and agent</h2><p>Each row is a separate reporting granularity. Unavailable counts are shown as unknown.</p>''' + usage_sample + '''<div class="scroll"><table><thead><tr><th>Agent</th><th>Requested model</th><th>Reported model</th><th>Provider</th><th>API</th><th>Reasoning effort</th><th>Granularity</th><th>Input</th><th>Cached input (subset)</th><th>Output</th><th>Reasoning output (subset)</th></tr></thead><tbody id="usage"></tbody></table></div><h2>Operation timeline sample</h2>''' + sample + '''<div class="scroll"><table><thead><tr><th>Time</th><th>Event</th><th>Stage / agent</th><th>Status</th><th>Details</th></tr></thead><tbody id="timeline"></tbody></table></div><noscript>The complete audit remains available in audit.json and audit.csv.</noscript></main><script id="data" type="application/json">''' + data + '''</script><script>
const d=JSON.parse(document.getElementById('data').textContent),$=id=>document.getElementById(id);$('semantics').textContent=d.token_semantics;
function option(id,v){const o=document.createElement('option');o.value=v;o.textContent=v;$(id).append(o)}
[...new Set(d.events.map(e=>e.agent_id).filter(Boolean))].sort().forEach(v=>option('agent',v));[...new Set(d.events.flatMap(e=>[e.requested_model,e.reported_model]).filter(Boolean))].sort().forEach(v=>option('model',v));
function cell(row,value){const td=document.createElement('td');td.textContent=value??'unknown';row.append(td);return td}
function matches(e){return (!$('agent').value||e.agent_id===$('agent').value)&&(!$('model').value||[e.requested_model,e.reported_model].includes($('model').value))}
function render(){const events=d.events.filter(matches).filter(e=>JSON.stringify(e).toLowerCase().includes($('search').value.toLowerCase()));$('timeline').replaceChildren();$('usage').replaceChildren();$('cards').replaceChildren();[['Recorded events',d.event_count],['Visible sample events',events.length],['Sample omitted',d.omitted_events]].forEach(([label,n])=>{const card=document.createElement('div');card.className='card';card.textContent=label;const count=document.createElement('strong');count.textContent=n;card.append(count);$('cards').append(card)});d.usage_summary.filter(matches).forEach(e=>{const tr=document.createElement('tr');['agent_id','requested_model','reported_model','provider','api','reasoning_effort','granularity','input_tokens','cached_input_tokens','output_tokens','reasoning_output_tokens'].forEach(k=>cell(tr,e[k]===null?'unknown':String(e[k])+(e.missing_observations[k]?' (partial)':'')));$('usage').append(tr)});events.forEach(e=>{const tr=document.createElement('tr');[e.timestamp,e.kind,[e.stage_id,e.agent_id].filter(Boolean).join(' / '),e.status].forEach(v=>cell(tr,v));const td=cell(tr,'');const details=document.createElement('details'),s=document.createElement('summary'),p=document.createElement('pre');s.textContent='Inspect event summary';p.textContent=JSON.stringify(e,null,2);details.append(s,p);td.append(details);$('timeline').append(tr)})}['agent','model','search'].forEach(id=>$(id).addEventListener('input',render));render();</script></html>'''


def probe_process(args, *, cwd=None, env=None, pass_fds=(), text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False, timeout=None):
    """Audited equivalent for the handlers' simple capture-only subprocess probes."""
    if not _CONTEXT.get():
        from .workspace import git_probe
        return git_probe(args, cwd=cwd, env=env, **({} if os.name == 'nt' else {'pass_fds': pass_fds}),
                              text=text, stdout=stdout, stderr=stderr,
                              check=check, timeout=timeout)
    directory = Path(cwd) if cwd is not None else Path.cwd()
    root = Path(_CONTEXT.get()["root"])
    result = run_process(args, cwd=directory, env=env, pass_fds=pass_fds,
                         timeout=timeout, log=root / "logs" / "probe.log",
                         combine_output=stderr == subprocess.STDOUT)
    result.stderr = None if stderr in (subprocess.STDOUT, subprocess.DEVNULL) else result.stderr
    if not text:
        result.stdout = result.stdout.encode()
        if result.stderr is not None:
            result.stderr = result.stderr.encode()
    if check:
        result.check_returncode()
    return result


class ProcessAudit:
    """Audit a caller-owned streaming process without controlling its lifecycle."""

    def __init__(self, root, args, *, cwd, log, env=None):
        self.root = Path(root)
        self.invocation_id = str(uuid.uuid4())
        self.identity = {"invocation_id": self.invocation_id, "operation_invocation_id": _CONTEXT.get().get("invocation_id")}
        self.folder = self.root / "audit-logs" / self.invocation_id
        self.log = Path(log)
        self.secrets = _secrets(env)
        self.pending = b""
        self.file = None
        self.finished = False
        self.started = time.monotonic()
        try:
            self.folder.mkdir(parents=True, exist_ok=True)
            self.file = (self.folder / "combined.log").open("w", encoding="utf-8")
        except OSError:
            warnings.warn("audit stream log unavailable", RuntimeWarning)
        record_event(root, self.invocation_id + ":started", "process.started", {"args": redact(args, self.secrets), "cwd": str(cwd), "logs": str(self.folder), "stream_mode": "combined"}, **self.identity, status="started")

    def write(self, chunk):
        self.pending += chunk
        while b"\n" in self.pending:
            line, self.pending = self.pending.split(b"\n", 1)
            self._write(line + b"\n")

    def _write(self, chunk):
        if self.file is not None:
            try:
                self.file.write(redact(chunk.decode("utf-8", errors="replace"), self.secrets))
                self.file.flush()
            except OSError:
                pass

    def finish(self, status, returncode=None):
        if self.finished:
            return
        self.finished = True
        self._write(self.pending)
        self.pending = b""
        if self.file is not None:
            try:
                self.file.close()
                self.log.parent.mkdir(parents=True, exist_ok=True)
                self.log.write_text((self.folder / "combined.log").read_text(encoding="utf-8"), encoding="utf-8")
            except OSError:
                warnings.warn("audit compatibility log unavailable", RuntimeWarning)
        record_event(self.root, self.invocation_id + ":finished", "process.finished", {"returncode": returncode, "elapsed_seconds": time.monotonic() - self.started}, **self.identity, status=status)
