"""Thin Modal client for narration TTS (Phase 7.8).

Imported lazily by :meth:`NarrationTTSService._post_modal_tts` so the local
narration path never needs the ``modal`` package or credentials: only the
Modal code path touches this module.
"""

from __future__ import annotations

from src.core.config import settings


async def modal_tts(
    *,
    text: str,
    voice_ref: str,
    language: str,
    speed: float = 1.0,
    emotion: str | None = None,
) -> bytes:
    """Call the Modal TTS function and return WAV bytes.

    Args:
        text: Speech text for the turn.
        voice_ref: Reference voice name from the catalog.
        language: ISO 639-1 language code.
        speed: Speaking rate in [0.5, 2.0].
        emotion: Optional emotion tag, accepted for ZONOS2 parity.

    Returns:
        The WAV payload returned by Modal.

    Raises:
        RuntimeError: When Modal is disabled or credentials are missing.
        Exception: Whatever the Modal call raises, for the caller to wrap.
    """
    if not settings.modal_enabled:
        raise RuntimeError("modal is disabled")
    if not settings.modal_token_id or not settings.modal_token_secret:
        raise RuntimeError("modal credentials are not configured")

    import modal

    function = modal.Function.from_name(settings.modal_app_name, "generate_tts")
    return await function.remote.aio(
        text=text,
        voice_ref=voice_ref,
        language=language,
        speed=speed,
        emotion=emotion,
    )


async def modal_zonos2_tts(
    *,
    text: str,
    voice_ref: str,
    language: str,
    speed: float = 1.0,
    emotion: str | None = None,
) -> bytes:
    """Call the Modal ZONOS2 function and return WAV bytes.

    Args:
        text: Speech text for the turn.
        voice_ref: Reference voice name from the catalog.
        language: ISO 639-1 language code.
        speed: Speaking rate multiplier; native ZONOS2 control.
        emotion: Optional emotion tag (happy, sad, angry, surprised).

    Returns:
        The WAV payload returned by Modal, at 44.1 kHz.

    Raises:
        RuntimeError: When Modal is disabled or credentials are missing.
        Exception: Whatever the Modal call raises, for the caller to wrap.
    """
    if not settings.modal_enabled:
        raise RuntimeError("modal is disabled")
    if not settings.modal_token_id or not settings.modal_token_secret:
        raise RuntimeError("modal credentials are not configured")

    import modal

    function = modal.Function.from_name(settings.modal_app_name, "generate_tts_zonos2")
    return await function.remote.aio(
        text=text, voice_ref=voice_ref, language=language, speed=speed, emotion=emotion
    )


async def upload_voice(name: str, wav: bytes) -> None:
    """Upload a reference voice to the Modal Volume.

    Args:
        name: Reference voice name.
        wav: Reference WAV bytes.

    Raises:
        RuntimeError: When Modal is disabled or credentials are missing.
        Exception: Whatever the Modal call raises.
    """
    if not settings.modal_enabled:
        raise RuntimeError("modal is disabled")
    if not settings.modal_token_id or not settings.modal_token_secret:
        raise RuntimeError("modal credentials are not configured")

    import modal

    function = modal.Function.from_name(settings.modal_app_name, "upload_voice")
    await function.remote.aio(name=name, wav=wav)
