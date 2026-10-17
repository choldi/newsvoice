"""Router para conversaciones multi-voz."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Body, Depends, HTTPException, Query, status
from fastapi.responses import Response, StreamingResponse

from tts_api.core.engine_client import get_shared_engine_client
from tts_api.core.exceptions import (
    EngineUnavailableError,
    VoiceNotFoundError,
)
from tts_api.services.conversation import ConversationService, ConversationTurn
from tts_api.services.tts import TtsService

router = APIRouter(prefix="/conversations", tags=["conversations"])

WAV_CHUNK_SIZE = 64 * 1024  # 64 KB chunks for streaming
MIN_CONVERSATION_TURNS = 2


def _stream_wav(wav_bytes: bytes, chunk_size: int = WAV_CHUNK_SIZE):
    """Generator that yields WAV data in chunks."""
    for i in range(0, len(wav_bytes), chunk_size):
        yield wav_bytes[i : i + chunk_size]


async def get_conversation_service() -> ConversationService:
    """Dependency para obtener el servicio de conversaciones."""
    engine_client = get_shared_engine_client()
    tts_service = TtsService(engine_client=engine_client)
    service = ConversationService(engine_client=engine_client, tts_service=tts_service)
    try:
        yield service
    finally:
        await tts_service.close()


@router.post(
    "",
    responses={200: {"content": {"audio/wav": {}}}},
    response_class=Response,
    summary="Generar conversación multi-voz",
    description=(
        "Genera audio de una conversación entre dos o más voces. Cada "
        "turno especifica una voz, texto e idioma. Los turnos se concatenan "
        "con silencios configurables. Soporta voces stock, clonadas y "
        "diseñadas."
    ),
)
async def generate_conversation(
    turns: Annotated[
        list[dict],
        Body(
            description=(
                "Lista de turnos. Cada turno tiene 'voice_id', 'text' y "
                "opcionalmente 'language'. Mínimo 2 turnos."
            ),
            examples=[
                [
                    {"voice_id": "es-mx-male", "text": "Hola, ¿cómo estás?", "language": "es"},
                    {"voice_id": "es-mx-female", "text": "Muy bien, gracias", "language": "es"},
                ]
            ],
        ),
    ],
    pause_ms: Annotated[
        int,
        Body(ge=0, le=5000, description="Milisilundos de silencio entre turnos (0-5000)"),
    ] = 300,
    speed: Annotated[float, Body(ge=0.5, le=2.0, description="Velocidad de habla")] = 1.0,
    stream: Annotated[bool, Query(description="Streaming por chunks")] = False,
    tts_service: ConversationService = Depends(get_conversation_service),
) -> Response:
    """Genera audio de una conversación multi-voz."""
    try:
        if not turns or len(turns) < MIN_CONVERSATION_TURNS:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={
                    "detail": "Se requieren al menos 2 turnos para una conversación",
                    "error_type": "validation_error",
                },
            )

        conversation_turns = []
        for i, turn in enumerate(turns):
            if "voice_id" not in turn or "text" not in turn:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail={
                        "detail": f"El turno {i + 1} debe tener 'voice_id' y 'text'",
                        "error_type": "validation_error",
                    },
                )
            if not turn["text"] or not turn["text"].strip():
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail={
                        "detail": f"El turno {i + 1} tiene texto vacío",
                        "error_type": "validation_error",
                    },
                )
            conversation_turns.append(
                ConversationTurn(
                    voice_id=turn["voice_id"],
                    text=turn["text"],
                    language=turn.get("language", "es"),
                )
            )

        result = await tts_service.generate(
            turns=conversation_turns,
            pause_ms=pause_ms,
            speed=speed,
        )

    except HTTPException:
        raise
    except VoiceNotFoundError as e:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "detail": f"Voz no encontrada: {e.voice_id}",
                "error_type": "voice_not_found",
                "voice_id": e.voice_id,
            },
        ) from e
    except EngineUnavailableError as e:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "detail": "Motor de síntesis no disponible",
                "error_type": "engine_unavailable",
            },
        ) from e
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={
                "detail": f"Error generando conversación: {e}",
                "error_type": "internal_error",
            },
        ) from e

    if stream:
        return StreamingResponse(
            _stream_wav(result.wav_bytes),
            media_type="audio/wav",
            headers={"Content-Length": str(len(result.wav_bytes))},
        )
    return Response(content=result.wav_bytes, media_type="audio/wav")
