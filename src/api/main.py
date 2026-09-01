"""FastAPI main application entry point."""

import structlog
from fastapi import FastAPI

from src.api.routes.mvp import router as mvp_router

logger = structlog.get_logger()

app = FastAPI(
    title="AI News Digest Engine API",
    description="Microservices API for AI-curated news summarization and orchestration",
    version="0.1.0",
)

# Include route routers
app.include_router(mvp_router)


@app.get("/health", tags=["health"])
async def health_check() -> dict[str, str]:
    """Health check endpoint to verify API operation.

    Returns:
        JSON response with health status.
    """
    return {"status": "ok", "service": "news-engine-api"}
