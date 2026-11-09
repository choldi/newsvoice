"""Router para introspección del catálogo de engines TTS.

Expone qué engines conoce el gateway y dónde pueden ejecutarse
(``local`` en este gateway y/o ``cloud``), para que los clientes
—notablemente news-engine— puedan ofrecer el switch "run in modal".
"""

from __future__ import annotations

from fastapi import APIRouter
from tts_contract import EngineCapabilities, EngineDescriptor, EnginesResponse

from tts_api.settings import get_settings

router = APIRouter(prefix="/engines", tags=["engines"])

#: Engines that can be instantiated to introspect their capabilities.
KNOWN_ENGINES = (
    "edgetts",
    "pocket_tts",
    "qwen3_tts",
    "omnivoice",
    "piper",
    "chatterbox",
    "zono2",
    "kokoro",
    "fish_tts",
    "openai_tts",
    "mock",
)


def _descriptor(name: str) -> EngineDescriptor:
    """Build the catalog descriptor for a single engine."""
    from tts_api.core.engine_factory import create_engine

    settings = get_settings()
    missing = {
        "fish_tts": ("FISH_API_KEY", settings.fish_configured),
        "openai_tts": ("OPENAI_TTS_BASE_URL", settings.openai_tts_configured),
    }.get(name)
    if missing and not missing[1]:
        return EngineDescriptor(
            name=name,
            available=False,
            error=f"{missing[0]} no configurada",
        )

    try:
        caps = create_engine(name).capabilities
    except Exception as exc:  # unknown/unavailable engines are reported, not fatal
        return EngineDescriptor(name=name, available=False, error=str(exc))

    return EngineDescriptor(
        name=name,
        available=True,
        locations=list(caps.locations),
        capabilities=EngineCapabilities(
            stock_voices=caps.stock_voices,
            voice_cloning=caps.voice_cloning,
            instruct_voice_design=caps.instruct_voice_design,
            emotions=caps.emotions,
            streaming=caps.streaming,
            supported_languages=list(caps.supported_languages),
        ),
    )


@router.get(
    "",
    response_model=EnginesResponse,
    summary="Listar engines disponibles y dónde pueden ejecutarse",
    description=(
        "Devuelve el catálogo de engines del gateway con sus capacidades y las "
        "ubicaciones permitidas (``local`` en el propio gateway y/o ``cloud``). "
        "El campo ``active`` marca el engine configurado por TTS_ENGINE."
    ),
)
async def list_engines() -> EnginesResponse:
    """Return the engine catalog with capabilities and locations."""
    settings = get_settings()
    engines = [_descriptor(name) for name in KNOWN_ENGINES]
    return EnginesResponse(status="ok", active=settings.TTS_ENGINE, engines=engines)
