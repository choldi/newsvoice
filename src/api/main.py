"""FastAPI application entry point."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from src.api.auth import admin_middleware
from src.api.db.database import run_migrations
from src.api.observability import observability_middleware
from src.api.routes import (
    admin_collections_router,
    admin_documents_router,
    admin_lessons_router,
    admin_narration_router,
    admin_runs_router,
    admin_schedules_router,
    admin_voices_router,
    audio_link_router,
    cluster_router,
    ingest_router,
    mvp_router,
    summarize_router,
    summarize_url_router,
    tts_router,
    voice_staging_router,
)
from src.api.routes.logs import router as logs_router
from src.api.routes_ui import router as ui_router
from src.core.logging import configure_logging

configure_logging(process_name="api")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Application lifespan: run migrations on startup."""
    await run_migrations()
    yield


app = FastAPI(
    title="News Engine API",
    description="AI-curated news digest generation (5 main stories + 5 headlines)",
    version="0.5.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.middleware("http")(admin_middleware)
app.middleware("http")(observability_middleware)

# Static assets (bundled htmx.js for the self-hosted GUI)
app.mount("/static", StaticFiles(directory=Path(__file__).parent / "static"), name="static")

# Routes
app.include_router(ingest_router, prefix="/api")
app.include_router(cluster_router, prefix="/api")
app.include_router(summarize_router, prefix="/api")
app.include_router(summarize_url_router, prefix="/api")
app.include_router(mvp_router, prefix="/api")
app.include_router(tts_router, prefix="/api")
app.include_router(admin_collections_router, prefix="/api")
app.include_router(admin_voices_router, prefix="/api")
app.include_router(admin_documents_router, prefix="/api")
app.include_router(admin_lessons_router, prefix="/api")
app.include_router(admin_runs_router, prefix="/api")
app.include_router(admin_schedules_router, prefix="/api")
app.include_router(admin_narration_router, prefix="/api")
app.include_router(logs_router, prefix="/api")
app.include_router(voice_staging_router, prefix="/api")
app.include_router(audio_link_router, prefix="/api")
app.include_router(ui_router)


@app.get("/health", tags=["health"])
async def health_check() -> dict[str, str]:
    """Health check endpoint."""
    return {"status": "ok", "service": "news-engine-api"}


def _instrument_fastapi() -> None:
    """Auto-instrument every route with an OpenTelemetry server span.

    Called once after all routers are registered; a missing optional package
    leaves the app working with the manual spans only.
    """
    from src.core.config import settings

    if not settings.trace_enabled:
        return
    try:
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

        from src.core.tracing import get_tracer

        get_tracer()
        FastAPIInstrumentor.instrument_app(app)
    except Exception:  # pragma: no cover - optional instrumentation package
        return


_instrument_fastapi()
