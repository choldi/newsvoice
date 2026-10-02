"""Temporal client and logging helpers shared by every worker entry point.

Deliberately small: it must stay importable from a machine that only has
``temporalio``, ``structlog`` and ``pydantic-settings`` installed (the voice
design box), so it cannot pull in the database, the RSS stack or any service.
"""

from __future__ import annotations

import logging

import structlog
from temporalio.client import Client

from src.core.config import settings

#: The SDK logs activities, workflows and the client under this hierarchy.
TEMPORAL_LOGGER_NAME = "temporalio"


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
        handler.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
        temporal_logger.addHandler(handler)
    for handler in temporal_logger.handlers:
        handler.setLevel(level)


def configure_logging() -> None:
    """Configure structlog and the Temporal SDK's loggers for the process.

    structlog carries the application's own JSON lines; the Temporal SDK
    carries the activity and workflow ones. Both have to be wired or half of
    the worker's output never leaves the container.
    """
    structlog.configure(
        processors=[
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(settings.log_level),
    )
    configure_temporal_logging()


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
    )
