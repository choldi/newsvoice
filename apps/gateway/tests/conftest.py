"""Configuración base de pytest y fixtures compartidas."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from tts_api.core.engine_client import (
    AudioResult,
    EngineHealth,
    OmniVoiceEngineClient,
    StockVoice,
    reset_shared_engine_client,
)
from tts_api.core.omnivoice_engine import GenerationParams, OmniVoiceEngine
from tts_api.main import app


@pytest_asyncio.fixture
async def async_client() -> AsyncIterator[AsyncClient]:
    """Cliente HTTP asíncrono para tests de integración.

    Ejecuta el lifespan de FastAPI manualmente, porque ``httpx``
    ``ASGITransport`` no lo dispara: sin él, ``main._active_engine``
    queda ``None`` y los endpoints de health/ready/emotions no responden.
    """
    transport = ASGITransport(app=app)
    async with (
        app.router.lifespan_context(app),
        AsyncClient(transport=transport, base_url="http://test") as client,
    ):
        yield client


@pytest.fixture(scope="session")
def event_loop():
    import asyncio

    loop = asyncio.get_event_loop_policy().new_event_loop()
    yield loop
    loop.close()


@pytest.fixture(autouse=True)
def _isolate_external_install(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Redirige rutas de instalación externa a tmp_path y fuerza modo mock."""
    import sys

    fake_install = tmp_path / "OMNIVOICE"
    fake_venv = tmp_path / "omnivoice_env"
    fake_install.mkdir()
    fake_venv.mkdir()

    scripts = fake_venv / ("Scripts" if sys.platform == "win32" else "bin")
    scripts.mkdir(parents=True, exist_ok=True)
    (scripts / ("python.exe" if sys.platform == "win32" else "python")).touch()

    monkeypatch.setenv("TTS_INSTALL_DIR", str(fake_install))
    monkeypatch.setenv("TTS_VENV_DIR", str(fake_venv))
    monkeypatch.setenv("TTS_USE_MOCK", "true")
    monkeypatch.setenv("TTS_FALLBACK_TO_MOCK", "false")
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'test.db'}")

    # El cliente de engine es un singleton de proceso: se limpia en cada test
    # para que un mock no se filtre al test siguiente.
    reset_shared_engine_client()

    yield

    import tts_api.core.omnivoice_engine as engine_mod

    engine_mod._engine_instance = None
    OmniVoiceEngine._instance = None
    OmniVoiceEngine._initialized = False
    reset_shared_engine_client()


# ---------------------------------------------------------------------------
# Mock del engine client
# ---------------------------------------------------------------------------


class FakeEngineClient(OmniVoiceEngineClient):
    """Implementación falsa del engine client para tests."""

    def __init__(self) -> None:
        self.started = False
        self.stopped = False
        self._voices: list[StockVoice] = [
            StockVoice(voice_id="es-mx-male", language="es", gender="male", name="Carlos"),
            StockVoice(voice_id="es-mx-female", language="es", gender="female", name="Ana"),
            StockVoice(voice_id="en-us-male", language="en", gender="male", name="John"),
        ]

    async def start(self) -> None:
        self.started = True

    async def stop(self) -> None:
        self.stopped = True

    async def health(self) -> EngineHealth:
        return EngineHealth(
            reachable=True, model_loaded=True, gpu_available=True, vram_free_mb=4096
        )

    async def list_stock_voices(self, language: str | None = None) -> list[StockVoice]:
        if language is None:
            return list(self._voices)
        return [v for v in self._voices if v.language == language]

    async def synthesize_stock(
        self,
        *,
        text: str,
        voice_id: str,
        speed: float = 1.0,
        generation_params: GenerationParams | None = None,
    ) -> AudioResult:
        wav = (
            b"RIFF$\x00\x00\x00WAVEfmt \x10\x00\x00\x00"
            b"\x01\x00\x01\x00\x44\xac\x00\x00\x88\x58\x01\x00"
            b"\x02\x00\x10\x00data\x00\x00\x00\x00"
        )
        return AudioResult(wav_bytes=wav, duration_sec=0.5, sample_rate=22050)

    async def synthesize_instruct(
        self,
        *,
        text: str,
        instruct: str,
        speed: float = 1.0,
        generation_params: GenerationParams | None = None,
    ) -> AudioResult:
        wav = (
            b"RIFF$\x00\x00\x00WAVEfmt \x10\x00\x00\x00"
            b"\x01\x00\x01\x00\x44\xac\x00\x00\x88\x58\x01\x00"
            b"\x02\x00\x10\x00data\x00\x00\x00\x00"
        )
        return AudioResult(wav_bytes=wav, duration_sec=0.5, sample_rate=22050)

    async def synthesize_clone(
        self,
        *,
        text: str,
        reference_audio_path: str,
        instruct: str | None = None,
        speed: float = 1.0,
        generation_params: GenerationParams | None = None,
    ) -> AudioResult:
        wav = (
            b"RIFF$\x00\x00\x00WAVEfmt \x10\x00\x00\x00"
            b"\x01\x00\x01\x00\x44\xac\x00\x00\x88\x58\x01\x00"
            b"\x02\x00\x10\x00data\x00\x00\x00\x00"
        )
        return AudioResult(wav_bytes=wav, duration_sec=0.5, sample_rate=22050)


@pytest.fixture
def fake_engine_client() -> FakeEngineClient:
    return FakeEngineClient()


@pytest.fixture
def mock_engine_client(monkeypatch: pytest.MonkeyPatch) -> FakeEngineClient:
    fake = FakeEngineClient()
    monkeypatch.setattr(
        "tts_api.core.engine_client.OmniVoiceEngineClient",
        lambda: fake,
    )
    return fake


@pytest.fixture
def sample_text() -> str:
    return "Hola, esto es una prueba de síntesis de voz."


@pytest.fixture
def sample_voice_id() -> str:
    return "es-mx-male"


@pytest.fixture
def sample_audio_bytes() -> bytes:
    return (
        b"RIFF$\x00\x00\x00WAVEfmt \x10\x00\x00\x00"
        b"\x01\x00\x01\x00\x44\xac\x00\x00\x88\x58\x01\x00"
        b"\x02\x00\x10\x00data\x00\x00\x00\x00"
    )
