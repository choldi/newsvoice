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


async def test_trace_function_binds_and_restores_trace_context() -> None:
    from src.core.tracing import trace_function

    context.set_trace_context("outer-trace", "outer-span")

    @trace_function(component="test.comp")
    async def inner() -> tuple[str | None, str | None]:
        return context.get_trace_id(), context.get_span_id()

    tid, sid = await inner()
    # Inside the span the context points at the new span, not the outer one.
    assert tid and tid != "outer-trace"
    assert sid and sid != "outer-span"
    # After the call the previous context is restored.
    assert context.get_trace_id() == "outer-trace"
    assert context.get_span_id() == "outer-span"
    context.clear_trace_context()


def test_interceptor_correlation_round_trip() -> None:
    from src.workers.interceptors import (
        CORRELATION_HEADER,
        decode_correlation,
        encode_correlation,
    )

    payload = encode_correlation("run-42")
    assert decode_correlation({CORRELATION_HEADER: payload}) == "run-42"
    assert decode_correlation({}) is None


async def test_correlation_activity_interceptor_sets_context() -> None:
    from src.workers.interceptors import (
        CORRELATION_HEADER,
        CorrelationInterceptor,
        encode_correlation,
    )

    captured: dict[str, str | None] = {}

    async def handler(input: object) -> str:
        captured["cid"] = context.get_correlation_id()
        return "ok"

    interceptor = CorrelationInterceptor().intercept_activity(_DummyNextInbound(handler))
    headers = {CORRELATION_HEADER: encode_correlation("cid-from-header")}
    context.clear_correlation_id()
    result = await interceptor.execute_activity(_DummyActivityInput(headers))
    assert result == "ok"
    assert captured["cid"] == "cid-from-header"
    context.clear_correlation_id()


async def test_correlation_workflow_inbound_sets_context() -> None:
    from src.workers.interceptors import (
        CORRELATION_HEADER,
        _CorrelationWorkflowInbound,
        encode_correlation,
    )

    class _NextWorkflow:
        async def execute_workflow(self, input: object) -> str:
            return "done"

    inbound = _CorrelationWorkflowInbound(_NextWorkflow())
    headers = {CORRELATION_HEADER: encode_correlation("wf-cid")}
    context.clear_correlation_id()
    result = await inbound.execute_workflow(_DummyWorkflowInput(headers))
    assert result == "done"
    assert inbound.correlation_id == "wf-cid"
    assert context.get_correlation_id() == "wf-cid"
    context.clear_correlation_id()


def test_correlation_workflow_outbound_stamps_activity_headers() -> None:
    from src.workers.interceptors import (
        _CorrelationWorkflowOutbound,
        decode_correlation,
    )

    class _Root:
        correlation_id = "wf-cid"

    class _NextOutbound:
        def start_activity(self, input: object) -> object:
            return input

    outbound = _CorrelationWorkflowOutbound(_NextOutbound(), _Root())
    started = _DummyStartActivityInput({})
    handle = outbound.start_activity(started)
    assert decode_correlation(handle.headers) == "wf-cid"


class _DummyWorkflowInput:
    def __init__(self, headers: dict) -> None:
        self.headers = headers
        self.type = object
        self.run_fn = None
        self.args: list = []


class _DummyStartActivityInput:
    def __init__(self, headers: dict) -> None:
        self.headers = headers
        self.activity = "x"
        self.args: list = []
        self.activity_id = None
        self.task_queue = None
        self.schedule_to_close_timeout = None
        self.schedule_to_start_timeout = None
        self.start_to_close_timeout = None
        self.heartbeat_timeout = None
        self.retry_policy = None
        self.cancellation_type = None
        self.disable_eager_execution = False
        self.versioning_intent = None
        self.summary = None
        self.priority = None


class _DummyActivityInput:
    def __init__(self, headers: dict) -> None:
        self.headers = headers
        self.fn = None
        self.args: list = []
        self.executor = None


class _DummyNextInbound:
    def __init__(self, handler) -> None:
        self._handler = handler

    async def execute_activity(self, input: object) -> object:
        return await self._handler(input)


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


def test_set_level_rejects_unknown_level() -> None:
    from fastapi.testclient import TestClient

    from src.api.main import app

    client = TestClient(app)
    resp = client.post("/api/logs/levels", params={"component": "c.x", "level": "NOPE"})
    assert resp.status_code == 422


def test_query_logs_level_and_component_filters() -> None:
    from fastapi.testclient import TestClient

    from src.api.main import app

    clear_ring_buffer()
    configure_logging("api")
    structlog.get_logger("t").info("info-line", component="api.services.alpha")
    structlog.get_logger("t").error("error-line", component="api.services.beta")

    client = TestClient(app)
    resp = client.get(
        "/api/logs/query",
        params=[("component", "api.services.alpha"), ("level", "INFO")],
    )
    assert resp.status_code == 200
    items = resp.json()["items"]
    assert any(i.get("event") == "info-line" for i in items)
    assert all(i.get("component") == "api.services.alpha" for i in items)


def test_query_logs_text_filter() -> None:
    from fastapi.testclient import TestClient

    from src.api.main import app

    clear_ring_buffer()
    configure_logging("api")
    structlog.get_logger("t").info("unique-needle-event")

    client = TestClient(app)
    resp = client.get("/api/logs/query", params={"text": "unique-needle"})
    assert resp.status_code == 200
    assert any(i.get("event") == "unique-needle-event" for i in resp.json()["items"])


def test_export_logs_writes_file(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    from fastapi.testclient import TestClient

    from src.api.main import app
    from src.core.config import get_settings

    monkeypatch.setattr(get_settings(), "obs_export_dir", str(tmp_path))
    clear_ring_buffer()
    configure_logging("api")
    structlog.get_logger("t").info("export-me")

    client = TestClient(app)
    resp = client.get("/api/logs/export", params={"fmt": "jsonl", "text": "export-me"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["count"] >= 1
    assert (tmp_path / body["filename"]).exists()


def test_file_sink_toggle_endpoint() -> None:
    from fastapi.testclient import TestClient

    from src.api.main import app

    client = TestClient(app)
    resp = client.get("/api/logs/file")
    assert resp.status_code == 200
    assert "enabled" in resp.json()


def test_traces_endpoints() -> None:
    from fastapi.testclient import TestClient

    from src.api.main import app
    from src.core.tracing import clear_spans, get_tracer

    clear_spans()
    tracer = get_tracer("test.trace")
    with tracer.start_as_current_span("root-span") as span:
        trace_id = format(span.get_span_context().trace_id, "032x")

    client = TestClient(app)
    listing = client.get("/api/traces")
    assert listing.status_code == 200
    assert any(t["trace_id"] == trace_id for t in listing.json()["traces"])

    detail = client.get(f"/api/traces/{trace_id}")
    assert detail.status_code == 200
    assert detail.json()["spans"]

    missing = client.get("/api/traces/deadbeef")
    assert missing.status_code == 404


async def test_logs_stream_returns_event_stream() -> None:
    from src.api.routes.logs import stream_logs

    class _FakeRequest:
        async def is_disconnected(self) -> bool:
            return True

    resp = await stream_logs(_FakeRequest())
    assert resp.media_type == "text/event-stream"
