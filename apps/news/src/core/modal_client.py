"""Thin Modal client for narration TTS (Phase 7.8).

Imported lazily by :meth:`NarrationTTSService._post_modal_tts` so the local
narration path never needs the ``modal`` package or credentials: only the
Modal code path touches this module.
"""

from __future__ import annotations

from src.core.config import settings
from src.core.modal_cost import measure
from src.core.tracing import trace_function


@trace_function(component="modal.client")
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

    # Class methods resolve through modal.Cls: bare Function.from_name(...).
    # remote.aio fails at invocation with "Function not found" (verified
    # against the deployed app); attribute hydration alone does not catch it.
    tts = modal.Cls.from_name(settings.modal_app_name, "QwenTTS")()
    audio, _cost = await measure(
        tts.generate_tts.remote.aio(
            text=text,
            voice_ref=voice_ref,
            language=language,
            speed=speed,
            emotion=emotion,
        ),
        backend="qwen3_tts",
    )
    return audio


@trace_function(component="modal.client")
async def design_voice(
    *,
    segments: list[str],
    instruct: str,
    language: str,
    seed: int | None = None,
    temperature: float = 0.7,
    top_p: float = 0.9,
) -> tuple[bytes, int]:
    """Call the Modal VoiceDesign function and return WAV bytes plus sample rate.

    Phase 10.4: the per-operation switch behind the "Ejecutar en Modal"
    checkbox. The local GPU worker stays the default; this path only runs
    when the caller explicitly asked for Modal and the guard below passes.

    Args:
        segments: Reference text segments, spoken in order.
        instruct: Natural-language persona description.
        language: ISO 639-1 language code.
        seed: RNG seed for a reproducible voice.
        temperature: Sampling temperature (mirrors ``QWEN_TTS_DESIGN_TEMPERATURE``).
        top_p: Nucleus sampling (mirrors ``QWEN_TTS_DESIGN_TOP_P``).

    Returns:
        Tuple of ``(wav_bytes, sample_rate)``.

    Raises:
        RuntimeError: When Modal is disabled or credentials are missing.
        Exception: Whatever the Modal call raises, for the caller to wrap.
    """
    if not settings.modal_enabled:
        raise RuntimeError("modal is disabled")
    if not settings.modal_token_id or not settings.modal_token_secret:
        raise RuntimeError("modal credentials are not configured")

    import modal

    design = modal.Cls.from_name(settings.modal_app_name, "VoiceDesignModel")()
    wav, sample_rate = await measure(
        design.design_voice.remote.aio(
            segments=segments,
            instruct=instruct,
            language=language,
            seed=seed,
            temperature=temperature,
            top_p=top_p,
        ),
        backend="design",
    )
    return wav, sample_rate


@trace_function(component="modal.client")
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

    zonos = modal.Cls.from_name(settings.modal_app_name, "Zonos2TTS")()
    audio, _cost = await measure(
        zonos.generate_tts_zonos2.remote.aio(
            text=text, voice_ref=voice_ref, language=language, speed=speed, emotion=emotion
        ),
        backend="zonos2",
    )
    return audio


@trace_function(component="modal.client")
async def modal_voxcpm2_tts(
    *,
    text: str,
    voice_ref: str,
    language: str,
    speed: float = 1.0,
    emotion: str | None = None,
) -> bytes:
    """Call the Modal VoxCPM2 function and return WAV bytes.

    Args:
        text: Speech text for the turn.
        voice_ref: Reference voice name from the catalog.
        language: ISO 639-1 language code.
        speed: Speaking rate; accepted for backend parity and post-processed
            with atempo by the caller (VoxCPM2 has no native rate control).
        emotion: Optional emotion tag, accepted for parity.

    Returns:
        The WAV payload returned by Modal, at 48 kHz. VoxCPM2 synthesizes a few
        takes and keeps the one a no-reference DNSMOS score ranks best.

    Raises:
        RuntimeError: When Modal is disabled or credentials are missing.
        Exception: Whatever the Modal call raises, for the caller to wrap.
    """
    if not settings.modal_enabled:
        raise RuntimeError("modal is disabled")
    if not settings.modal_token_id or not settings.modal_token_secret:
        raise RuntimeError("modal credentials are not configured")

    import modal

    voxcpm = modal.Cls.from_name(settings.modal_app_name, "VoxCPM2TTS")()
    audio, _cost = await measure(
        voxcpm.generate_tts_voxcpm2.remote.aio(
            text=text,
            voice_ref=voice_ref,
            language=language,
            speed=speed,
            emotion=emotion,
            cfg_value=settings.voxcpm2_cfg_value,
            inference_timesteps=settings.voxcpm2_inference_timesteps,
            num_candidates=settings.voxcpm2_candidates,
        ),
        backend="voxcpm2",
    )
    return audio


async def upload_voice(name: str, wav: bytes, ref_text: str = "") -> None:
    """Upload a reference voice to the Modal Volume.

    Args:
        name: Reference voice name.
        wav: Reference WAV bytes.
        ref_text: Transcript that produced the reference; the clone backends
            read it from the volume manifest, so it is uploaded alongside.

    Raises:
        RuntimeError: When Modal is disabled or credentials are missing.
        Exception: Whatever the Modal call raises, for the caller to wrap.
    """
    if not settings.modal_enabled:
        raise RuntimeError("modal is disabled")
    if not settings.modal_token_id or not settings.modal_token_secret:
        raise RuntimeError("modal credentials are not configured")

    import modal

    function = modal.Function.from_name(settings.modal_app_name, "upload_voice")
    await measure(
        function.remote.aio(name=name, wav=wav, ref_text=ref_text),
        backend="upload_voice",
    )


@trace_function(component="modal.client")
async def modal_judge(
    *,
    system_prompt: str,
    user_prompt: str,
    model: str,
    max_tokens: int = 2048,
) -> dict[str, str]:
    """Call the Modal LLM judge used by digest verification (Phase 7.13).

    Args:
        system_prompt: Judge system prompt.
        user_prompt: Judge user prompt.
        model: HuggingFace model id to serve as judge.
        max_tokens: Response budget for the judgement.

    Returns:
        Dict with the judge's raw ``content``.

    Raises:
        RuntimeError: When Modal is disabled or credentials are missing.
        Exception: Whatever the Modal call raises.
    """
    if not settings.modal_enabled:
        raise RuntimeError("modal is disabled")
    if not settings.modal_token_id or not settings.modal_token_secret:
        raise RuntimeError("modal credentials are not configured")

    import modal

    function = modal.Function.from_name(settings.modal_app_name, "judge")
    result, _cost = await measure(
        function.remote.aio(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            model=model,
            max_tokens=max_tokens,
        ),
        backend="judge",
    )
    return result
