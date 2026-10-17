"""Punto de entrada principal de la API TTS multi-engine."""

import asyncio
import logging
import sys

# --- Configuración del Event Loop Policy ---
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
    _EVENT_LOOP_POLICY_NAME = "WindowsProactorEventLoopPolicy"
else:
    asyncio.set_event_loop_policy(asyncio.DefaultEventLoopPolicy())
    _EVENT_LOOP_POLICY_NAME = type(asyncio.get_event_loop_policy()).__name__

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)

logger = logging.getLogger(__name__)

logger.info(
    "Event loop policy configurada: platform=%s, policy=%s",
    sys.platform,
    _EVENT_LOOP_POLICY_NAME,
)


def _force_proactor_loop_factory() -> asyncio.AbstractEventLoop:
    if sys.platform == "win32":
        loop = asyncio.ProactorEventLoop()
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
        return loop
    return asyncio.new_event_loop()


from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from tts_api.api.v1 import conversations, system, tts, voices
from tts_api.core.cleanup import start_cleanup_task, stop_cleanup_task
from tts_api.core.engine_client import (
    reset_shared_engine_client,
    set_shared_engine_client,
)
from tts_api.middleware import APIKeyMiddleware, RequestIDMiddleware
from tts_api.settings import get_settings

# Global engine reference for lifespan
_active_engine = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _active_engine

    try:
        current_loop = asyncio.get_running_loop()
        loop_class = type(current_loop).__name__
        is_proactor = sys.platform == "win32" and isinstance(
            current_loop, asyncio.ProactorEventLoop
        )
        logger.info(
            "Lifespan startup: loop class=%s, is_proactor=%s, platform=%s",
            loop_class,
            is_proactor,
            sys.platform,
        )
        if sys.platform == "win32" and not is_proactor:
            logger.warning(
                "ATENCIÓN: en Windows se requiere ProactorEventLoop para asyncio.subprocess."
            )
    except RuntimeError:
        pass

    settings = get_settings()
    engine_name = settings.TTS_ENGINE
    logger.info("Application startup: inicializando engine '%s'...", engine_name)

    try:
        from tts_api.core.engine_factory import create_engine

        _active_engine = create_engine(engine_name)
        # El cliente queda compartido por el proceso para que todas las
        # peticiones reutilicen el mismo engine (sin recargar pesos).
        await set_shared_engine_client(_active_engine).start()
        logger.info("Engine '%s' inicializado correctamente", _active_engine.name)
    except Exception as e:
        if engine_name == "omnivoice" and settings.TTS_FALLBACK_TO_MOCK:
            logger.exception(
                "Fallo inicializando engine OmniVoice, pero TTS_FALLBACK_TO_MOCK=true: "
                "la API arrancará con mock. Error: %s",
                e,
            )
            from tts_api.core.engines.mock_engine import MockEngine

            _active_engine = MockEngine()
            await set_shared_engine_client(_active_engine).start()
        else:
            logger.exception("Fallo inicializando engine '%s': %s", engine_name, e)
            raise

    # Iniciar cleanup task
    start_cleanup_task(settings.OUTPUTS_DIR, ttl_seconds=settings.OUTPUT_TTL_SECONDS)

    yield

    # Shutdown
    stop_cleanup_task()
    logger.info("Application shutdown: cerrando engine '%s'...", engine_name)
    if _active_engine is not None:
        await _active_engine.close()
        _active_engine = None
    reset_shared_engine_client()


app = FastAPI(
    title="TTS API",
    description="API REST multi-engine para síntesis de voz, clonado y conversaciones",
    version="0.2.0",
    lifespan=lifespan,
    docs_url="/docs",
    redoc_url="/redoc",
    openapi_url="/openapi.json",
    swagger_ui_parameters={
        "docExpansion": "none",
        "filter": True,
        "showExtensions": True,
        "showCommonExtensions": True,
        "syntaxHighlight.theme": "agate",
    },
)

# --- Middleware ---
settings = get_settings()

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.add_middleware(RequestIDMiddleware)
app.add_middleware(APIKeyMiddleware, api_key=settings.API_KEY)

# --- Rate limiting (slowapi) ---
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address

limiter = Limiter(key_func=get_remote_address)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

# --- Prometheus metrics ---
from prometheus_fastapi_instrumentator import Instrumentator

