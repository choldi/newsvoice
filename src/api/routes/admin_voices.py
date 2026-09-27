"""Admin voices routes for the Phase 6 GUI.

Lists OmniVoice stock/cloned voices and clones a voice from a reference audio
file. Audio uploads arrive base64-encoded in JSON to avoid a `python-multipart`
runtime dependency; the VoicesService forwards them to OmniVoice as multipart.
"""

import base64

import structlog
from fastapi import APIRouter, HTTPException, status

from src.api.models.admin import VoiceCloneIn, VoiceCloneOut, VoicesOut
from src.api.services.voices import VoicesService

logger = structlog.get_logger(__name__)

router = APIRouter(tags=["admin-voices"])


@router.get("/admin/voices", response_model=VoicesOut)
async def list_stock_voices(language: str | None = None) -> VoicesOut:
    """List available stock voices, optionally filtered by language.

    Args:
        language: ISO 639-1 language filter (e.g. 'es').

    Returns:
        VoicesOut with the stock voice list.
    """
    service = VoicesService()
    voices = await service.list_stock(language=language or None)
    return VoicesOut(voices=voices)


@router.get("/admin/voices/cloned", response_model=VoicesOut)
async def list_cloned_voices() -> VoicesOut:
    """List cloned voices.

    Returns:
        VoicesOut with the cloned voice list.
    """
    service = VoicesService()
    voices = await service.list_cloned()
    return VoicesOut(voices=voices)


@router.post("/admin/voices/clone", response_model=VoiceCloneOut)
async def clone_voice(payload: VoiceCloneIn) -> VoiceCloneOut:
    """Clone a voice from a base64-encoded reference audio file.

    Args:
        payload: VoiceCloneIn with name, language, and base64 audio.

    Returns:
        VoiceCloneOut with the cloning response.

    Raises:
        HTTPException: 422 on invalid base64, 502 on upstream errors.
    """
    try:
        reference_audio = base64.b64decode(payload.audio_base64, validate=True)
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Invalid base64 audio: {exc}",
        ) from exc
    try:
        service = VoicesService()
        voice = await service.clone_voice(
            payload.name,
            payload.language,
            reference_audio,
            filename=payload.filename,
            ref_text=payload.ref_text,
        )
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Voice cloning failed: {exc}",
        ) from exc
    return VoiceCloneOut(voice=voice)
