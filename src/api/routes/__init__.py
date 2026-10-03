"""API routes package."""

from src.api.routes.admin_collections import router as admin_collections_router
from src.api.routes.admin_documents import router as admin_documents_router
from src.api.routes.admin_lessons import router as admin_lessons_router
from src.api.routes.admin_narration import router as admin_narration_router
from src.api.routes.admin_runs import router as admin_runs_router
from src.api.routes.admin_schedules import router as admin_schedules_router
from src.api.routes.admin_voices import router as admin_voices_router
from src.api.routes.audio_link import router as audio_link_router
from src.api.routes.cluster import router as cluster_router
from src.api.routes.ingest import router as ingest_router
from src.api.routes.mvp import router as mvp_router
from src.api.routes.summarize import router as summarize_router
from src.api.routes.summarize_url import router as summarize_url_router
from src.api.routes.tts import router as tts_router
from src.api.routes.voice_staging import router as voice_staging_router

__all__ = [
    "admin_collections_router",
    "admin_lessons_router",
    "admin_documents_router",
    "admin_narration_router",
    "admin_runs_router",
    "admin_schedules_router",
    "admin_voices_router",
    "audio_link_router",
    "cluster_router",
    "ingest_router",
    "mvp_router",
    "summarize_router",
    "summarize_url_router",
    "tts_router",
    "voice_staging_router",
]
