"""Temporal client and logging helpers shared by every worker entry point.

Deliberately small: it must stay importable from a machine that only has
``temporalio``, ``structlog`` and ``pydantic-settings`` installed (the voice
design box), so it cannot pull in the database, the RSS stack or any service.
"""

from __future__ import annotations

import logging
from typing import Any

import structlog
from temporalio.client import Client

from src.core.config import settings

#: The SDK logs activities, workflows and the client under this hierarchy.
TEMPORAL_LOGGER_NAME = "temporalio"


class _ContextFilter(logging.Filter):
    """Stamp the current correlation/trace ids onto SDK log records."""

    def filter(self, record: logging.LogRecord) -> bool:
        """Attach the observability contextvars to ``record``."""
        from src.core import context

        record.correlation_id = context.get_correlation_id() or "-"
        record.trace_id = context.get_trace_id() or "-"
        return True


def configure_temporal_logging() -> None:
    """Route the Temporal SDK's loggers to a stream handler of our own.

    The SDK writes through the standard ``logging`` module and, with no
    handler installed, falls back to WARNING: every INFO line an activity or a
    workflow emits would be missing from journalctl and docker logs. Level and
    handler are taken from settings, and the call is idempotent so entry
    points and tests can run it repeatedly.

    The SDK's loggers accept a plain message only — any other keyword argument
    reaches ``logging.Logger._log`` and raises ``TypeError`` — so call sites
    pass structured data in an f-string.
    """
    level = getattr(logging, str(settings.log_level).upper(), logging.INFO)
    temporal_logger = logging.getLogger(TEMPORAL_LOGGER_NAME)
    temporal_logger.setLevel(level)
    temporal_logger.propagate = False
    if not temporal_logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(
            logging.Formatter("%(levelname)s %(name)s [%(correlation_id)s]: %(message)s")
        )
        handler.addFilter(_ContextFilter())
        temporal_logger.addHandler(handler)
    for handler in temporal_logger.handlers:
        handler.setLevel(level)


def configure_logging() -> None:
    """Configure structlog and the Temporal SDK's loggers for the process.

    structlog carries the application's own JSON lines; the Temporal SDK
    carries the activity and workflow ones. Both have to be wired or half of
    the worker's output never leaves the container. The structlog side is the
    shared observability configuration so worker records carry the same
    correlation/trace metadata as the API's.
    """
    from src.core.logging import configure_logging as _configure_observability

    _configure_observability(process_name="worker")
    configure_temporal_logging()


def build_interceptors() -> list[Any]:
    """Return the client/worker interceptors for observability.

    The OpenTelemetry interceptor creates spans and propagates the W3C trace
    context; the correlation interceptor carries the business id. Both are
    best-effort: when tracing is disabled, or the contrib package is missing,
    the client still connects (the correlation id alone is enough to follow a
    run in the logs).

    Returns:
        Interceptors to hand to ``Client.connect`` (the worker inherits any
        that also implement ``temporalio.worker.Interceptor``).
    """
    if not settings.trace_enabled:
        return []
    interceptors: list[Any] = []
    try:
        # Install the local in-memory TracerProvider before the interceptor
        # resolves its tracer, otherwise spans go to the default no-op provider.
        from temporalio.contrib.opentelemetry import TracingInterceptor

        from src.core.tracing import get_tracer

        get_tracer()
        interceptors.append(TracingInterceptor(always_create_workflow_spans=True))
    except Exception:  # pragma: no cover - optional contrib package
        structlog.get_logger(__name__).warning("otel_interceptor_unavailable")
    from src.workers.interceptors import CorrelationInterceptor

    interceptors.append(CorrelationInterceptor())
    return interceptors


async def build_client(namespace: str | None = None) -> Client:
    """Connect to the Temporal server.

    Args:
        namespace: Namespace to connect to. Defaults to the configured one, so
            the voice workers are the only callers that pass their own.

    Returns:
        A Temporal Client connected to the given server/namespace.
    """
    target = namespace or settings.temporal_namespace
    structlog.get_logger(__name__).info(
        "connecting_to_temporal",
        host=settings.temporal_host,
        namespace=target,
    )
    return await Client.connect(
        settings.temporal_host,
        namespace=target,
        interceptors=build_interceptors(),
    )
