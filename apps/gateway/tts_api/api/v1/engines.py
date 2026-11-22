"""Router para introspección del catálogo declarativo de engines TTS."""

from __future__ import annotations

import importlib.util
import os

from fastapi import APIRouter
from tts_contract import EngineCapabilities, EngineDescriptor, EnginesResponse

from tts_api.core.engine_registry import EngineRegistry
from tts_api.settings import get_settings

router = APIRouter(prefix="/engines", tags=["engines"])


def _descriptor(name: str, registry: EngineRegistry) -> EngineDescriptor:
    """Build the catalog descriptor for a single engine."""
    spec = registry.get(name)
    missing_env = [key for key in spec.required_env if not os.getenv(key, "").strip()]
    if spec.remote_url_env and not os.getenv(spec.remote_url_env, "").strip():
        missing_env.append(spec.remote_url_env)
    if missing_env:
        return EngineDescriptor(
            name=name,
            available=False,
            locations=list(spec.locations),
            error=f"{', '.join(missing_env)} no configurada",
        )
    missing_modules = [
        module for module in spec.required_modules if importlib.util.find_spec(module) is None
    ]
    if missing_modules:
        return EngineDescriptor(
            name=name,
            available=False,
            locations=list(spec.locations),
            error=f"dependencia no instalada: {', '.join(missing_modules)}",
        )
    declared = spec.capabilities
    return EngineDescriptor(
        name=name,
        available=spec.enabled,
        locations=list(spec.locations),
        capabilities=EngineCapabilities(
            stock_voices=declared.stock_voices,
            voice_cloning=declared.voice_cloning,
            instruct_voice_design=declared.instruct_voice_design,
            emotions=declared.emotions,
            streaming=declared.streaming,
            supported_languages=list(declared.supported_languages),
        ),
        error=None if spec.enabled else "engine desactivado",
    )


@router.get(
    "",
    response_model=EnginesResponse,
    summary="Listar engines disponibles y dónde pueden ejecutarse",
    description=(
        "Devuelve el catálogo declarativo de engines del gateway con sus capacidades y las "
        "ubicaciones permitidas (``local`` en el propio gateway y/o ``cloud``). "
        "El campo ``active`` marca el engine que se aplicará en el próximo arranque."
    ),
)
async def list_engines() -> EnginesResponse:
    """Return the engine catalog with capabilities and locations."""
    settings = get_settings()
    registry = settings.engine_registry
    engines = [_descriptor(name, registry) for name in registry.names()]
    return EnginesResponse(
        status="ok",
        active=settings.effective_tts_engine,
        engines=engines,
    )
