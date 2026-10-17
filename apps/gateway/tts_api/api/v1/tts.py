"""Router para síntesis de texto a voz (TTS) — engine-agnostic."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Body, Depends, Header, HTTPException, Query, status
from fastapi.responses import Response, StreamingResponse

from tts_api.core.engine_client import get_shared_engine_client
from tts_api.core.engine_pool import get_engine_pool
from tts_api.core.exceptions import (
    EngineUnavailableError,
    FeatureNotSupportedError,
    UnsupportedEmotionError,
    UnsupportedInstructError,
    UnsupportedLanguageError,
    VoiceNotFoundError,
)
from tts_api.services.tts import TtsService
from tts_api.services.voice_service import VoiceService

router = APIRouter(prefix="/tts", tags=["tts"])

WAV_CHUNK_SIZE = 64 * 1024  # 64 KB chunks for streaming


def _stream_wav(wav_bytes: bytes, chunk_size: int = WAV_CHUNK_SIZE):
    """Generator that yields WAV data in chunks."""
    for i in range(0, len(wav_bytes), chunk_size):
        yield wav_bytes[i : i + chunk_size]


async def get_tts_service() -> TtsService:
    """Dependency para obtener el servicio TTS.

    Reutiliza el cliente de engine compartido por el proceso: cerrarlo aquí
    descargaría el modelo tras cada petición y obligaría a recargarlo en la
    siguiente.
    """
    engine_client = get_shared_engine_client()
    voice_service = VoiceService()
    await voice_service.initialize()
    yield TtsService(engine_client=engine_client, voice_service=voice_service)


def _handle_tts_error(e: Exception) -> None:
    """Convierte excepciones del dominio en HTTPException."""
    if isinstance(e, VoiceNotFoundError):
        detail: dict = {
            "detail": f"Voz no encontrada: {e.voice_id} ({e.voice_type})",
            "error_type": "voice_not_found",
            "voice_id": e.voice_id,
        }
        if e.valid_voice_ids:
            detail["valid_voice_ids"] = e.valid_voice_ids
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=detail) from e
    if isinstance(e, UnsupportedLanguageError):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "detail": f"Idioma no soportado: {e.language}",
                "error_type": "unsupported_language",
                "language": e.language,
                "supported_languages": e.supported_languages,
            },
        ) from e
    if isinstance(e, UnsupportedInstructError):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "detail": str(e),
                "error_type": "unsupported_instruct",
                "invalid_items": [
                    {"token": token, "suggestion": sug} for token, sug in e.invalid_items.items()
                ],
                "valid_tokens": e.valid_items,
            },
        ) from e
    if isinstance(e, UnsupportedEmotionError):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "detail": f"Emoción no soportada: {e.emotion}",
                "error_type": "unsupported_emotion",
                "emotion": e.emotion,
                "supported_emotions": e.supported_emotions,
            },
        ) from e
    if isinstance(e, FeatureNotSupportedError):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "detail": str(e),
                "error_type": "feature_not_supported",
                "feature": e.feature,
                "engine": e.engine,
            },
        ) from e
    if isinstance(e, EngineUnavailableError):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "detail": "Motor de síntesis no disponible",
                "error_type": "engine_unavailable",
            },
        ) from e
    raise HTTPException(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        detail={"detail": "Error interno del servidor", "error_type": "internal_error"},
    ) from e


# --- POST /tts — Stock voices ---
@router.post(
    "",
    responses={200: {"content": {"audio/wav": {}}}},
    response_class=Response,
    summary="Sintetizar texto a voz con voz stock",
    description=(
        "Genera audio WAV a partir de texto usando una voz predefinida (stock). "
        "Soporta voces en múltiples idiomas con control de velocidad. "
        "Opcionalmente puedes usar `instruct` para voice design libre (solo OmniVoice engine)."
    ),
)
async def synthesize_tts(
    text: Annotated[str, Body(min_length=1, description="Texto a sintetizar")],
    voice_id: Annotated[str, Body(description="ID de la voz (ej: es-mx-male)")],
    language: Annotated[str, Body(description="Idioma del texto (ISO 639-1)")],
    speed: Annotated[float, Body(ge=0.5, le=2.0, description="Velocidad de habla")] = 1.0,
    instruct: Annotated[
        str | None, Body(description="Instruct personalizado (voice design libre, solo OmniVoice)")
    ] = None,
    emotion: Annotated[
        str | None, Body(description="Emoción (solo engines que la soporten)")
    ] = None,
    stream: Annotated[bool, Query(description="Streaming por chunks")] = False,
    accept: Annotated[str | None, Header(description="Tipo de contenido esperado")] = None,
    tts_service: TtsService = Depends(get_tts_service),
) -> Response:
    """Sintetiza texto a voz con voz stock, clonada o instruct."""
    try:
        pool = get_engine_pool()
        await pool.acquire()
        try:
            if instruct:
                result = await tts_service.synthesize_instruct(
                    text=text,
                    instruct=instruct,
                    language=language,
                    speed=speed,
                    emotion=emotion,
                )
            else:
                result = await tts_service.synthesize_stock(
                    text=text,
                    voice_id=voice_id,
                    language=language,
                    speed=speed,
                    emotion=emotion,
                )
        finally:
            pool.release()
    except (
        VoiceNotFoundError,
        UnsupportedLanguageError,
        UnsupportedInstructError,
        UnsupportedEmotionError,
        EngineUnavailableError,
        FeatureNotSupportedError,
    ) as e:
        _handle_tts_error(e)
    except Exception as e:
        _handle_tts_error(e)

    if stream:
        return StreamingResponse(
            _stream_wav(result.wav_bytes),
            media_type="audio/wav",
            headers={"Content-Length": str(len(result.wav_bytes))},
        )
    return Response(content=result.wav_bytes, media_type="audio/wav")


# --- POST /tts/instruct — Custom instruct ---
@router.post(
    "/instruct",
    responses={200: {"content": {"audio/wav": {}}}},
    response_class=Response,
    summary="Sintetizar texto con instruct personalizado",
    description=(
        "Voice design libre: especifica atributos del hablante directamente mediante un instruct "
        "(ej: 'female, young adult, british accent'). Solo soportado por el engine OmniVoice."
    ),
)
async def synthesize_instruct(
    text: Annotated[str, Body(min_length=1, description="Texto a sintetizar")],
    instruct: Annotated[
        str,
        Body(description="Instruct de voice design (ej: 'female, young adult, british accent')"),
    ],
    language: Annotated[str, Body(description="Idioma del texto (ISO 639-1)")],
    speed: Annotated[float, Body(ge=0.5, le=2.0, description="Velocidad de habla")] = 1.0,
    emotion: Annotated[
        str | None, Body(description="Emoción (solo engines que la soporten)")
    ] = None,
    stream: Annotated[bool, Query(description="Streaming por chunks")] = False,
    accept: Annotated[str | None, Header(description="Tipo de contenido esperado")] = None,
    tts_service: TtsService = Depends(get_tts_service),
) -> Response:
    """Sintetiza texto con un instruct de voice design personalizado."""
    try:
        pool = get_engine_pool()
        await pool.acquire()
        try:
            result = await tts_service.synthesize_instruct(
                text=text,
                instruct=instruct,
                language=language,
                speed=speed,
                emotion=emotion,
            )
        finally:
            pool.release()
    except (
        UnsupportedLanguageError,
        UnsupportedInstructError,
        UnsupportedEmotionError,
        EngineUnavailableError,
        FeatureNotSupportedError,
    ) as e:
        _handle_tts_error(e)
    except Exception as e:
        _handle_tts_error(e)

    if stream:
        return StreamingResponse(
            _stream_wav(result.wav_bytes),
            media_type="audio/wav",
            headers={"Content-Length": str(len(result.wav_bytes))},
        )
    return Response(content=result.wav_bytes, media_type="audio/wav")


# --- GET /tts/voice-design/tokens ---
@router.get(
    "/voice-design/tokens",
    summary="Listar tokens válidos para voice design",
    description="Devuelve los tokens de instruct agrupados por categoría (solo OmniVoice engine).",
)
async def get_voice_design_tokens() -> dict:
    """Devuelve los tokens válidos para instruct de voice design.

    Returns empty categories if the active engine does not support voice design.
    """
    from tts_api.settings import get_settings

    settings = get_settings()

    if settings.TTS_ENGINE != "omnivoice":
        return {
            "note": f"Voice design no soportado por el engine '{settings.TTS_ENGINE}'. "
            "Cambia TTS_ENGINE=omnivoice para usar voice design.",
            "gender": [],
            "age": [],
            "pitch": [],
            "style": [],
            "english_accent": [],
            "chinese_dialect": [],
        }

    return {
        "gender": ["male", "female"],
        "age": ["child", "teenager", "young adult", "middle-aged", "elderly"],
        "pitch": ["very low pitch", "low pitch", "moderate pitch", "high pitch", "very high pitch"],
        "style": ["whisper"],
        "english_accent": [
            "american accent",
            "australian accent",
            "british accent",
            "canadian accent",
            "chinese accent",
            "indian accent",
            "japanese accent",
            "korean accent",
            "portuguese accent",
            "russian accent",
        ],
        "chinese_dialect": [
            "河南话",
            "陕西话",
            "四川话",
            "贵州话",
            "云南话",
            "桂林话",
            "济南话",
            "石家庄话",
            "甘肃话",
            "宁夏话",
            "青岛话",
            "东北话",
        ],
    }
