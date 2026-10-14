"""Centralized structured logging with ring buffer, optional file sink, and contextvars.

Phase 1 of the observability work. This module owns the single structlog
configuration used by both the API and the workers so that every process emits
the same JSON lines with the same metadata: ``correlation_id``, ``trace_id``,
``span_id``, ``component`` and ``process``.

It also keeps a bounded in-memory ring buffer of the most recent records, which
is what the logs UI reads and what the SSE stream tails. Worker processes run in
a different container, so the UI additionally reads the shared rotating
JSONL file (same ``./data`` volume) for cross-process visibility.
"""

from __future__ import annotations

import json
import logging
import logging.handlers
import threading
from collections import deque
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import structlog

from src.core import context
from src.core.config import get_settings

#: Most recent structured records, shared by the query and stream endpoints.
_RING_BUFFER: deque[dict[str, Any]] = deque(maxlen=10_000)
#: Per-component effective levels. Empty means "inherit the global level".
_COMPONENT_LEVELS: dict[str, str] = {}
#: Guards ring buffer and level registry against concurrent mutation.
_LOCK = threading.Lock()
#: Whether the running process has already configured logging.
_CONFIGURED = False
#: Shared rotating JSONL sink used by both structlog and stdlib records.
_FILE_HANDLER: logging.Handler | None = None
#: Renders an event dict to a JSON string (shared by ring and file sinks).
_JSON_RENDERER = structlog.processors.JSONRenderer()

