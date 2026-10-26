from __future__ import annotations

import ast
import json
import importlib.util
import logging
import os
import re
import sys
import time
import warnings
from contextlib import contextmanager, redirect_stdout
from contextvars import ContextVar
from datetime import datetime, timezone
from pathlib import Path
from threading import RLock, local
from typing import Any, Iterator, TextIO
from uuid import uuid4

WORKER_VERSION = "0.1.0"
TERMINAL_LOG_PREFIX = "__PYMSS_STUDIO_TERMINAL_LOG__"
MAX_LOG_LINE_CHARS = 8 * 1024
MAX_LOG_BYTES = 5 * 1024 * 1024
MAX_PERSISTENT_LOG_BYTES = 10 * 1024 * 1024
MAX_LOG_TEXT_NESTING = 8
SENSITIVE_KEY_RE = re.compile(r"(password|passwd|token|secret|credential|authorization|api[_-]?key|proxy[-_]?pass)", re.IGNORECASE)
URL_CREDENTIALS_RE = re.compile(r"([a-z][a-z0-9+.-]*://)[^\s/@]+@", re.IGNORECASE)
SENSITIVE_ASSIGNMENT_RE = re.compile(
    r"(?i)\b(password|passwd|access_token|refresh_token|token|secret|credential|authorization|api[_-]?key|proxy[-_]?pass)['\"]?\s*[=:]\s*"
)

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

_PROTOCOL_STDOUT: TextIO | None = None
_BOUND_REQUEST_ID: str | None = None
_PROTOCOL_WRITE_LOCK = RLock()
_LOG_CONTEXT: ContextVar[dict[str, Any]] = ContextVar("worker_log_context", default={})
_LOG_TASK_ID: ContextVar[str | None] = ContextVar("worker_log_task_id", default=None)
_LOG_EVENTS = {
    "task_log": ("worker", "worker.infer"),
    "download_log": ("worker", "worker.download"),
    "optional_package_log": ("third_party", "pip"),
    "runtime_install_log": ("third_party", "pip"),
    "runtime_core_update_log": ("third_party", "pip"),
}
_NOISY_THIRD_PARTY_LOGGER_PREFIXES = ("numba", "llvmlite")


def _is_noisy_third_party_logger(name: str) -> bool:
    """Return whether a logger belongs to a compiler-detail namespace."""
    return any(name == prefix or name.startswith(f"{prefix}.") for prefix in _NOISY_THIRD_PARTY_LOGGER_PREFIXES)


def normalize_log_level(value: Any) -> str | None:
    level = str(value or "").lower()
    return {"warning": "warn", "critical": "fatal"}.get(level, level) if level in {
        "trace", "debug", "info", "warn", "warning", "error", "fatal", "critical",
    } else None


def current_log_task_id() -> str | None:
    return _LOG_TASK_ID.get()


@contextmanager
def task_logging(task_id: str) -> Iterator[None]:
    """Attribute synchronous file work without guessing the origin of background logs."""
    token = _LOG_TASK_ID.set(task_id)
    try:
        yield
    finally:
        _LOG_TASK_ID.reset(token)


def _safe_log_value(value: Any, key: str = "") -> Any:
    if SENSITIVE_KEY_RE.search(key):
        return "<redacted>"
    if isinstance(value, dict):
        return {str(name): _safe_log_value(item, str(name)) for name, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_safe_log_value(item) for item in value]
    if isinstance(value, str):
        return _redact_sensitive_text(value)
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return _redact_sensitive_text(str(value))


