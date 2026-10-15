"""Local-only tracing utilities using OpenTelemetry (no OTLP export).

Phase 1 of the observability work. OpenTelemetry is configured with an
in-memory span store instead of an OTLP collector: this project is self-hosted
and local-only, so the UI reads the recent spans straight from this process.

The ``trace_function`` decorator is what makes "trace a call across all modules
and functions" possible. It is opt-in per call site; services and the LLM/TTS
paths get it in Phase 2, activities and workflows in Phase 3.

Determinism note: never call this from inside a Temporal workflow. Workflows are
replayed and must not perform I/O or non-deterministic work; Temporal's own
interceptors cover them.
"""

from __future__ import annotations

import asyncio
import functools
import os
import uuid
from collections import deque
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any

from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import ReadableSpan, Span, TracerProvider
from opentelemetry.sdk.trace.export import (
    ConsoleSpanExporter,
    SimpleSpanProcessor,
    SpanExporter,
    SpanExportResult,
)

from src.core import context

#: Recent finished spans, keyed by trace id, for the traces view.
_INMEM_SPANS: deque[dict[str, Any]] = deque(maxlen=5000)


def _format_span(span: ReadableSpan) -> dict[str, Any]:
    """Convert a finished span into a JSON-friendly dict.

    Args:
        span: Finished span emitted by the SDK.

    Returns:
        Dict with ids, name, timing and attributes.
    """
    ctx = span.get_span_context()
    return {
        "trace_id": format(ctx.trace_id, "032x"),
        "span_id": format(ctx.span_id, "016x"),
        "parent_span_id": format(span.parent.span_id, "016x") if span.parent else None,
        "name": span.name,
        "start_time": span.start_time,
        "end_time": span.end_time,
        "duration_ms": (
            round((span.end_time - span.start_time) / 1_000_000, 3)
            if span.start_time and span.end_time
            else None
        ),
        "component": (dict(span.attributes or {}).get("component")),
        "attributes": dict(span.attributes or {}),
        "status": span.status.status_code.name if span.status else None,
    }


class _InMemorySpanExporter(SpanExporter):
    """Collect finished spans in a bounded deque for the local UI."""

    def export(self, spans: list[ReadableSpan]) -> SpanExportResult:
        for span in spans:
            _INMEM_SPANS.append(_format_span(span))
        return SpanExportResult.SUCCESS

    def shutdown(self) -> None:
        return None


def _ensure_provider() -> None:
    """Install the local TracerProvider exactly once per process."""
    if trace.get_tracer_provider().__class__.__name__ == "TracerProvider":
        return
    resource = Resource(attributes={"service.name": "news-engine"})
    provider = TracerProvider(resource=resource)
    provider.add_span_processor(SimpleSpanProcessor(_InMemorySpanExporter()))
    if os.getenv("TRACE_CONSOLE", "0") == "1":
        provider.add_span_processor(SimpleSpanProcessor(ConsoleSpanExporter()))
    trace.set_tracer_provider(provider)


def get_tracer(component: str = "news-engine") -> trace.Tracer:
    """Return a tracer named after a component.

    Args:
        component: Dotted component name used to group and filter spans.

    Returns:
        An OpenTelemetry tracer.
    """
    _ensure_provider()
    return trace.get_tracer(component)


def start_span(name: str, **attrs: Any) -> Span:
    """Start a span and mirror its ids into the logging contextvars.

    Args:
        name: Span name.
        **attrs: Span attributes; ``component`` selects the tracer.

    Returns:
        The started (not yet ended) span.
    """
    tracer = get_tracer(str(attrs.pop("component", "news-engine")))
    span = tracer.start_span(name, attributes=attrs)
    ctx = span.get_span_context()
    context.set_trace_context(format(ctx.trace_id, "032x"), format(ctx.span_id, "016x"))
    return span


def inject_trace_context() -> str:
    """Return a W3C ``traceparent`` for the current (or a fresh) trace.

    Used to propagate the trace across process boundaries such as Temporal
    activity inputs and Modal calls.

    Returns:
        A ``00-<trace_id>-<span_id>-01`` traceparent string.
    """
    tid = context.get_trace_id()
    sid = context.get_span_id()
    if not tid or not sid:
        tid = uuid.uuid4().hex
        sid = format(uuid.uuid4().int & ((1 << 64) - 1), "016x")
        context.set_trace_context(tid, sid)
    return f"00-{tid}-{sid}-01"


