"""Tests unitarios del engine Pocket TTS (sin cargar el modelo real)."""

from __future__ import annotations

import io
import shutil
import wave
from pathlib import Path

import pytest

from tts_api.core import ffmpeg
from tts_api.core.engines.pocket_tts_engine import PocketTTSEngine
from tts_api.core.exceptions import EngineUnavailableError


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
# POCKET_TTS_MODEL -> argumentos de load_model()
# ---------------------------------------------------------------------------


def test_model_kwargs_acepta_codigo_de_idioma(monkeypatch: pytest.MonkeyPatch) -> None:
    """Un código de idioma se traduce a language=."""
    monkeypatch.setenv("POCKET_TTS_MODEL", "english")
    engine = PocketTTSEngine()
    assert engine._model_kwargs() == {"language": "english"}


def test_model_kwargs_acepta_config_yaml(monkeypatch: pytest.MonkeyPatch) -> None:
    """Una URL de config YAML se traduce a config=."""
    monkeypatch.setenv("POCKET_TTS_MODEL", "hf://repo/config.yaml")
    engine = PocketTTSEngine()
    assert engine._model_kwargs() == {"config": "hf://repo/config.yaml"}


def test_model_kwargs_acepta_ruta_local(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Una ruta local existente a YAML se traduce a config=."""
    config = tmp_path / "voice.yaml"
    config.write_text("language: english\n")
    monkeypatch.setenv("POCKET_TTS_MODEL", str(config))
    engine = PocketTTSEngine()
    assert engine._model_kwargs() == {"config": str(config)}


def test_model_kwargs_ignora_repo_id(monkeypatch: pytest.MonkeyPatch) -> None:
    """Un repo id de HF sin .yaml no sirve: se ignora (config por defecto)."""
    monkeypatch.setenv("POCKET_TTS_MODEL", "kyutai/pocket-tts-100m-en")
    engine = PocketTTSEngine()
    assert engine._model_kwargs() == {}


def test_model_kwargs_usa_el_idioma_por_defecto(monkeypatch: pytest.MonkeyPatch) -> None:
    """Sin valor configurado se cae en el idioma por defecto (english)."""
    monkeypatch.delenv("POCKET_TTS_MODEL", raising=False)
    engine = PocketTTSEngine()
    assert engine._model_kwargs() == {"language": "english"}


def test_model_kwargs_vacio(monkeypatch: pytest.MonkeyPatch) -> None:
    """Un valor en blanco se trata como no configurado."""
    monkeypatch.setenv("POCKET_TTS_MODEL", "   ")
    engine = PocketTTSEngine()
    assert engine._model_kwargs() == {}


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
async def test_synthesize_aplica_speed() -> None:
    """synthesize() aplica la velocidad indicada al audio generado."""
    engine = PocketTTSEngine()
    engine._model = _FakeModel()
    engine._voice_states["giovanni"] = object()

    wav = await engine.synthesize(
        text="Hola mundo",
        voice_id="en-us-male",
        language="en",
        speed=2.0,
    )

    assert _wav_duration_sec(wav) == pytest.approx(0.5, rel=0.15)


# ---------------------------------------------------------------------------
# Caché de estado de clonado
# ---------------------------------------------------------------------------


def test_clone_state_se_cachea(tmp_path: Path) -> None:
    """El estado del prompt se calcula una sola vez por referencia."""
    reference = tmp_path / "ref.wav"
    reference.write_bytes(_make_wav_bytes())

    engine = PocketTTSEngine()
    engine._model = _FakeModel()

    first = engine._clone_state(str(reference))
    second = engine._clone_state(str(reference))

    assert first is second
    assert engine._model.prompt_calls == 1


def test_clone_state_se_invalida_si_cambia_la_referencia(tmp_path: Path) -> None:
    """Cambiar la referencia invalida el estado cacheado."""
    reference = tmp_path / "ref.wav"
    reference.write_bytes(_make_wav_bytes())

    engine = PocketTTSEngine()
    engine._model = _FakeModel()

    engine._clone_state(str(reference))
    reference.write_bytes(_make_wav_bytes(duration_sec=2.0))
    engine._clone_state(str(reference))

    assert engine._model.prompt_calls == 2
