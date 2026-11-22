from __future__ import annotations

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field

from tts_api.core.engine_registry import EngineRegistry, EngineSpec, EngineSpecPatch
from tts_api.settings import get_settings

router = APIRouter(prefix="/admin/engines", tags=["admin", "engines"])


class AdminEnginesOut(BaseModel):
    model_config = ConfigDict(extra="ignore")

    status: str = "ok"
    active: str
    restart_required: bool = False
    engines: list[EngineSpec] = Field(default_factory=list)


class EngineMutationOut(BaseModel):
    model_config = ConfigDict(extra="ignore")

    status: str = "ok"
    engine: str
    restart_required: bool = True
    spec: EngineSpec | None = None


class EngineValidationOut(BaseModel):
    model_config = ConfigDict(extra="ignore")

    status: str = "ok"
    engine: str
    valid: bool
    errors: list[str] = Field(default_factory=list)
    restart_required: bool = False


class ActivateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, description="Engine id to activate on next restart")


class ValidateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, description="Engine id to validate")
    spec: EngineSpecPatch = Field(default_factory=EngineSpecPatch)


def _registry() -> EngineRegistry:
    return get_settings().engine_registry


def _http_error(message: str, code: int = status.HTTP_400_BAD_REQUEST) -> HTTPException:
    return HTTPException(
        status_code=code,
        detail={"detail": message, "error_type": "engine_registry_error"},
    )


@router.get(
    "",
    response_model=AdminEnginesOut,
    summary="Listar el registro declarativo de engines",
)
async def list_engine_registry() -> AdminEnginesOut:
    """Return every effective engine spec and the staged active selection."""
    settings = get_settings()
    registry = settings.engine_registry
    return AdminEnginesOut(
        active=settings.effective_tts_engine,
        engines=[registry.get(name) for name in registry.names()],
    )


@router.post(
    "/validate",
    response_model=EngineValidationOut,
    summary="Validar una especificación de engine sin aplicarla",
)
async def validate_engine_spec(request: ValidateRequest) -> EngineValidationOut:
    """Validate a candidate spec against the registry schema."""
    registry = _registry()
    errors = registry.validate(request.id, request.spec)
    return EngineValidationOut(engine=request.id, valid=not errors, errors=errors)


@router.put(
    "/{engine_id}",
    response_model=EngineMutationOut,
    summary="Crear o actualizar un engine en la configuración local",
)
async def upsert_engine_spec(engine_id: str, patch: EngineSpecPatch) -> EngineMutationOut:
    """Stage an engine definition to be loaded on the next restart."""
    registry = _registry()
    try:
        spec = registry.upsert(engine_id, patch)
        registry.save()
    except ValueError as exc:
        raise _http_error(str(exc)) from exc
    return EngineMutationOut(engine=engine_id, spec=spec)


@router.delete(
    "/{engine_id}",
    response_model=EngineMutationOut,
    summary="Desactivar un engine del manifiesto o eliminar uno local",
)
async def delete_engine_spec(engine_id: str) -> EngineMutationOut:
    """Stage the removal or disablement of an engine."""
    registry = _registry()
    if not registry.remove(engine_id):
        raise _http_error(f"Engine desconocido: {engine_id}", status.HTTP_404_NOT_FOUND)
    registry.save()
    return EngineMutationOut(engine=engine_id)


@router.post(
    "/activate",
    response_model=EngineMutationOut,
    summary="Seleccionar el engine activo para el próximo arranque",
)
async def activate_engine(request: ActivateRequest) -> EngineMutationOut:
    """Stage the active engine selection for the next restart."""
    registry = _registry()
    try:
        registry.set_active(request.id)
        registry.save()
    except (KeyError, ValueError) as exc:
        raise _http_error(str(exc)) from exc
    return EngineMutationOut(engine=request.id.strip().lower())