def log_record(
    message: str,
    *,
    source: str = "worker",
    level: str | None = "info",
    logger: str = "worker",
    event: str | None = None,
    timestamp: str | None = None,
    detail: str | None = None,
    task_id: str | None = None,
    node_id: str | None = None,
    job_id: str | None = None,
    request_id: str | None = None,
    location: dict[str, Any] | None = None,
    context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Create one transport record, without formatting metadata into its message."""
    payload: dict[str, Any] = {
        "schemaVersion": 1,
        "id": uuid4().hex,
        "timestamp": timestamp or now_iso(),
        "source": source,
        "level": normalize_log_level(level),
        "logger": logger,
        "event": event or "log",
        "message": _redact_sensitive_text(str(message)),
        "context": _safe_log_value({"processId": os.getpid(), **_LOG_CONTEXT.get(), **(context or {})}),
    }
    for key, value in {
        "taskId": task_id,
        "requestId": request_id if request_id is not None else _BOUND_REQUEST_ID,
        "nodeId": node_id,
        "jobId": job_id if job_id is not None else _LOG_CONTEXT.get().get("jobId"),
        "detail": detail,
        "location": location,
    }.items():
        if value is not None:
            payload[key] = _safe_log_value(value)
    return payload


class ProtocolLogHandler(logging.Handler):
    """Forward the original LogRecord once; never log a transport failure recursively."""

    def __init__(
        self,
        *,
        event_type: str = "worker_log",
        task_id: str | None = None,
        task_id_provider: Any = None,
        node_id_provider: Any = None,
        level_provider: Any = None,
        source: str | None = None,
        context: dict[str, Any] | None = None,
        emitter: Any = None,
    ) -> None:
        super().__init__(logging.INFO)
        self.event_type = event_type
        self.task_id = task_id
        self.task_id_provider = task_id_provider
        self.node_id_provider = node_id_provider
        self.level_provider = level_provider
        self.source = source
        self.context = context or {}
        self.emitter = emitter
        self._emitting = local()

    @property
    def level(self) -> int:
        provider = getattr(self, "level_provider", None)
        return provider() if provider is not None else self._level

    @level.setter
    def level(self, value: int) -> None:
        self._level = value

    def emit(self, record: logging.LogRecord) -> None:
        # Numba and llvmlite can emit thousands of compiler-detail records while
        # rendering one export. Keep warnings/errors, even if a child logger was
        # explicitly changed to DEBUG after protocol_logging configured it.
        if _is_noisy_third_party_logger(record.name) and record.levelno < logging.WARNING:
            return
        if getattr(self._emitting, "active", False):
            return
        self._emitting.active = True
        try:
            task_id = self.task_id_provider() if self.task_id_provider else self.task_id
            node_id = self.node_id_provider() if self.node_id_provider else None
            detail = None
            if record.exc_info:
                detail = logging.Formatter().formatException(record.exc_info)
            elif record.stack_info:
                detail = record.stack_info
            source = self.source or ("worker" if record.name.startswith("worker") else "third_party")
            context = {"processId": record.process, "thread": record.threadName, **self.context}
            if task_id is None:
                context["taskScope"] = "operation"
            else:
                context.pop("taskScope", None)
            payload = log_record(
                record.getMessage(),
                source=source,
                level=record.levelname,
                logger=record.name,
                event=getattr(record, "event", "log"),
                timestamp=datetime.fromtimestamp(record.created, timezone.utc).isoformat().replace("+00:00", "Z"),
                detail=detail,
                task_id=task_id,
                node_id=node_id,
                context=context,
                location={"file": record.pathname, "line": record.lineno, "function": record.funcName},
            )
            (self.emitter or emit)(self.event_type, payload, task_id=task_id)
        except Exception:
            # A broken pipe or malformed third-party record must not change task state,
            # call logging.handleError, or produce another copy on stderr.
            pass
        finally:
            self._emitting.active = False


@contextmanager
def protocol_logging(command: str, task_id: str | None = None, job_id: str | None = None, *, batch: bool = False) -> Iterator[None]:
    """Capture standard third-party logging only while hosted by Studio."""
    if os.environ.get("PYMSS_STUDIO_LOG_TRANSPORT") != "protocol":
        yield
        return
    context = {"command": command}
    if job_id:
        context["jobId"] = job_id
    token = _LOG_CONTEXT.set(context)
    task_token = _LOG_TASK_ID.set(None if batch else task_id)
    root = logging.getLogger()
    previous_level = root.level
    level = logging.DEBUG if os.environ.get("PYMSS_STUDIO_DEBUG_LOG") == "1" else logging.INFO
    handler = ProtocolLogHandler(
        task_id_provider=lambda: current_log_task_id() or (None if batch else task_id),
        context=context,
    )
    handler.setLevel(level)
    console_handlers = [
        item for item in root.handlers
        if isinstance(item, logging.StreamHandler) and not isinstance(item, logging.FileHandler)
    ]

    def suppress_console(_record: logging.LogRecord) -> bool:
        return False

    for console in console_handlers:
        console.addFilter(suppress_console)
    root.addHandler(handler)
    if previous_level > level:
        root.setLevel(level)
    noisy_logger_levels = {
        name: logging.getLogger(name).level
        for name in _NOISY_THIRD_PARTY_LOGGER_PREFIXES
    }
    for name in _NOISY_THIRD_PARTY_LOGGER_PREFIXES:
        logging.getLogger(name).setLevel(max(logging.WARNING, noisy_logger_levels[name]))
    previous_showwarning = warnings.showwarning

    def showwarning(message: Any, category: Any, filename: str, lineno: int, file: Any = None, line: Any = None) -> None:
        if file is not None:
            previous_showwarning(message, category, filename, lineno, file=file, line=line)
            return
        record = logging.LogRecord("py.warnings", logging.WARNING, filename, lineno, str(message), (), None)
        record.event = "python.warning"
        handler.handle(record)

    warnings.showwarning = showwarning
    try:
        with isolate_protocol_stdout():
            yield
    finally:
        root.removeHandler(handler)
        handler.close()
        root.setLevel(previous_level)
        for name, previous_logger_level in noisy_logger_levels.items():
            logging.getLogger(name).setLevel(previous_logger_level)
        for console in console_handlers:
            console.removeFilter(suppress_console)
        warnings.showwarning = previous_showwarning
        _LOG_TASK_ID.reset(task_token)
        _LOG_CONTEXT.reset(token)


@contextmanager
def bind_request_id(request_id: str | None) -> Iterator[None]:
    """Tag events emitted without an explicit request id with ``request_id``."""
    global _BOUND_REQUEST_ID
    previous = _BOUND_REQUEST_ID
    _BOUND_REQUEST_ID = request_id
    try:
        yield
    finally:
        _BOUND_REQUEST_ID = previous


@contextmanager
def isolate_protocol_stdout() -> Iterator[None]:
    """Keep protocol events on stdout while routing incidental prints to stderr."""
    global _PROTOCOL_STDOUT
    previous_stdout = _PROTOCOL_STDOUT
    _PROTOCOL_STDOUT = previous_stdout or sys.stdout
    try:
        with redirect_stdout(sys.stderr):
            yield
    finally:
        _PROTOCOL_STDOUT = previous_stdout

def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def debug_log(event: str, **fields: Any) -> None:
    if os.environ.get("PYMSS_STUDIO_LOG_TRANSPORT") == "protocol":
        try:
            detail = fields.pop("detail", None)
            message = {
                "worker.start": "Worker started",
                "worker.payload": "Command arguments loaded",
                "worker.exit": "Worker finished",
            }.get(event, event)
            if event == "worker.exception":
                # The business error event carries the single primary error record.
                return
            emit("worker_log", log_record(
                message,
                event=event,
                level="debug",
                logger="worker.lifecycle",
                detail=detail,
                context=fields,
            ))
        except Exception:
            pass
        return
    if os.environ.get("PYMSS_STUDIO_DEBUG_LOG") != "1":
        return
    path = os.environ.get("PYMSS_STUDIO_SESSION_LOG")
    persistent_path = os.environ.get("PYMSS_STUDIO_PERSISTENT_LOG")
    if not path and not persistent_path:
        return
    try:
        parts = [now_iso(), "PY", event]
        for key, value in fields.items():
            if value in (None, ""):
                continue
            if SENSITIVE_KEY_RE.search(key):
                text = "<redacted>"
            else:
                text = _redact_sensitive_text(str(value).replace("\r", " ").replace("\n", " "))
            if any(ch.isspace() for ch in text) or "=" in text:
                text = json.dumps(text, ensure_ascii=False)
            parts.append(f"{key}={text}")
        line = " ".join(parts)
        if len(line) > MAX_LOG_LINE_CHARS:
            line = line[:MAX_LOG_LINE_CHARS] + " ...[truncated]"
        _write_debug_line(path, line, MAX_LOG_BYTES)
        _write_debug_line(persistent_path, line, MAX_PERSISTENT_LOG_BYTES)
        if os.environ.get("PYMSS_STUDIO_TERMINAL_LOG") == "1":
            print(TERMINAL_LOG_PREFIX + line, file=sys.stderr, flush=True)
    except Exception:
        pass


def _redact_serialized_strings(value: str, depth: int) -> str:
    output = []
    copied = 0
    at = 0
    while at < len(value):
        quote = value[at]
        if quote not in {"'", '"'} or (at > 0 and value[at - 1] == "\\"):
            at += 1
            continue
        is_bytes = at > 0 and value[at - 1] == "b" and (
            at == 1 or (not value[at - 2].isalnum() and value[at - 2] not in "_\\")
        )
        literal_start = at - 1 if is_bytes else at
        if not is_bytes and at > 0 and (value[at - 1].isalnum() or value[at - 1] == "_"):
            first = at + 1
            while first < len(value) and value[first].isspace():
                first += 1
            # Word apostrophes are not wrappers; an adjacent serialized container
            # or another encoded string can still follow a producer's text prefix.
            if first == len(value) or value[first] not in "{[\"'\\":
                at += 1
                continue
        end = at + 1
        known_escapes = True
        while end < len(value) and value[end] != quote:
            if value[end] == "\\" and end + 1 < len(value):
                known_escapes = known_escapes and value[end + 1] in "\\'\"abfnrtvxuUN\r\n"
                end += 2
            else:
                end += 1
        if end == len(value):
            break
        literal = value[literal_start:end + 1]
        decoded = None
        encoder = json.dumps
        try:
            decoded = json.loads(literal)
        except (ValueError, RecursionError):
            # literal_eval only receives a single, lexically bounded string literal.
            # Reject unknown escapes before parsing so diagnostics do not emit warnings.
            if known_escapes:
                try:
                    decoded = ast.literal_eval(literal)
                    encoder = repr
                except (ValueError, SyntaxError, RecursionError):
                    pass
        if isinstance(decoded, bytes):
            def encoder(text: str) -> str:
                return repr(text.encode("utf-8"))

            try:
                decoded = decoded.decode("utf-8")
            except UnicodeDecodeError:
                if SENSITIVE_KEY_RE.search(literal):
                    output.extend((value[copied:literal_start], repr(b"<redacted>")))
                    copied = end + 1
                at = end + 1
                continue
        if isinstance(decoded, str) and SENSITIVE_KEY_RE.search(decoded) and (
            SENSITIVE_ASSIGNMENT_RE.search(decoded) or any(c in decoded for c in "\"'\\")
        ):
            clean = "<redacted>" if depth >= MAX_LOG_TEXT_NESTING else _redact_sensitive_text(decoded, _depth=depth + 1)
            if clean != decoded:
                output.extend((value[copied:literal_start], encoder(clean)))
                copied = end + 1
        at = end + 1
    output.append(value[copied:])
    return "".join(output)


def _redact_sensitive_text(value: str, *, _depth: int = 0) -> str:
    # Decode complete JSON/repr string layers before scanning assignments. Counting
    # backslashes in an encoded layer cannot identify its inner credential boundary.
    value = _redact_serialized_strings(value, _depth)
    value = URL_CREDENTIALS_RE.sub(r"\1", value)
    output = []
    copied = 0
    search_at = 0
    while match := SENSITIVE_ASSIGNMENT_RE.search(value, search_at):
        start = match.end()
        quote = value[start] if start < len(value) and value[start] in {"'", '"'} else None
        if quote is not None:
            start += 1
            end = start
            while end < len(value) and value[end] != quote:
                # Skip escaped quotes and escaped backslashes as a pair. Keep both
                # surrounding quotes so downstream JSON and text redaction remain valid.
                end += 2 if value[end] == "\\" and end + 1 < len(value) else 1
        elif match.group(1).lower() == "authorization":
            end = start
            while end < len(value) and value[end] not in "\r\n":
                end += 1
        else:
            end = start
            while end < len(value) and not value[end].isspace() and value[end] not in ",;&}]'\"":
                end += 1
        output.extend((value[copied:start], "<redacted>"))
        copied = end
        search_at = end + 1 if quote is not None and end < len(value) else end
    output.append(value[copied:])
    return "".join(output)


def _write_debug_line(path: str | None, line: str, max_bytes: int) -> None:
    if not path:
        return
    log_path = Path(path)
    if log_path.exists() and log_path.stat().st_size >= max_bytes:
        return
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8", errors="replace") as file:
        file.write(line + "\n")


def emit(event_type: str, payload: dict[str, Any] | None = None, *, request_id: str | None = None, task_id: str | None = None) -> None:
    payload = payload or {}
    if event_type in _LOG_EVENTS and payload.get("schemaVersion") != 1:
        source, logger = _LOG_EVENTS[event_type]
        context = {key: value for key, value in payload.items() if key not in {"message", "level", "detail", "nodeId"}}
        canonical = log_record(
            str(payload.get("message") or ""),
            source=source,
            logger=logger,
            event=event_type,
            level=payload.get("level"),
            detail=payload.get("detail"),
            task_id=task_id,
            request_id=request_id,
            node_id=payload.get("nodeId"),
            context=context,
        )
        payload = {**_safe_log_value(payload), **canonical}
    message = json.dumps({
        "type": event_type,
        "requestId": request_id if request_id is not None else _BOUND_REQUEST_ID,
        "taskId": task_id,
        "timestamp": now_iso(),
        "payload": payload,
    }, ensure_ascii=False)
    stream = _PROTOCOL_STDOUT or sys.stdout
    # Windows can transiently return ``[Errno 22] Invalid argument`` when the parent
    # is draining a worker pipe while a long-running pip process is still producing output.
    # A failed telemetry write must not turn a successful installation into a worker failure;
    # retry the write briefly before surfacing a genuinely closed/broken pipe.
    with _PROTOCOL_WRITE_LOCK:
        for attempt in range(3):
            try:
                _write_protocol_message(stream, message)
                return
            except OSError as error:
                retryable = isinstance(error, BrokenPipeError) or error.errno in {5, 22, 32, 33}
                if not retryable or attempt == 2:
                    raise
                time.sleep(0.05 * (attempt + 1))


def _write_protocol_message(stream: TextIO, message: str) -> None:
    # Writing bytes avoids another Windows TextIOWrapper flush failure for redirected stdout;
    # StringIO and other test/custom streams still use their regular text interface.
    buffer = getattr(stream, "buffer", None)
    if buffer is not None:
        buffer.write((message + "\n").encode("utf-8"))
        buffer.flush()
    else:
        stream.write(message + "\n")
        stream.flush()


def emit_error(
    code: str,
    message: str,
    detail: str | None = None,
    *,
    request_id: str | None = None,
    task_id: str | None = None,
    recoverable: bool = False,
    extra: dict[str, Any] | None = None,
) -> int:
    payload: dict[str, Any] = {
        "code": code,
        "message": message,
        "detail": detail,
        "recoverable": recoverable,
    }
    if extra:
        payload.update(extra)
    emit("error", payload, request_id=request_id, task_id=task_id)
    return 1


def import_available(name: str) -> bool:
    return importlib.util.find_spec(name) is not None


def load_payload(payload_arg: str | None) -> dict[str, Any]:
    if not payload_arg:
        return {}
    path = Path(payload_arg)
    if path.is_file():
        return json.loads(path.read_text(encoding="utf-8"))
    return json.loads(payload_arg)


def _as_int(value: Any) -> int | None:
    try:
        if value is None or value == "":
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


def _as_float(value: Any) -> float | None:
    try:
        if value is None or value == "":
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _as_bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"true", "1", "yes", "on"}:
            return True
        if normalized in {"false", "0", "no", "off"}:
            return False
    return None
