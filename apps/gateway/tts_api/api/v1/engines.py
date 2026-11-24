"""Router para introspección del catálogo de engines TTS."""

from __future__ import annotations

import importlib.util
import os

from fastapi import APIRouter
from tts_contract import (
    LEGACY_ENGINE_ALIASES,
    EngineCapabilities,
    EngineDescriptor,
    EnginesResponse,
    canonical_engine_id,
)

from tts_api.core.engine_factory import create_engine_for_catalog
from tts_api.core.engine_registry import EngineRegistry, EngineSpec
from tts_api.settings import get_settings

router = APIRouter(prefix="/engines", tags=["engines"])


def _declared_capabilities(spec: EngineSpec) -> EngineCapabilities:
    """Build the manifest fallback used when runtime introspection fails."""
    declared = spec.capabilities
    return EngineCapabilities(
        stock_voices=declared.stock_voices,
        voice_cloning=declared.voice_cloning,
        instruct_voice_design=declared.instruct_voice_design,
        emotions=declared.emotions,
        streaming=declared.streaming,
        supported_languages=list(declared.supported_languages),
    )


def _descriptor(name: str, registry: EngineRegistry) -> EngineDescriptor:
    """Build the catalog descriptor for a single engine."""
    key = canonical_engine_id(name)
    spec = registry.get(key)
    missing_env = [item for item in spec.required_env if not os.getenv(item, "").strip()]
    if spec.remote_url_env and not os.getenv(spec.remote_url_env, "").strip():
        missing_env.append(spec.remote_url_env)
    missing_modules = [
        module for module in spec.required_modules if importlib.util.find_spec(module) is None
    ]
    aliases = sorted(
        alias for alias, target in LEGACY_ENGINE_ALIASES.items() if target == key and alias != key
    )
    base: dict[str, object] = {
        "name": key,
        "label": spec.display_name,
        "aliases": aliases,
        "execution_mode": "gateway",
        "default_output_format": ".mp3" if key == "edgetts" else ".wav",
    }
    declared = _declared_capabilities(spec)
    if missing_env:
        return EngineDescriptor(
            **base,
            available=False,
            locations=list(spec.locations),
            capabilities=declared,
            error=f"{', '.join(missing_env)} no configurada",
        )
    if missing_modules:
        return EngineDescriptor(
            **base,
            available=False,
            locations=list(spec.locations),
            capabilities=declared,
            error=f"dependencia no instalada: {', '.join(missing_modules)}",
        )
    try:
        runtime = create_engine_for_catalog(key, registry).capabilities
    except Exception as exc:
        return EngineDescriptor(
            **base,
            available=False,
            locations=list(spec.locations),
            capabilities=declared,
            error=f"no se pudo introspectar: {exc}",
        )
    locations = list(dict.fromkeys([*runtime.locations, *spec.locations]))
    capabilities = EngineCapabilities(
        stock_voices=runtime.stock_voices,
        voice_cloning=runtime.voice_cloning,
        instruct_voice_design=runtime.instruct_voice_design,
        emotions=runtime.emotions,
        streaming=runtime.streaming,
        supported_languages=list(runtime.supported_languages),
    )
    return EngineDescriptor(
        **base,
        available=spec.enabled,
        locations=locations,
        capabilities=capabilities,
        error=None if spec.enabled else "engine desactivado",
    )


@router.get(
    "",
    response_model=EnginesResponse,
    summary="Listar engines disponibles y dónde pueden ejecutarse",
    description=(
        "Devuelve los engines del gateway con las capacidades que declara su implementación "
        "y las ubicaciones permitidas (``local`` en el propio gateway y/o ``cloud``). "
        "El campo ``active`` marca el engine que se aplicará en el próximo arranque."
    ),
)
async def list_engines() -> EnginesResponse:
    """Return the engine catalog with runtime capabilities and locations."""
    settings = get_settings()
    registry = settings.engine_registry
    engines = [_descriptor(name, registry) for name in registry.names()]
    return EnginesResponse(
        status="ok",
        active=canonical_engine_id(settings.effective_tts_engine),
        engines=engines,
    )