_VALID_LEVELS = {"TRACE", "DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
_LEVEL_NUM = {
    "TRACE": logging.DEBUG,
    "DEBUG": logging.DEBUG,
    "INFO": logging.INFO,
    "WARNING": logging.WARNING,
    "ERROR": logging.ERROR,
    "CRITICAL": logging.CRITICAL,
}


def _settings() -> Any:
    """Return the application settings (indirection keeps tests patchable)."""
    return get_settings()


def _resize_ring(size: int) -> None:
    """Resize the ring buffer, preserving the most recent records.

    Args:
        size: New maximum number of records.
    """
    global _RING_BUFFER
    with _LOCK:
        if _RING_BUFFER.maxlen != size:
            _RING_BUFFER = deque(_RING_BUFFER, maxlen=size)


def _valid_level(level: str) -> str:
    """Normalize and validate a level name, raising ValueError when unknown.

    Args:
        level: Case-insensitive level name.

    Returns:
        The upper-cased level name.

    Raises:
        ValueError: When the level is not one of the accepted names.
    """
    normalized = str(level).strip().upper()
    if normalized not in _VALID_LEVELS:
        raise ValueError(f"unknown log level: {level!r}")
    return normalized


def _context_processor(_, __, event_dict: dict[str, Any]) -> dict[str, Any]:
    """Add correlation/trace/component/process context to every record."""
    if (cid := context.get_correlation_id()) and "correlation_id" not in event_dict:
        event_dict["correlation_id"] = cid
    if (tid := context.get_trace_id()) and "trace_id" not in event_dict:
        event_dict["trace_id"] = tid
    if (sid := context.get_span_id()) and "span_id" not in event_dict:
        event_dict["span_id"] = sid
    if (comp := context.get_component()) and "component" not in event_dict:
        event_dict["component"] = comp
    if (proc := context.get_process()) and "process" not in event_dict:
        event_dict["process"] = proc
    return event_dict


def _ring_processor(_, __, event_dict: dict[str, Any]) -> dict[str, Any]:
    """Copy each finalized record into the bounded ring buffer."""
    with _LOCK:
        _RING_BUFFER.append(dict(event_dict))
    return event_dict


def _file_processor(_, __, event_dict: dict[str, Any]) -> dict[str, Any]:
    """Append the rendered JSON line to the shared rotating file sink."""
    handler = _FILE_HANDLER
    if handler is None:
        return event_dict
    try:
        rendered = _JSON_RENDERER(None, "event", dict(event_dict))
        handler.stream.write(rendered + "\n")
        handler.stream.flush()
    except Exception:
        pass
    return event_dict


def _enforce_min_level(_, method_name: str, event_dict: dict[str, Any]) -> dict[str, Any]:
    """Drop records below the effective level for their component.

    The structlog filtering bound logger already applies the process-wide level.
    Per-component overrides are a second gate applied here when a component is
    registered by the UI.

    Args:
        method_name: structlog level method name ('info', 'debug', ...).

    Returns:
        The event dict, or a dropped marker when the record is filtered out.
    """
    component = event_dict.get("component") or context.get_component()
    if not component:
        return event_dict
    with _LOCK:
        level = _COMPONENT_LEVELS.get(component)
    if not level:
        return event_dict
    if _LEVEL_NUM.get(method_name.upper(), logging.INFO) < _LEVEL_NUM[level]:
        raise structlog.DropEvent
    return event_dict


def configure_logging(process_name: str = "api") -> None:
    """Configure structlog for this process.

    Idempotent: repeated calls (entry points, tests) reconfigure cheaply and
    never attach a second sink.

    Args:
        process_name: Label recorded on every record ('api', 'worker', ...).
    """
    global _CONFIGURED
    settings = _settings()
    context.set_process(process_name)
    _resize_ring(settings.log_ring_size)

    processors: list[Any] = [
        structlog.contextvars.merge_contextvars,
        _context_processor,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        _enforce_min_level,
    ]
    if settings.log_include_caller:
        processors.append(
            structlog.processors.CallsiteParameterAdder(
                {
                    structlog.processors.CallsiteParameter.MODULE,
                    structlog.processors.CallsiteParameter.FUNC_NAME,
                    structlog.processors.CallsiteParameter.LINENO,
                }
            )
        )
    processors.extend(
        [
            _ring_processor,
            _file_processor,
            structlog.processors.JSONRenderer(),
        ]
    )

    structlog.configure(
        processors=processors,
        wrapper_class=structlog.make_filtering_bound_logger(
            _LEVEL_NUM.get(str(settings.log_level).upper(), logging.INFO)
        ),
        cache_logger_on_first_use=not settings.log_include_caller,
    )

    _configure_file_sink(settings)
    if not _CONFIGURED:
        _configure_stdlib(settings)
        _CONFIGURED = True


def _configure_stdlib(settings: Any) -> None:
    """Make stdlib ``logging`` records (the Temporal SDK) reachable in the UI.

    The SDK logs through ``logging``; without a handler its lines never reach
    docker logs, the ring buffer or the shared file. We capture them into both.

    Args:
        settings: Application settings.
    """

    class _BufferHandler(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            try:
                payload = {
                    "event": record.getMessage(),
                    "level": record.levelname.lower(),
                    "logger": record.name,
                    "timestamp": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
                    "component": context.get_component() or record.name,
                    "process": context.get_process(),
                }
                with _LOCK:
                    _RING_BUFFER.append(payload)
                handler = _FILE_HANDLER
                if handler is not None:
                    handler.stream.write(json.dumps(payload, ensure_ascii=True) + "\n")
                    handler.stream.flush()
            except Exception:
                pass

    root = logging.getLogger()
    level = _LEVEL_NUM.get(str(settings.log_level).upper(), logging.INFO)
    if root.level == logging.WARNING or not root.handlers:
        root.setLevel(level)
        if not root.handlers:
            root.addHandler(logging.StreamHandler())
    root.addHandler(_BufferHandler())


def _configure_file_sink(settings: Any) -> None:
    """Create the shared rotating JSONL sink used by structlog and stdlib.

    The API and every worker mount the same ``./data`` volume, so a file sink is
    what makes worker logs visible to the API-served UI. Disabled when
    ``log_file_dir`` is empty.

    Args:
        settings: Application settings.
    """
    global _FILE_HANDLER
    if not settings.log_file_dir or _FILE_HANDLER is not None:
        return
    try:
        directory = Path(settings.log_file_dir)
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / settings.log_file_name
        handler = logging.handlers.RotatingFileHandler(
            path,
            maxBytes=int(settings.log_file_max_bytes),
            backupCount=int(settings.log_file_backup_count),
            encoding="utf-8",
        )
    except OSError:
        # A read-only or root-owned data volume must never stop the process;
        # the ring buffer still serves the UI.
        return
    _FILE_HANDLER = handler


def register_component(name: str) -> None:
    """Register a component so the UI can list and control its level.

    Args:
        name: Dotted component name (e.g. ``api.services.tts``).
    """
    with _LOCK:
        _COMPONENT_LEVELS.setdefault(name, _settings().log_level)


def set_component_level(component: str, level: str) -> None:
    """Set the effective level for one component.

    Args:
        component: Dotted component name.
        level: One of TRACE/DEBUG/INFO/WARNING/ERROR/CRITICAL.

    Raises:
        ValueError: When the level is unknown.
    """
    normalized = _valid_level(level)
    with _LOCK:
        _COMPONENT_LEVELS[component] = normalized
    # Mirror onto the stdlib logger when the component matches a real logger.
    logging.getLogger(component).setLevel(_LEVEL_NUM[normalized])


def get_component_levels() -> dict[str, str]:
    """Return a copy of the per-component level registry."""
    with _LOCK:
        return dict(_COMPONENT_LEVELS)


def get_ring_buffer() -> list[dict[str, Any]]:
    """Return a snapshot copy of the in-memory ring buffer."""
    with _LOCK:
        return list(_RING_BUFFER)


def clear_ring_buffer() -> None:
    """Empty the in-memory ring buffer (mainly for tests)."""
    with _LOCK:
        _RING_BUFFER.clear()


def read_file_tail(limit: int = 500) -> list[dict[str, Any]]:
    """Read the last ``limit`` parseable JSON lines from the shared log file.

    Args:
        limit: Maximum number of records to return, newest last.

    Returns:
        Parsed records; unparseable lines are skipped.
    """
    settings = _settings()
    if not settings.log_file_dir:
        return []
    path = Path(settings.log_file_dir) / settings.log_file_name
    if not path.exists():
        return []
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    records: list[dict[str, Any]] = []
    for line in lines[-limit:]:
        line = line.strip()
        if not line:
            continue
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return records
