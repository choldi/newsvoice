"""Routes package initialization."""

from src.api.routes.ingest import router as ingest_router
from src.api.routes.mvp import router as mvp_router
from src.api.routes.summarize import router as summarize_router

__all__ = ["ingest_router", "mvp_router", "summarize_router"]
