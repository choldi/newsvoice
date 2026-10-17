"""Unit tests for the worker logging bootstrap (Temporal SDK loggers)."""

import logging

import pytest

from src.core.config import settings
from src.workers.client import TEMPORAL_LOGGER_NAME, configure_temporal_logging


def _temporal_logger() -> logging.Logger:
    return logging.getLogger(TEMPORAL_LOGGER_NAME)


def test_temporal_loggers_are_wired_to_a_stream_handler() -> None:
    configure_temporal_logging()

    temporal_logger = _temporal_logger()

    assert temporal_logger.level == logging.INFO
    assert temporal_logger.propagate is False
    assert any(isinstance(handler, logging.StreamHandler) for handler in temporal_logger.handlers)


def test_info_records_reach_the_handler() -> None:
    configure_temporal_logging()
    received: list[str] = []
    capture = logging.Handler()
    capture.emit = lambda record: received.append(record.getMessage())  # type: ignore[method-assign]
    temporal_logger = _temporal_logger()
    temporal_logger.addHandler(capture)
    try:
        temporal_logger.info("activity progress")
    finally:
        temporal_logger.removeHandler(capture)

    assert received == ["activity progress"]


def test_honours_the_configured_log_level(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "log_level", "WARNING")

    configure_temporal_logging()

    assert _temporal_logger().level == logging.WARNING


def test_repeated_calls_do_not_stack_handlers() -> None:
    configure_temporal_logging()
    before = list(_temporal_logger().handlers)

    configure_temporal_logging()
    configure_temporal_logging()

    assert _temporal_logger().handlers == before
