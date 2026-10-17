"""Observation middleware: correlation IDs and request access logs (Phase 1).

Boundary of the whole tracing design. Every inbound HTTP request gets a
``correlation_id`` (from the ``X-Correlation-ID`` header when the caller already
has one, otherwise generated). The id is stored in a contextvar so every log
line and span emitted while handling the request carries it, echoed back in the
response headers, and it is what the UI uses to show one call across modules.
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable

import structlog
from fastapi import Request, Response

from src.core import context

logger = structlog.get_logger(__name__)

CORRELATION_HEADER = "X-Correlation-ID"
_TRACEPARENT_HEADER = "traceparent"


async def observability_middleware(
    request: Request, call_next: Callable[[Request], Awaitable[Response]]
) -> Response:
    """Bind correlation/trace context and log one access line per request.

    Args:
        request: Incoming request.
        call_next: Downstream ASGI handler.

    Returns:
        The response with the correlation id echoed back.
    """
    incoming = request.headers.get(CORRELATION_HEADER)
    correlation_id = incoming or context.generate_correlation_id()
    context.set_correlation_id(correlation_id)
    context.set_component("api.middleware")

    traceparent = request.headers.get(_TRACEPARENT_HEADER)
    if traceparent:
        parts = traceparent.split("-")
        if len(parts) >= 3:
            context.set_trace_context(parts[1], parts[2])

    started = time.perf_counter()
    try:
        response = await call_next(request)
    except Exception:
        duration_ms = (time.perf_counter() - started) * 1000
        logger.exception(
            "request_failed",
            method=request.method,
            path=request.url.path,
            duration_ms=round(duration_ms, 2),
        )
        raise
    duration_ms = (time.perf_counter() - started) * 1000
    response.headers[CORRELATION_HEADER] = correlation_id
    logger.info(
        "request",
        method=request.method,
        path=request.url.path,
        status_code=response.status_code,
        duration_ms=round(duration_ms, 2),
    )
    return response