Instrumentator().instrument(app).expose(app, endpoint="/metrics", include_in_schema=False)

# --- Static files (Web GUI) ---
STATIC_DIR = Path(__file__).parent / "static"
if STATIC_DIR.is_dir():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

# --- Routers ---
app.include_router(voices.router, prefix="/api/v1")
app.include_router(tts.router, prefix="/api/v1")
app.include_router(conversations.router, prefix="/api/v1")
app.include_router(system.router, prefix="/api/v1")


@app.get("/api/v1/emotions", tags=["tts"], summary="Listar emociones soportadas")
async def list_emotions() -> list[dict]:
    """Devuelve la lista de emociones soportadas por el engine activo.

    Solo OmniVoice soporta emociones explícitas ([happy], [sad], etc.).
    """
    if _active_engine is None or not _active_engine.capabilities.emotions:
        return []

    descriptions = {
        "happy": "Tono alegre y contento",
        "sad": "Tono melancólico o triste",
        "angry": "Tono enfadado o iracundo",
        "excited": "Tono entusiasmado y enérgico",
        "calm": "Tono sereno y relajado",
        "nervous": "Tono tenso o ansioso",
        "whisper": "Voz susurrada",
        "singing": "Estilo cantado / melódico",
    }
    # OmniVoice-specific emotions
    from tts_api.core.omnivoice_engine import SUPPORTED_EMOTIONS

    return [
        {"id": e, "name": e.capitalize(), "description": descriptions.get(e, "")}
        for e in SUPPORTED_EMOTIONS
    ]


@app.get("/api/v1/health", tags=["Health"])
async def health_check() -> JSONResponse:
    settings = get_settings()
    if _active_engine is None:
        return JSONResponse(
            content={
                "status": "not_initialized",
                "version": settings.APP_VERSION,
                "engine": settings.TTS_ENGINE,
            },
            status_code=503,
        )

    health = await _active_engine.health_check()
    mode = health.get("mode", "UNKNOWN")
    degraded = mode == "MOCK" or not health.get("model_loaded", False)

    return JSONResponse(
        content={
            "status": "degraded" if degraded else "ok",
            "version": settings.APP_VERSION,
            "engine": _active_engine.name,
            "device": health.get("device", "unknown"),
            "mode": mode,
            "stock_voices_count": health.get("stock_voices_count", 0),
            "gpu_available": health.get("gpu_available", False),
        }
    )


@app.get("/api/v1/health/live", tags=["Health"])
async def liveness() -> JSONResponse:
    return JSONResponse(content={"status": "alive"})


@app.get("/api/v1/health/ready", tags=["Health"])
async def readiness() -> JSONResponse:
    settings = get_settings()
    if _active_engine is None:
        return JSONResponse(
            content={"status": "not_ready", "checks": {"engine_initialized": False}},
            status_code=503,
        )

    health = await _active_engine.health_check()
    ready = health.get("model_loaded", False)

    checks = {"engine_initialized": ready, "engine": _active_engine.name}

    # Only check OmniVoice-specific paths for omnivoice engine
    if _active_engine.name == "omnivoice":
        from tts_api.core.engine_paths import default_install_dir

        checks["install_dir_exists"] = default_install_dir().exists()
        checks["venv_python_exists"] = settings.python_bin.exists()
        ready = ready and checks["install_dir_exists"] and checks["venv_python_exists"]

    return JSONResponse(
        content={
            "status": "ready" if ready else "not_ready",
            "checks": checks,
        },
        status_code=200 if ready else 503,
    )


@app.get("/", tags=["Root"])
async def root():
    index = STATIC_DIR / "index.html"
    if index.exists():
        return FileResponse(str(index))
    engine_name = _active_engine.name if _active_engine else "unknown"
    return JSONResponse(
        content={
            "name": "TTS API",
            "version": "0.2.0",
            "engine": engine_name,
            "docs": "/docs",
            "health": "/api/v1/health",
        }
    )


if __name__ == "__main__":
    import uvicorn

    if sys.platform == "win32":
        uvicorn.run(
            "tts_api.main:app",
            host="0.0.0.0",
            port=8000,
            reload=True,
            loop="asyncio",
            factory=True,
        )
    else:
        uvicorn.run(
            "tts_api.main:app",
            host="0.0.0.0",
            port=8000,
            reload=True,
        )
