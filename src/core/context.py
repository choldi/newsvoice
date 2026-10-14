"""Context variables for correlation IDs, tracing, and request-scoped metadata."""

from __future__ import annotations

import uuid
from contextvars import ContextVar
from typing import Any

correlation_id_var: ContextVar[str | None] = ContextVar("correlation_id", default=None)
trace_id_var: ContextVar[str | None] = ContextVar("trace_id", default=None)
span_id_var: ContextVar[str | None] = ContextVar("span_id", default=None)
component_var: ContextVar[str | None] = ContextVar("component", default=None)
process_var: ContextVar[str | None] = ContextVar("process", default=None)
execution_id_var: ContextVar[str | None] = ContextVar("execution_id", default=None)
span_name_var: ContextVar[str | None] = ContextVar("span_name", default=None)


def generate_correlation_id() -> str:
    return str(uuid.uuid4())


def generate_trace_id() -> str:
    return uuid.uuid4().hex


def get_correlation_id() -> str | None:
    return correlation_id_var.get()


def set_correlation_id(cid: str | None) -> None:
    correlation_id_var.set(cid)


def clear_correlation_id() -> None:
    correlation_id_var.set(None)


def get_trace_id() -> str | None:
    return trace_id_var.get()


def get_span_id() -> str | None:
    return span_id_var.get()


def set_trace_context(trace_id: str | None, span_id: str | None = None) -> None:
    if trace_id is not None:
        trace_id_var.set(trace_id)
    if span_id is not None:
        span_id_var.set(span_id)


def clear_trace_context() -> None:
    trace_id_var.set(None)
    span_id_var.set(None)


def get_component() -> str | None:
    return component_var.get()


def set_component(name: str | None) -> None:
    component_var.set(name)


def get_process() -> str | None:
    return process_var.get()


def set_process(name: str | None) -> None:
    process_var.set(name)


def bind_context(**kwargs: Any) -> None:
    for k, v in kwargs.items():
        if k == "correlation_id" and v is not None:
            set_correlation_id(str(v))
        elif k == "trace_id" and v is not None:
            trace_id_var.set(str(v))
        elif k == "span_id" and v is not None:
            span_id_var.set(str(v))
        elif k == "component" and v is not None:
            set_component(str(v))
        elif k == "process" and v is not None:
            set_process(str(v))
        elif k == "execution_id" and v is not None:
            execution_id_var.set(str(v))
        elif k == "span_name" and v is not None:
            span_name_var.set(str(v))


def clear_context() -> None:
    correlation_id_var.set(None)
    trace_id_var.set(None)
    span_id_var.set(None)
    component_var.set(None)
    process_var.set(None)
    execution_id_var.set(None)
    span_name_var.set(None)
