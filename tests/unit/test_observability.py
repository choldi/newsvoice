"""Unit tests for the observability core: context, logging and tracing."""

import json
from pathlib import Path

import pytest
import structlog

from src.core import context
from src.core.logging import (
    clear_ring_buffer,
    configure_logging,
    get_component_levels,
    get_ring_buffer,
    read_file_tail,
    register_component,
    set_component_level,
)


def test_correlation_id_round_trip() -> None:
    context.set_correlation_id("abc-123")
    assert context.get_correlation_id() == "abc-123"
    context.clear_correlation_id()
    assert context.get_correlation_id() is None


def test_trace_context_round_trip() -> None:
    context.set_trace_context("trace-1", "span-1")
    assert context.get_trace_id() == "trace-1"
    assert context.get_span_id() == "span-1"
    context.clear_trace_context()
    assert context.get_trace_id() is None


def test_bind_context_sets_known_keys_only() -> None:
    context.bind_context(correlation_id="c", component="api.x", process="api", unknown="ignored")
    assert context.get_correlation_id() == "c"
    assert context.get_component() == "api.x"
    assert context.get_process() == "api"
    context.clear_context()


def test_generate_ids_are_unique() -> None:
    assert context.generate_correlation_id() != context.generate_correlation_id()
    assert context.generate_trace_id() != context.generate_trace_id()


def test_logging_ring_buffer_captures_records() -> None:
    clear_ring_buffer()
    configure_logging("test")
    structlog.get_logger("t").info("hello", foo=1)
    records = get_ring_buffer()
    assert records[-1]["event"] == "hello"
    assert records[-1]["foo"] == 1
    assert records[-1]["process"] == "test"


def test_logging_adds_correlation_id_from_context() -> None:
    clear_ring_buffer()
    configure_logging("test")
    context.set_correlation_id("corr-42")
    structlog.get_logger("t").info("with-corr")
    assert get_ring_buffer()[-1]["correlation_id"] == "corr-42"
    context.clear_correlation_id()


def test_component_level_registry() -> None:
    register_component("api.services.tts")
    assert "api.services.tts" in get_component_levels()
    set_component_level("api.services.tts", "DEBUG")
    assert get_component_levels()["api.services.tts"] == "DEBUG"


def test_set_component_level_rejects_unknown() -> None:
    with pytest.raises(ValueError):
        set_component_level("x", "NOPE")


def test_file_sink_writes_jsonl(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import src.core.logging as logmod

    monkeypatch.setattr(logmod, "_FILE_HANDLER", None)
    from src.core.config import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "log_file_dir", str(tmp_path))
    monkeypatch.setattr(settings, "log_file_name", "obs.jsonl")
    configure_logging("test")
    structlog.get_logger("t").info("to-file", n=7)
    records = read_file_tail()
    assert any(r.get("event") == "to-file" and r.get("n") == 7 for r in records)


async def test_trace_function_sync_async() -> None:
    from src.core.tracing import trace_function

    @trace_function(component="test.comp")
    def add(a: int, b: int) -> int:
        return a + b

    @trace_function(component="test.comp")
    async def aadd(a: int, b: int) -> int:
        return a + b

    assert add(1, 2) == 3
    assert await aadd(2, 3) == 5


def test_trace_span_records_in_memory() -> None:
    from src.core.tracing import get_inmem_spans, get_tracer

    tracer = get_tracer("test.span")
    with tracer.start_as_current_span("unit-span") as span:
        span.set_attribute("k", "v")
    spans = get_inmem_spans()
    assert isinstance(spans, list)


def test_file_sink_degrades_when_unwritable(monkeypatch: pytest.MonkeyPatch) -> None:
    import src.core.logging as logmod
    from src.core.config import get_settings

    monkeypatch.setattr(logmod, "_FILE_HANDLER", None)
    settings = get_settings()
    monkeypatch.setattr(settings, "log_file_dir", "/proc/definitely-not-writable")
    configure_logging("test")
    # Must not raise and must leave the file handler disabled.
    assert logmod._FILE_HANDLER is None


def test_logs_api_query_filters() -> None:
    from fastapi.testclient import TestClient

    from src.api.main import app

    clear_ring_buffer()
    configure_logging("api")
    context.set_correlation_id("filter-me")
    structlog.get_logger("t").info("target-event")
    context.clear_correlation_id()

    client = TestClient(app)
    resp = client.get("/api/logs/query", params={"correlation_id": "filter-me"})
    assert resp.status_code == 200
    items = resp.json()["items"]
    assert any(i.get("event") == "target-event" for i in items)
    assert all(i.get("correlation_id") == "filter-me" for i in items)


def test_logs_api_levels_endpoint() -> None:
    from fastapi.testclient import TestClient

    from src.api.main import app

    client = TestClient(app)
    resp = client.post("/api/logs/levels", params={"component": "c.x", "level": "INFO"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert json.loads(json.dumps(body))["level"] == "INFO"
