from __future__ import annotations

import io
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from pydantic import BaseModel, ConfigDict, Field, field_validator
from pydub import AudioSegment

from tts_api.api.v1.tts import _handle_tts_error, get_tts_service
from tts_api.core.engine_pool import get_engine_pool
from tts_api.services.tts import TtsService

router = APIRouter(tags=["openai-compatible"])

LANGUAGE_CODE_LENGTH = 2


class OpenAISpeechRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    model: str = Field(min_length=1, description="Accepted for client compatibility")
    input: str = Field(min_length=1, description="Text to synthesize")
    voice: str = Field(min_length=1, description="Gateway voice id or provider voice name")
    response_format: str = Field(default="wav", description="wav or mp3")
    speed: float = Field(default=1.0, ge=0.5, le=2.0)

    @field_validator("response_format")
    @classmethod
    def _normalize_format(cls, value: str) -> str:
        return value.strip().lower()


def _convert_audio(wav_bytes: bytes, response_format: str) -> tuple[bytes, str]:
    if response_format == "wav":
        return wav_bytes, "audio/wav"
    if response_format == "mp3":
        segment = AudioSegment.from_file(io.BytesIO(wav_bytes), format="wav")
        buffer = io.BytesIO()
        segment.export(buffer, format="mp3")
        return buffer.getvalue(), "audio/mpeg"
    raise HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail={
            "detail": f"response_format no soportado: {response_format}",
            "error_type": "unsupported_response_format",
        },
    )


@router.post(
    "/v1/audio/speech",
    responses={200: {"content": {"audio/wav": {}, "audio/mpeg": {}}}},
    response_class=Response,
    summary="OpenAI-compatible speech synthesis",
    description=(
        "Compatibility facade over the native TTS gateway. The active engine is chosen by the "
        "gateway registry; the model field is accepted so OpenAI clients work unchanged."
    ),
)
async def create_speech(
    request: OpenAISpeechRequest,
    language: Annotated[str, Query(description="ISO 639-1 language of the input text")] = "es",
    tts_service: TtsService = Depends(get_tts_service),
) -> Response:
    """Synthesize text using the OpenAI-compatible request contract."""
    code = language.strip().lower()
    if not code.isalpha() or len(code) != LANGUAGE_CODE_LENGTH:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "detail": "language must be a two-letter ISO 639-1 code",
                "error_type": "unsupported_language",
                "language": language,
            },
        )
    pool = get_engine_pool()
    await pool.acquire()
    try:
        result = await tts_service.synthesize_stock(
            text=request.input,
            voice_id=request.voice,
            language=code,
            speed=request.speed,
        )
    except Exception as exc:
        _handle_tts_error(exc)
        raise
    finally:
        pool.release()
    body, media_type = _convert_audio(result.wav_bytes, request.response_format)
    return Response(content=body, media_type=media_type)
