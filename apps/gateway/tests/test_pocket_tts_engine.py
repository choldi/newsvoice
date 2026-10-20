"""Tests unitarios del engine Pocket TTS (sin cargar el modelo real)."""

from __future__ import annotations

import io
import shutil
import wave
from pathlib import Path

import pytest

from tts_api.core import ffmpeg
from tts_api.core.engines.pocket_tts_engine import (
    _POCKET_LANGUAGE_MODELS,
    PocketTTSEngine,
)
from tts_api.core.exceptions import EngineUnavailableError, VoiceNotFoundError


def _make_wav_bytes(sample_rate: int = 24000, duration_sec: float = 1.0) -> bytes:
    """Genera bytes WAV mono PCM16 válidos."""
    num_samples = int(duration_sec * sample_rate)
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav_file:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(sample_rate)
        wav_file.writeframes(b"\x00\x00" * num_samples)
    return buffer.getvalue()


def _wav_duration_sec(wav_bytes: bytes) -> float:
    """Devuelve la duración en segundos de un WAV PCM16."""
    with wave.open(io.BytesIO(wav_bytes), "rb") as wav_file:
        return wav_file.getnframes() / wav_file.getframerate()


class _FakeModel:
    """Sustituto del modelo de pocket-tts para tests."""

    sample_rate = 24000

    def __init__(self) -> None:
        self.prompt_calls = 0
        self.generate_calls = 0

    def get_state_for_audio_prompt(self, path: str) -> str:
        self.prompt_calls += 1
        return f"state-{self.prompt_calls}"

    def generate_audio(self, state: str, text: str) -> list[float]:
        self.generate_calls += 1
        return [0.0] * self.sample_rate  # 1 s de audio


# ---------------------------------------------------------------------------
# POCKET_TTS_MODEL -> override global de load_model()
# ---------------------------------------------------------------------------


