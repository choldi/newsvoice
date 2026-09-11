"""API routes package."""

from src.api.routes.cluster import router as cluster_router
from src.api.routes.ingest import router as ingest_router
from src.api.routes.mvp import router as mvp_router
from src.api.routes.summarize import router as summarize_router
from src.api.routes.tts import router as tts_router

__all__ = [
    "cluster_router",
    "ingest_router",
    "mvp_router",
    "summarize_router",
    "tts_router",
]
