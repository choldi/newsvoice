"""FastAPI main application entry point."""

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI

from src.api.db.database import run_migrations
from src.api.routes.ingest import router as ingest_router
from src.api.routes.mvp import router as mvp_router
from src.api.routes.summarize import router as summarize_router

logger = structlog.get_logger()


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Lifespan context manager to handle application startup and shutdown tasks."""
    logger.info("application_startup_running_migrations")
    await run_migrations()
    yield
    logger.info("application_shutdown")


app = FastAPI(
    title="AI News Digest Engine API",
    description="Microservices API for AI-curated news summarization and orchestration",
    version="0.2.0",
    lifespan=lifespan,
)

# Include route routers
app.include_router(ingest_router)
app.include_router(summarize_router)
app.include_router(mvp_router)


@app.get("/health", tags=["health"])
async def health_check() -> dict[str, str]:
    """Health check endpoint to verify API operation.

    Returns:
        JSON response with health status.
    """
    return {"status": "ok", "service": "news-engine-api"}
