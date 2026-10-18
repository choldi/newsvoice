"""Router del catálogo de voces que el gateway posee.

Es la única fuente de verdad sobre qué voces existen y qué referencias están
listas para clonar: news-engine la lee con ``GET /voices/catalog`` en lugar de
su propio filesystem, sube las referencias diseñadas con
``POST /voices/catalog/refs/{name}`` y crea voces de operador con
``POST /voices/catalog/custom``.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, File, Form, HTTPException, Response, UploadFile, status
from tts_contract import VoiceCatalogError, VoiceCatalogResponse, VoiceManifestEntry, VoiceProfile

from tts_api.services.voice_catalog_service import (
    VoiceCatalogConflictError,
    VoiceCatalogNotFoundError,
    VoiceCatalogService,
    VoiceCatalogValidationError,
)

router = APIRouter(tags=["catalog"])


def get_catalog_service() -> VoiceCatalogService:
    """Dependency que devuelve el servicio del catálogo.

    Returns:
        Instancia del servicio, que relee el filesystem en cada llamada para
        que dos procesos del mismo host no sirvan un catálogo distinto.
    """
    return VoiceCatalogService()


def _http_error(exc: VoiceCatalogError) -> HTTPException:
    """Translate a catalog error into the HTTP answer news-engine expects.

    Args:
        exc: Error raised by :class:`VoiceCatalogService`.

    Returns:
        HTTPException with a flat ``detail`` string (the shape
        :func:`tts_contract.parse_gateway_error` unwraps).
    """
    if isinstance(exc, VoiceCatalogConflictError):
        code = status.HTTP_409_CONFLICT
    elif isinstance(exc, VoiceCatalogNotFoundError):
        code = status.HTTP_404_NOT_FOUND
    elif isinstance(exc, VoiceCatalogValidationError):
        code = status.HTTP_422_UNPROCESSABLE_ENTITY
    else:
        code = status.HTTP_500_INTERNAL_SERVER_ERROR
    return HTTPException(status_code=code, detail=str(exc))


@router.get(
    "/voices/catalog",
    response_model=VoiceCatalogResponse,
    summary="Catálogo de voces con estado de generación",
    description=(
        "Devuelve todos los perfiles de voz (los del repositorio y los creados "
        "por el operador), su manifest y el estado de cada referencia: "
        "`pendiente`, `generada` o `desactualizada`."
    ),
)
async def get_voice_catalog(
    service: VoiceCatalogService = Depends(get_catalog_service),
) -> VoiceCatalogResponse:
    """Serve the whole catalog.

    Args:
        service: Catalog service.

    Returns:
        Profiles, manifest and per-profile reference status.

    Raises:
        HTTPException: 500 when a catalog file cannot be read.
    """
    try:
        return service.catalog()
    except VoiceCatalogError as exc:
        raise _http_error(exc) from exc


@router.get(
    "/voices/catalog/refs/{name}",
    summary="Audio de referencia de una voz",
    description="Devuelve los bytes WAV de la referencia generada para un perfil.",
)
async def get_voice_reference(
    name: str,
    service: VoiceCatalogService = Depends(get_catalog_service),
) -> Response:
    """Serve one profile's reference WAV.

    Args:
        name: Profile name, optionally with a ``.wav`` suffix.
        service: Catalog service.

    Returns:
        The WAV bytes with an ``audio/wav`` content type.

    Raises:
        HTTPException: 404 when the name is unsafe, unknown or has no
            reference stored.
    """
    try:
        wav = service.read_reference(name)
    except VoiceCatalogError as exc:
        raise _http_error(exc) from exc
    filename = name if name.lower().endswith(".wav") else f"{name}.wav"
    return Response(
        content=wav,
        media_type="audio/wav",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.post(
    "/voices/catalog/refs/{name}",
    status_code=status.HTTP_201_CREATED,
    response_model=VoiceManifestEntry,
    summary="Guardar la referencia de una voz",
    description=(
        "Recibe el WAV diseñado por news-engine y lo guarda junto a su entrada "
        "de manifest. El gateway calcula duración, muestreo y digest del audio "
        "recibido, no confía en los que declara el cliente."
    ),
)
async def store_voice_reference(
    name: str,
    file: UploadFile = File(..., description="Referencia WAV diseñada"),
    instruct: str = Form(..., description="Instruct del perfil al generarla"),
    ref_text: str = Form(..., description="Transcripción de la referencia"),
    design_model: str = Form("", description="Modelo que produjo la referencia"),
    service: VoiceCatalogService = Depends(get_catalog_service),
) -> VoiceManifestEntry:
    """Store a designed reference and return its manifest entry.

    Args:
        name: Profile name (slug, without the ``.wav`` suffix).
        file: Raw WAV bytes of the reference.
        instruct: Profile instruct at generation time.
        ref_text: Profile transcript at generation time.
        design_model: Model that produced the reference.
        service: Catalog service.

    Returns:
        The stored manifest entry.

    Raises:
        HTTPException: 404 for an unknown profile, 422 for unusable audio and
            500 when the manifest cannot be written.
    """
    payload = await file.read()
    await file.close()
    try:
        return service.store_reference(
            name,
            payload,
            instruct=instruct,
            ref_text=ref_text,
            design_model=design_model,
        )
    except VoiceCatalogError as exc:
        raise _http_error(exc) from exc


@router.post(
    "/voices/catalog/custom",
    status_code=status.HTTP_201_CREATED,
    response_model=VoiceProfile,
    summary="Crear una voz de operador",
    description=(
        "Añade al catálogo un perfil que no viene con el repositorio. Rechaza "
        "con 409 un nombre que ya exista en el catálogo base o entre las voces "
        "de operador."
    ),
)
async def create_custom_voice(
    profile: VoiceProfile,
    service: VoiceCatalogService = Depends(get_catalog_service),
) -> VoiceProfile:
    """Append an operator-created profile to the catalog.

    Args:
        profile: Validated profile to append.
        service: Catalog service.

    Returns:
        The saved profile.

    Raises:
        HTTPException: 409 when the name is taken, 500 when the file cannot be
            written.
    """
    try:
        return service.save_custom(profile)
    except VoiceCatalogError as exc:
        raise _http_error(exc) from exc
