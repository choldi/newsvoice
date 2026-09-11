"""Temporal activities wrapping the news-engine services."""

from typing import Any

from temporalio import activity

from src.api.models.article import DigestIn
from src.api.models.tts import TTSIn
from src.api.services.digest import DigestService
from src.api.services.tts import TTSService


@activity.defn
async def generate_digest_activity(payload: dict[str, Any]) -> dict[str, Any]:
    """Generate the AI news digest from RSS articles/clusters.

    Args:
        payload: Dict matching the DigestIn schema (articles and/or clusters).

    Returns:
        Dict with status, digest markdown, and article_count.
    """
    activity.logger.info("Generating digest")
    digest_in = DigestIn.model_validate(payload)
    result = await DigestService().generate_digest(digest_in)
    return result.model_dump()


@activity.defn
async def generate_tts_activity(digest: dict[str, Any]) -> dict[str, Any]:
    """Convert the digest markdown into MP3 audio.

    Args:
        digest: Dict produced by generate_digest_activity (must contain 'digest').

    Returns:
        Dict with audio_path, duration_estimate, cached, voice, and char_count.
    """
    activity.logger.info("Generating TTS audio")
    tts_in = TTSIn(markdown=digest["digest"])
    result = await TTSService().generate_tts(tts_in)
    return result.model_dump()