def trace_function(
    component: str | None = None,
    capture_args: bool = False,
    capture_result: bool = False,
    max_arg_len: int = 200,
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Decorate a sync or async function so each call opens a span.

    Args:
        component: Component name for filtering; defaults to the module name.
        capture_args: Record a truncated repr of positional args.
        capture_result: Record a truncated repr of the return value.
        max_arg_len: Truncation length for captured args/results.

    Returns:
        A decorator wrapping the target function.
    """

    def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
        comp = component or func.__module__

        def _attrs(args: tuple[Any, ...], kwargs: dict[str, Any]) -> dict[str, Any]:
            attrs: dict[str, Any] = {"component": comp, "module": func.__module__}
            if capture_args and args:
                attrs["args"] = repr(args)[:max_arg_len]
            if capture_args and kwargs:
                attrs["kwargs"] = repr(kwargs)[:max_arg_len]
            return attrs

        @contextmanager
        def _span(args: tuple[Any, ...], kwargs: dict[str, Any]) -> Iterator[None]:
            tracer = get_tracer(comp)
            with tracer.start_as_current_span(
                func.__qualname__, attributes=_attrs(args, kwargs)
            ) as span:
                ctx = span.get_span_context()
                token_t = context.trace_id_var.set(format(ctx.trace_id, "032x"))
                token_s = context.span_id_var.set(format(ctx.span_id, "016x"))
                try:
                    yield None
                finally:
                    context.trace_id_var.reset(token_t)
                    context.span_id_var.reset(token_s)

        @functools.wraps(func)
        def sync_wrapper(*args: Any, **kwargs: Any) -> Any:
            with _span(args, kwargs):
                return func(*args, **kwargs)

        @functools.wraps(func)
        async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
            with _span(args, kwargs):
                return await func(*args, **kwargs)

        return async_wrapper if asyncio.iscoroutinefunction(func) else sync_wrapper

    return decorator


def get_inmem_spans(
    trace_id: str | None = None,
    component: str | None = None,
    limit: int = 200,
) -> list[dict[str, Any]]:
    """Return recently finished spans, newest last.

    Args:
        trace_id: Filter to a single trace.
        component: Filter to a single component.
        limit: Maximum number of spans to return.

    Returns:
        List of span dicts.
    """
    items = list(_INMEM_SPANS)
    if trace_id:
        items = [i for i in items if i.get("trace_id") == trace_id]
    if component:
        items = [i for i in items if i.get("component") == component]
    return items[-limit:]


def summarize_traces(limit: int = 50) -> list[dict[str, Any]]:
    """Group recent spans into per-trace summaries, newest first.

    Args:
        limit: Maximum number of trace summaries to return.

    Returns:
        List of dicts with trace id, span count, root name, component and
        timing.
    """
    grouped: dict[str, dict[str, Any]] = {}
    for span in _INMEM_SPANS:
        trace_id = span.get("trace_id")
        if not trace_id:
            continue
        entry = grouped.setdefault(
            trace_id,
            {
                "trace_id": trace_id,
                "count": 0,
                "start_time": span.get("start_time"),
                "total_duration_ms": 0.0,
                "component": span.get("component"),
                "root_name": span.get("name"),
            },
        )
        entry["count"] += 1
        entry["total_duration_ms"] = round(
            entry["total_duration_ms"] + (span.get("duration_ms") or 0.0), 3
        )
        start = span.get("start_time")
        if start and (not entry["start_time"] or start < entry["start_time"]):
            entry["start_time"] = start
            entry["component"] = span.get("component")
            entry["root_name"] = span.get("name")
    ordered = sorted(grouped.values(), key=lambda e: e.get("start_time") or 0, reverse=True)
    for entry in ordered:
        start_ns = entry.get("start_time")
        if start_ns:
            entry["start_iso"] = datetime.fromtimestamp(start_ns / 1_000_000_000, tz=UTC).isoformat(
                timespec="seconds"
            )
        else:
            entry["start_iso"] = ""
    return ordered[:limit]


def clear_spans() -> None:
    """Empty the in-memory span store (mainly for tests)."""
    _INMEM_SPANS.clear()
