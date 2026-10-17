"""Router para gestión de voces."""

from __future__ import annotations

import asyncio
from pathlib import Path

from fastapi import APIRouter, Body, Depends, File, Form, HTTPException, Query, UploadFile, status
from loguru import logger

from tts_api.core.engine_client import OmniVoiceEngineClient, StockVoice
from tts_api.core.exceptions import (
    FeatureNotSupportedError,
    InvalidReferenceAudioError,
    UnsupportedInstructError,
    UnsupportedLanguageError,
    VoiceNotFoundError,
)
from tts_api.services.voice_service import VoiceService

router = APIRouter(prefix="/voices", tags=["voices"])


async def get_engine_client() -> OmniVoiceEngineClient:
    """Dependency para obtener el cliente del engine."""
    client = OmniVoiceEngineClient()
    await client.start()
    try:
        yield client
    finally:
        await client.stop()


async def get_voice_service() -> VoiceService:
    """Dependency para obtener el servicio de voces."""
    service = VoiceService()
    await service.initialize()
    try:
        yield service
    finally:
        pass  # Service cleanup handled elsewhere if needed


@router.get(
    "/stock",
    response_model=list[StockVoice],
    summary="Listar voces stock disponibles",
    description=(
        "Devuelve la lista de voces predefinidas (stock) disponibles en el motor. "
        "Cada voz incluye voice_id, idioma, género y nombre. "
        "Opcionalmente filtrar por idioma (ISO 639-1)."
    ),
)
async def list_stock_voices(
    language: str | None = Query(None, description="Filtrar por idioma (ISO 639-1)"),
    engine_client: OmniVoiceEngineClient = Depends(get_engine_client),
) -> list[StockVoice]:
    return await engine_client.list_stock_voices(language)


@router.post(
    "/clone",
    status_code=status.HTTP_201_CREATED,
    summary="Clonar voz desde audio de referencia",
    description=(
        "Clona una voz a partir de un archivo de audio de referencia. "
        "Especifica `engines` para asociar la voz a uno o más engines "
        "(ej: 'pocket_tts' o 'pocket_tts,omnivoice'). "
        "La voz clonada se almacena y puede usarse en llamadas subsiguientes a /tts."
    ),
)
async def clone_voice(
    name: str = Form(..., description="Nombre único para la voz clonada"),
    language: str = Form(..., description="Idioma del audio de referencia (ISO 639-1)"),
    reference_audio: UploadFile = File(
        ..., description="Archivo de audio de referencia (WAV, FLAC)"
    ),
    ref_text: str | None = Form(
        None, description="Transcripción del audio de referencia (mejora calidad del clonado)"
    ),
    engines: str = Form(
        "auto",
        description=(
            "Engine(s) para la voz: 'pocket_tts', 'omnivoice', "
            "'pocket_tts,omnivoice' (separado por coma), o 'auto' (activa)"
        ),
    ),
    voice_service: VoiceService = Depends(get_voice_service),
) -> dict:
    # Validate file type
    if not reference_audio.filename:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Nombre de archivo requerido"
        )

    # Save uploaded file temporarily
    import tempfile

    with tempfile.NamedTemporaryFile(
        delete=False, suffix=Path(reference_audio.filename).suffix
    ) as tmp_file:
        content = await reference_audio.read()
        tmp_file.write(content)
        tmp_file_path = Path(tmp_file.name)

    try:
        # Auto-transcribe if ref_text not provided and auto-transcribe is enabled
        if not ref_text:
            from tts_api.services import transcription

            if transcription.is_available():
                ref_text = await asyncio.to_thread(
                    transcription.transcribe, tmp_file_path, language
                )
                if ref_text:
                    logger.info("Auto-transcribed reference audio: %s", ref_text[:80])

        # Parse engines param
        if engines.strip().lower() in ("auto", "", "default"):
            engine_param = None
        else:
            engine_param = engines.strip()

        # Clone the voice
        voice_id = await voice_service.clone_voice(
            name=name,
            language=language,
            reference_audio_path=tmp_file_path,
            ref_text=ref_text,
            engine=engine_param,
        )

        return {
            "voice_id": voice_id,
            "name": name,
            "language": language,
            "engines": engine_param or "auto",
            "ref_text": ref_text,
            "message": f"Voz '{name}' clonada exitosamente",
        }
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(e)) from e
    except (UnsupportedLanguageError, InvalidReferenceAudioError) as e:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(e)) from e
    except FeatureNotSupportedError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "detail": str(e),
                "error_type": "feature_not_supported",
                "feature": e.feature,
                "engine": e.engine,
            },
        ) from e
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"Error interno: {e!s}"
        ) from e
    finally:
        # Clean up temporary file
        tmp_file_path.unlink(missing_ok=True)


