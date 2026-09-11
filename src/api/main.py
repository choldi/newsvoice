"""FastAPI application entry point."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from src.api.db.database import run_migrations
from src.api.routes import (
    cluster_router,
    ingest_router,
    mvp_router,
    summarize_router,
    tts_router,
)
from src.core.config import settings

structlog.configure(
    processors=[
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.JSONRenderer(),
    ],
    wrapper_class=structlog.make_filtering_bound_logger(settings.log_level),
)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Application lifespan: run migrations on startup."""
    await run_migrations()
    yield


app = FastAPI(
    title="News Engine API",
    description="AI-curated news digest generation (5 main stories + 5 headlines)",
    version="0.4.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Routes
app.include_router(ingest_router, prefix="/api")
app.include_router(cluster_router, prefix="/api")
app.include_router(summarize_router, prefix="/api")
app.include_router(mvp_router, prefix="/api")
app.include_router(tts_router, prefix="/api")


@app.get("/health", tags=["health"])
async def health_check() -> dict[str, str]:
    """Health check endpoint."""
    return {"status": "ok", "service": "news-engine-api"}
