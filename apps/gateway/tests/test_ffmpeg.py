"""Tests del backend ffmpeg (resample y time-stretch) sin librosa."""

from __future__ import annotations

import shutil
from typing import Any

import numpy as np
import pytest

from tts_api.core import ffmpeg

requires_ffmpeg = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg no disponible")


def _tone(sample_rate: int = 24000, duration_sec: float = 2.0, freq: float = 220.0) -> np.ndarray:
    """Genera una señal mono float32 de `freq` Hz."""
    samples = np.arange(int(duration_sec * sample_rate)) / sample_rate
    return (0.4 * np.sin(2 * np.pi * freq * samples)).astype(np.float32)


def _peak_hz(data: np.ndarray, sample_rate: int) -> float:
    """Frecuencia dominante de la señal (FFT)."""
    spectrum = np.abs(np.fft.rfft(data))
    freqs = np.fft.rfftfreq(len(data), 1 / sample_rate)
    return float(freqs[int(np.argmax(spectrum))])


# ---------------------------------------------------------------------------
# atempo chain
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("speed", "expected"),
    [
        (1.0, "atempo=1"),
        (1.2, "atempo=1.2"),
        (4.0, "atempo=2.0,atempo=2"),
        (0.25, "atempo=0.5,atempo=0.5"),
    ],
)
def test_atempo_chain_encadena_fuera_de_rango(speed: float, expected: str) -> None:
    """ffmpeg solo admite atempo 0.5-2.0: fuera de rango se encadena."""
    assert ffmpeg.atempo_chain(speed) == expected


def test_time_stretch_llama_a_ffmpeg_con_el_filtro(monkeypatch: pytest.MonkeyPatch) -> None:
    """La llamada a ffmpeg incluye el binario y el filtro atempo encadenado."""
    captured: dict[str, Any] = {}

    class _Result:
        returncode = 0
        stdout = b"\x00\x00\x00\x00" * 8

    def fake_run(command: list[str], **_kwargs: Any) -> Any:
        captured["command"] = command
        return _Result()

    monkeypatch.setattr(ffmpeg.subprocess, "run", fake_run)
    result = ffmpeg.time_stretch(np.zeros(64, dtype=np.float32), 24000, 4.0)

    assert result is not None
    assert captured["command"][0] == "ffmpeg"
    assert "atempo=2.0,atempo=2" in captured["command"]


# ---------------------------------------------------------------------------
# contrato
# ---------------------------------------------------------------------------


def test_resample_devuelve_la_misma_senal_si_no_hay_que_remuestrear() -> None:
    """Mismo rate de entrada y salida no paga el coste de un subproceso."""
    data = _tone()
    assert ffmpeg.resample(data, 24000, 24000) is data


def test_time_stretch_rechaza_speed_no_positivo() -> None:
    """Un speed no positivo devuelve None en vez de invocar ffmpeg."""
    assert ffmpeg.time_stretch(_tone(), 24000, 0.0) is None
    assert ffmpeg.time_stretch(_tone(), 24000, -1.0) is None


def test_sin_binario_ffmpeg_devuelve_none(monkeypatch: pytest.MonkeyPatch) -> None:
    """Sin el binario instalado las funciones devuelven None, no lanzan."""

    def fake_run(*_args: Any, **_kwargs: Any) -> Any:
        raise FileNotFoundError("ffmpeg")

    monkeypatch.setattr(ffmpeg.subprocess, "run", fake_run)

    assert ffmpeg.time_stretch(_tone(), 24000, 1.2) is None
    assert ffmpeg.resample(_tone(), 24000, 22050) is None


# ---------------------------------------------------------------------------
# integración con el binario real
# ---------------------------------------------------------------------------


@requires_ffmpeg
def test_time_stretch_real_acelera_conservando_el_tono() -> None:
    """1.5x acorta la duración y no mueve el tono de entrada."""
    sample_rate = 24000
    stretched = ffmpeg.time_stretch(_tone(sample_rate), sample_rate, 1.5)

    assert stretched is not None
    assert len(stretched) / sample_rate == pytest.approx(2.0 / 1.5, rel=0.05)
    assert _peak_hz(stretched, sample_rate) == pytest.approx(220.0, abs=5.0)


@requires_ffmpeg
def test_resample_real_cambia_el_sample_rate() -> None:
    """4000 -> 22050 conserva la duración aproximada de la señal."""
    source = _tone(sample_rate=4000, duration_sec=1.0)
    resampled = ffmpeg.resample(source, 4000, 22050)

    assert resampled is not None
    assert len(resampled) / 22050 == pytest.approx(1.0, rel=0.05)