@router.get("/cloned", response_model=list[dict])
async def list_cloned_voices(
    language: str | None = Query(None, description="Filtrar por idioma (ISO 639-1)"),
    engine: str | None = Query(None, description="Filtrar por engine (pocket_tts, omnivoice)"),
    limit: int = Query(100, ge=1, le=1000, description="Límite de resultados"),
    offset: int = Query(0, ge=0, description="Desplazamiento para paginación"),
    voice_service: VoiceService = Depends(get_voice_service),
) -> list[dict]:
    """
    Lista las voces clonadas disponibles.

    - **language**: Filtrar por idioma (ISO 639-1)
    - **engine**: Filtrar por engine (pocket_tts, omnivoice)
    - **limit**: Número máximo de resultados (1-1000)
    - **offset**: Desplazamiento para paginación
    """
    return await voice_service.list_voices(
        language=language,
        limit=limit,
        offset=offset,
        engine=engine,
    )


@router.get("/cloned/{voice_id}", response_model=dict)
async def get_cloned_voice(
    voice_id: str,
    voice_service: VoiceService = Depends(get_voice_service),
) -> dict:
    """
    Obtiene una voz clonada específica por su ID.

    - **voice_id**: UUID de la voz clonada
    """
    try:
        return await voice_service.get_voice(voice_id)
    except VoiceNotFoundError as e:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"Voz clonada no encontrada: {e.voice_id}"
        ) from e


@router.delete("/cloned/{voice_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_cloned_voice(
    voice_id: str,
    voice_service: VoiceService = Depends(get_voice_service),
) -> None:
    """
    Elimina una voz clonada por su ID.

    - **voice_id**: UUID de la voz clonada
    """
    try:
        deleted = await voice_service.delete_voice(voice_id)
        if not deleted:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Voz clonada no encontrada: {voice_id}",
            )
    except VoiceNotFoundError as e:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"Voz clonada no encontrada: {e.voice_id}"
        ) from e


# --- Designed voices (instruct-based presets) ---


@router.post(
    "/design",
    status_code=status.HTTP_201_CREATED,
    summary="Crear voz por voice design (instruct)",
    description=(
        "Crea una voz a partir de un instruct de voice design. "
        "El instruct define atributos del hablante (ej: 'female, young adult, british accent'). "
        "La voz se guarda como preset reutilizable en llamadas a /tts."
    ),
)
async def create_designed_voice(
    name: str = Body(..., description="Nombre único para la voz"),
    instruct: str = Body(
        ..., description="Instruct de voice design (ej: 'female, young adult, british accent')"
    ),
    language: str = Body(..., description="Idioma principal (ISO 639-1)"),
    voice_service: VoiceService = Depends(get_voice_service),
) -> dict:
    try:
        voice_id = await voice_service.create_designed_voice(
            name=name,
            instruct=instruct,
            language=language,
        )
        return {
            "voice_id": voice_id,
            "name": name,
            "instruct": instruct,
            "language": language,
            "message": f"Voz diseñada '{name}' creada exitosamente",
        }
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(e),
        ) from e
    except UnsupportedLanguageError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e),
        ) from e
    except UnsupportedInstructError as e:
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


@router.get(
    "/designed",
    response_model=list[dict],
    summary="Listar voces diseñadas",
    description="Lista las voces diseñadas (instruct-based presets) guardadas.",
)
async def list_designed_voices(
    language: str | None = Query(None, description="Filtrar por idioma (ISO 639-1)"),
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    voice_service: VoiceService = Depends(get_voice_service),
) -> list[dict]:
    return await voice_service.list_designed_voices(
        language=language,
        limit=limit,
        offset=offset,
    )


@router.get(
    "/designed/{voice_id}",
    response_model=dict,
    summary="Obtener voz diseñada por ID",
)
async def get_designed_voice(
    voice_id: str,
    voice_service: VoiceService = Depends(get_voice_service),
) -> dict:
    try:
        return await voice_service.get_designed_voice(voice_id)
    except VoiceNotFoundError as e:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Voz diseñada no encontrada: {e.voice_id}",
        ) from e


@router.delete(
    "/designed/{voice_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Eliminar voz diseñada",
)
async def delete_designed_voice(
    voice_id: str,
    voice_service: VoiceService = Depends(get_voice_service),
) -> None:
    try:
        deleted = await voice_service.delete_designed_voice(voice_id)
        if not deleted:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Voz diseñada no encontrada: {voice_id}",
            )
    except VoiceNotFoundError as e:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Voz diseñada no encontrada: {e.voice_id}",
        ) from e
