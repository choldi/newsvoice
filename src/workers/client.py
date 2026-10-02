"""Temporal client and logging helpers shared by every worker entry point.

Deliberately small: it must stay importable from a machine that only has
``temporalio``, ``structlog`` and ``pydantic-settings`` installed (the voice
design box), so it cannot pull in the database, the RSS stack or any service.
"""

from __future__ import annotations

import structlog
from temporalio.client import Client

from src.core.config import settings


def configure_logging() -> None:
    """Configure structlog with JSON output for the worker process."""
    structlog.configure(
        processors=[
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(settings.log_level),
    )


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
