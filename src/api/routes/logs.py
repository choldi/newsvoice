"""Logs and traces API endpoints.

Serves the observability UI: per-component level control, filtered log
queries, a live Server-Sent Events tail, JSONL/TXT export, the shared file
sink toggle, and the recent-traces views backed by the in-memory spans.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import StreamingResponse

from src.core import logging as logging_ext
from src.core import tracing

router = APIRouter(tags=["logs"])

#: SSE polling cadence and keep-alive period (number of idle polls).
_STREAM_POLL_SECONDS = 1.0
_STREAM_HEARTBEAT_POLLS = 15


@router.get("/logs/levels")
async def get_levels() -> dict:
    """Return the per-component effective log levels."""
    return {"status": "ok", "levels": logging_ext.get_component_levels()}


@router.get("/logs/components", include_in_schema=False)
async def get_components() -> dict:
    """Return the known components for the UI's pickers."""
    return {"status": "ok", "components": logging_ext.list_components()}


@router.post("/logs/levels")
async def set_level(
    component: str = Query(...),
    level: str = Query(...),
) -> dict:
    """Set the effective level for a component."""
    try:
        logging_ext.set_component_level(component, level)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"status": "ok", "component": component, "level": level.upper()}


@router.get("/logs/query")
async def query_logs(
    limit: int = Query(default=200, ge=1, le=2000),
    offset: int = Query(default=0, ge=0),
    level: list[str] | None = Query(default=None),
    component: list[str] | None = Query(default=None),
    text: str | None = Query(default=None),
    correlation_id: str | None = Query(default=None),
    trace_id: str | None = Query(default=None),
    module: str | None = Query(default=None),
    func: str | None = Query(default=None),
    since_id: int | None = Query(default=None),
    include_file: bool = Query(default=True),
) -> dict:
    """Return filtered, paginated log records (newest last)."""
    items = logging_ext.query_logs(
        limit=limit,
        offset=offset,
        levels=level,
        components=component,
        text=text,
        correlation_id=correlation_id,
        trace_id=trace_id,
        module=module,
        func=func,
        since_id=since_id,
        include_file=include_file,
    )
    return {"status": "ok", "count": len(items), "items": items}


@router.get("/logs/stream")
async def stream_logs(
    request: Request,
    level: list[str] | None = Query(default=None),
    component: list[str] | None = Query(default=None),
    text: str | None = Query(default=None),
    correlation_id: str | None = Query(default=None),
    trace_id: str | None = Query(default=None),
    module: str | None = Query(default=None),
    func: str | None = Query(default=None),
) -> StreamingResponse:
    """Stream new log records as Server-Sent Events.

    Existing records are primed as already-seen so a freshly connected client
    only receives records emitted after it subscribed (the initial history is
    fetched through ``/logs/query``).
    """

    async def _events() -> AsyncIterator[str]:
        seen: set[tuple] = set()
        for record in logging_ext.query_logs(limit=2000, include_file=True):
            seen.add((record.get("process"), record.get("id")))
        idle = 0
        while True:
            if await request.is_disconnected():
                break
            records = logging_ext.query_logs(
                limit=500,
                levels=level,
                components=component,
                text=text,
                correlation_id=correlation_id,
                trace_id=trace_id,
                module=module,
                func=func,
                include_file=True,
            )
            emitted = False
            for record in records:
                key = (record.get("process"), record.get("id"))
                if key in seen:
                    continue
                seen.add(key)
                emitted = True
                event_id = f"{record.get('process')}:{record.get('id')}"
                payload = json.dumps(record, ensure_ascii=True, default=str)
                yield f"id: {event_id}\ndata: {payload}\n\n"
            idle = 0 if emitted else idle + 1
            if idle >= _STREAM_HEARTBEAT_POLLS:
                idle = 0
                yield ": keep-alive\n\n"
            await asyncio.sleep(_STREAM_POLL_SECONDS)

    return StreamingResponse(
        _events(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get("/logs/export")
async def export_logs(
    fmt: str = Query(default="jsonl", pattern="^(jsonl|txt)$"),
    limit: int = Query(default=2000, ge=1, le=20000),
    level: list[str] | None = Query(default=None),
    component: list[str] | None = Query(default=None),
    text: str | None = Query(default=None),
    correlation_id: str | None = Query(default=None),
    trace_id: str | None = Query(default=None),
    module: str | None = Query(default=None),
    func: str | None = Query(default=None),
) -> dict:
    """Export the current filtered view to a file under ``obs_export_dir``."""
    records = logging_ext.query_logs(
        limit=limit,
        levels=level,
        components=component,
        text=text,
        correlation_id=correlation_id,
        trace_id=trace_id,
        module=module,
        func=func,
        include_file=True,
    )
    result = logging_ext.export_logs(records, fmt=fmt)
    return {"status": "ok", **result}


@router.get("/logs/file")
async def get_file_sink() -> dict:
    """Report whether the shared JSONL file sink is active."""
    return {"status": "ok", **logging_ext.file_sink_state()}


@router.post("/logs/file/redirect")
async def set_file_sink(enabled: bool = Query(...)) -> dict:
    """Enable or disable the shared JSONL file sink at runtime."""
    return {"status": "ok", **logging_ext.set_file_sink_enabled(enabled)}


@router.get("/traces")
async def list_traces(
    limit: int = Query(default=50, ge=1, le=500),
) -> dict:
    """Return recent traces grouped by trace id (newest first)."""
    traces = tracing.summarize_traces(limit=limit)
    return {"status": "ok", "count": len(traces), "traces": traces}


@router.get("/traces/{trace_id}")
async def get_trace(trace_id: str) -> dict:
    """Return the span timeline and linked logs for one trace id."""
    spans = tracing.get_inmem_spans(trace_id=trace_id, limit=1000)
    if not spans:
        raise HTTPException(status_code=404, detail="unknown trace id")
    logs = logging_ext.query_logs(trace_id=trace_id, limit=500, include_file=True)
    return {"status": "ok", "trace_id": trace_id, "spans": spans, "logs": logs}
