"""Logs and traces API endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Query

from src.core import logging as logging_ext

router = APIRouter(tags=["logs"])


@router.get("/logs/levels")
async def get_levels():
    return {"status": "ok", "levels": logging_ext.get_component_levels()}


@router.post("/logs/levels")
async def set_level(component: str = Query(...), level: str = Query(...)):
    logging_ext.set_component_level(component, level)
    return {"status": "ok", "component": component, "level": level}


@router.get("/logs/query")
async def query_logs(
    limit: int = Query(default=100, ge=1, le=1000),
    since_id: str | None = Query(default=None),
    since_ts: str | None = Query(default=None),
    level: list[str] | None = Query(default=None),
    component: list[str] | None = Query(default=None),
    text: str | None = Query(default=None),
    correlation_id: str | None = Query(default=None),
    trace_id: str | None = Query(default=None),
):
    buffer = logging_ext.get_ring_buffer()
    # simple filtering
    items = buffer[-limit:] if limit else buffer
    result = []
    for it in items:
        ok = True
        if correlation_id and it.get("correlation_id") != correlation_id:
            ok = False
        if trace_id and it.get("trace_id") != trace_id:
            ok = False
        if ok:
            result.append(it)
    return {"status": "ok", "count": len(result), "items": result}