def test_model_override_vacio_usa_checkpoint_por_idioma(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Sin override, el idioma se resuelve con el checkpoint por idioma."""
    monkeypatch.setenv("POCKET_TTS_MODEL", "")
    engine = PocketTTSEngine()
    assert engine._language_kwargs("es") == {"language": "spanish"}
    assert engine._language_kwargs("fr") == {"language": "french"}
    assert engine._language_kwargs("en") == {"language": "english"}


def test_model_override_acepta_config_yaml(monkeypatch: pytest.MonkeyPatch) -> None:
    """Una URL de config YAML se traduce a config= (override global)."""
    monkeypatch.setenv("POCKET_TTS_MODEL", "hf://repo/config.yaml")
    engine = PocketTTSEngine()
    assert engine._language_kwargs("es") == {"config": "hf://repo/config.yaml"}


def test_model_override_acepta_ruta_local(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Una ruta local existente a YAML se traduce a config=."""
    config = tmp_path / "voice.yaml"
    config.write_text("language: english\n")
    monkeypatch.setenv("POCKET_TTS_MODEL", str(config))
    engine = PocketTTSEngine()
    assert engine._language_kwargs("es") == {"config": str(config)}


def test_model_override_ignora_repo_id(monkeypatch: pytest.MonkeyPatch) -> None:
    """Un repo id de HF sin .yaml no sirve: se ignora (checkpoint por idioma)."""
    monkeypatch.setenv("POCKET_TTS_MODEL", "kyutai/pocket-tts-100m-en")
    engine = PocketTTSEngine()
    assert engine._language_kwargs("en") == {"language": "english"}


def test_model_override_en_blanco(monkeypatch: pytest.MonkeyPatch) -> None:
    """Un valor en blanco se trata como no configurado."""
    monkeypatch.setenv("POCKET_TTS_MODEL", "   ")
    engine = PocketTTSEngine()
    assert engine._model_kwargs() == {}


# ---------------------------------------------------------------------------
# Selección de idioma por defecto
# ---------------------------------------------------------------------------


def test_default_language_por_defecto_es(monkeypatch: pytest.MonkeyPatch) -> None:
    """Sin configurar, el idioma por defecto es español."""
    monkeypatch.setenv("POCKET_TTS_DEFAULT_LANGUAGE", "")
    engine = PocketTTSEngine()
    assert engine.default_language == "es"


def test_default_language_respeta_configurado(monkeypatch: pytest.MonkeyPatch) -> None:
    """Un idioma configurado válido se respeta."""
    monkeypatch.setenv("POCKET_TTS_DEFAULT_LANGUAGE", "fr")
    engine = PocketTTSEngine()
    assert engine.default_language == "fr"


def test_default_language_ignora_idioma_invalido(monkeypatch: pytest.MonkeyPatch) -> None:
    """Un idioma sin checkpoint cae a español."""
    monkeypatch.setenv("POCKET_TTS_DEFAULT_LANGUAGE", "xx")
    engine = PocketTTSEngine()
    assert engine.default_language == "es"


# ---------------------------------------------------------------------------
# Mapa de idioma -> checkpoint
# ---------------------------------------------------------------------------


def test_language_model_map_cubre_es_fr_en() -> None:
    """Los idiomas principales tienen checkpoint dedicado."""
    assert _POCKET_LANGUAGE_MODELS["es"] == "spanish"
    assert _POCKET_LANGUAGE_MODELS["fr"] == "french"
    assert _POCKET_LANGUAGE_MODELS["en"] == "english"


# ---------------------------------------------------------------------------
# Referencias ES/FR
# ---------------------------------------------------------------------------


def test_references_vacio_sin_configurar(monkeypatch: pytest.MonkeyPatch) -> None:
    """Sin configurar no hay referencias."""
    monkeypatch.setenv("POCKET_TTS_REFERENCES", "")
    engine = PocketTTSEngine()
    assert engine._settings.pocket_tts_references == {}


def test_references_parsea_json(monkeypatch: pytest.MonkeyPatch) -> None:
    """Las referencias se parsean desde JSON."""
    monkeypatch.setenv("POCKET_TTS_REFERENCES", '{"es_male": "/tmp/es.wav"}')
    engine = PocketTTSEngine()
    assert engine._settings.pocket_tts_references == {"es_male": "/tmp/es.wav"}


def test_references_ignora_json_invalido(monkeypatch: pytest.MonkeyPatch) -> None:
    """Un JSON inválido se ignora en vez de reventar."""
    monkeypatch.setenv("POCKET_TTS_REFERENCES", "not json")
    engine = PocketTTSEngine()
    assert engine._settings.pocket_tts_references == {}


# ---------------------------------------------------------------------------
# speed
# ---------------------------------------------------------------------------


def test_apply_speed_identico_con_uno() -> None:
    """speed=1.0 devuelve el WAV original sin re-codificar."""
    wav = _make_wav_bytes()
    engine = PocketTTSEngine()
    assert engine._apply_speed(wav, 1.0) is wav


@pytest.mark.parametrize("speed", [0.0, -1.0])
def test_apply_speed_ignora_valores_invalidos(speed: float) -> None:
    """speed no positivo se ignora en vez de reventar."""
    wav = _make_wav_bytes()
    engine = PocketTTSEngine()
    assert engine._apply_speed(wav, speed) is wav


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg no disponible")
def test_apply_speed_acelera() -> None:
    """speed>1 acorta la duración manteniendo el sample rate."""
    wav = _make_wav_bytes(duration_sec=2.0)
    engine = PocketTTSEngine()
    result = engine._apply_speed(wav, 1.5)

    duration = _wav_duration_sec(result)
    assert duration == pytest.approx(2.0 / 1.5, rel=0.1)

    with wave.open(io.BytesIO(result), "rb") as wav_file:
        assert wav_file.getframerate() == 24000
        assert wav_file.getnchannels() == 1


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg no disponible")
def test_apply_speed_ralentiza() -> None:
    """speed<1 alarga la duración."""
    wav = _make_wav_bytes(duration_sec=1.0)
    engine = PocketTTSEngine()
    result = engine._apply_speed(wav, 0.5)

    assert _wav_duration_sec(result) == pytest.approx(2.0, rel=0.1)


def test_apply_speed_falla_sin_ffmpeg(monkeypatch: pytest.MonkeyPatch) -> None:
    """Sin ffmpeg el time-stretch falla con un error explícito, no en silencio."""
    monkeypatch.setattr(ffmpeg, "time_stretch", lambda *_args, **_kwargs: None)
    engine = PocketTTSEngine()

    with pytest.raises(EngineUnavailableError, match="speed"):
        engine._apply_speed(_make_wav_bytes(), 1.5)


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg no disponible")
async def test_synthesize_aplica_speed(monkeypatch: pytest.MonkeyPatch) -> None:
    """synthesize() aplica la velocidad indicada al audio generado.

    Una voz stock EN (preset) carga perezosamente el modelo inglés.
    """
    engine = PocketTTSEngine()
    fake = _FakeModel()
    monkeypatch.setattr(engine, "_get_model", _fake_get_model(fake))

    wav = await engine.synthesize(
        text="Hello world",
        voice_id="en-us-male",
        language="en",
        speed=2.0,
    )

    assert _wav_duration_sec(wav) == pytest.approx(0.5, rel=0.15)


async def test_synthesize_usa_idioma_de_la_voz(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """El idioma de la voz manda sobre el idioma pedido en la petición."""
    reference = tmp_path / "es_male.wav"
    reference.write_bytes(_make_wav_bytes())
    monkeypatch.setenv("POCKET_TTS_REFERENCES", f'{{"es_male": "{reference}"}}')
    engine = PocketTTSEngine()
    fake = _FakeModel()
    loaded: list[str] = []

    async def _get_model(language: str) -> _FakeModel:
        loaded.append(language)
        return fake

    monkeypatch.setattr(engine, "_get_model", _get_model)

    await engine.synthesize(text="Hola", voice_id="es-mx-male", language="en")

    assert "es" in loaded


async def test_synthesize_voz_es_sin_referencia_falla(monkeypatch: pytest.MonkeyPatch) -> None:
    """Una voz ES sin referencia configurada falla con error explícito."""
    monkeypatch.setenv("POCKET_TTS_REFERENCES", "")
    engine = PocketTTSEngine()
    monkeypatch.setattr(engine, "_get_model", _fake_get_model(_FakeModel()))

    with pytest.raises(EngineUnavailableError, match="referencia"):
        await engine.synthesize(text="Hola", voice_id="es-mx-male", language="es")


async def test_synthesize_voz_es_con_referencia(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Una voz ES con referencia configurada renderiza vía clonación."""
    reference = tmp_path / "es_male.wav"
    reference.write_bytes(_make_wav_bytes())
    monkeypatch.setenv("POCKET_TTS_REFERENCES", f'{{"es_male": "{reference}"}}')
    engine = PocketTTSEngine()
    fake = _FakeModel()
    monkeypatch.setattr(engine, "_get_model", _fake_get_model(fake))

    wav = await engine.synthesize(text="Hola", voice_id="es-mx-male", language="es")

    assert fake.prompt_calls == 1
    assert _wav_duration_sec(wav) == pytest.approx(1.0, rel=0.15)


async def test_synthesize_voz_desconocida() -> None:
    """Una voz fuera del catálogo stock da VoiceNotFoundError."""
    engine = PocketTTSEngine()
    with pytest.raises(VoiceNotFoundError):
        await engine.synthesize(text="x", voice_id="nope", language="es")


def _fake_get_model(fake: _FakeModel):
    """Devuelve un sustituto async de ``PocketTTSEngine._get_model``."""

    async def _get_model(_language: str) -> _FakeModel:
        return fake

    return _get_model


# ---------------------------------------------------------------------------
# Caché de estado de clonado
# ---------------------------------------------------------------------------


def test_clone_state_se_cachea(tmp_path: Path) -> None:
    """El estado del prompt se calcula una sola vez por referencia."""
    reference = tmp_path / "ref.wav"
    reference.write_bytes(_make_wav_bytes())

    engine = PocketTTSEngine()
    fake = _FakeModel()

    first = engine._clone_state(fake, str(reference))
    second = engine._clone_state(fake, str(reference))

    assert first is second
    assert fake.prompt_calls == 1


def test_clone_state_se_invalida_si_cambia_la_referencia(tmp_path: Path) -> None:
    """Cambiar la referencia invalida el estado cacheado."""
    reference = tmp_path / "ref.wav"
    reference.write_bytes(_make_wav_bytes())

    engine = PocketTTSEngine()
    fake = _FakeModel()

    engine._clone_state(fake, str(reference))
    reference.write_bytes(_make_wav_bytes(duration_sec=2.0))
    engine._clone_state(fake, str(reference))

    assert fake.prompt_calls == 2
