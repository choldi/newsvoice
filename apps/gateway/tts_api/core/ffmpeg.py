"""Post-procesado de audio con el binario del sistema ``ffmpeg``.

Es el único backend de resample y time-stretch del proyecto. Sustituye a
``librosa``, cuya cadena de dependencias (``numba``) fija o downgradea ``numpy``
y rompe el stack ``torch 2.5.1`` + ``pocket-tts``: ver README.md y
docs/CONVENTIONS.md.

Todas las funciones devuelven ``None`` cuando ``ffmpeg`` no está disponible o
el filtro falla; cada llamante decide con qué error explotar eso.
"""

from __future__ import annotations

import subprocess
from typing import Any

# Rango de valores que acepta el filtro `atempo` de ffmpeg.
_ATEMPO_MIN = 0.5
_ATEMPO_MAX = 2.0


def atempo_chain(speed: float) -> str:
    """Construye el filtro `atempo` de ffmpeg para un factor de velocidad.

    ``ffmpeg`` solo admite ``atempo`` en el rango 0.5-2.0, así que los
    factores fuera de ese rango se descomponen en varios filtros encadenados.

    Args:
        speed: Factor de velocidad (>1 acelera, <1 ralentiza).

    Returns:
        La cadena de filtros para ``-filter:a``.
    """
    remaining = float(speed)
    steps: list[str] = []
    while remaining > _ATEMPO_MAX:
        steps.append(f"atempo={_ATEMPO_MAX}")
        remaining /= _ATEMPO_MAX
    while remaining < _ATEMPO_MIN:
        steps.append(f"atempo={_ATEMPO_MIN}")
        remaining /= _ATEMPO_MIN
    steps.append(f"atempo={remaining:.6g}")
    return ",".join(steps)


def _filter_pcm(
    data: Any,
    sample_rate: int,
    audio_filter: str,
    target_rate: int,
) -> Any | None:
    """Aplica un filtro `-filter:a` sobre PCM float32 mono.

    Args:
        data: Señal mono de muestras float32.
        sample_rate: Frecuencia de muestreo de entrada en Hz.
        audio_filter: Filtro ffmpeg (p. ej. ``atempo=1.2``).
        target_rate: Frecuencia de muestreo de salida en Hz.

    Returns:
        La señal procesada, o ``None`` si no hay ``ffmpeg`` o el filtro falla.
    """
    import numpy as np

    command = [
        "ffmpeg",
        "-v",
        "error",
        "-f",
        "f32le",
        "-ar",
        str(sample_rate),
        "-ac",
        "1",
        "-i",
        "pipe:0",
        "-filter:a",
        audio_filter,
        "-f",
        "f32le",
        "-ac",
        "1",
        "-ar",
        str(target_rate),
        "pipe:1",
    ]
    try:
        completed = subprocess.run(
            command,
            input=np.asarray(data, dtype=np.float32).tobytes(),
            capture_output=True,
            check=False,
        )
    except (FileNotFoundError, OSError):
        return None
    if completed.returncode != 0 or not completed.stdout:
        return None
    return np.frombuffer(completed.stdout, dtype=np.float32)


def time_stretch(data: Any, sample_rate: int, speed: float) -> Any | None:
    """Acelera o ralentiza la señal conservando el tono (``atempo``).

    Args:
        data: Señal mono de muestras float32.
        sample_rate: Frecuencia de muestreo en Hz.
        speed: Factor de velocidad (>1 acelera, <1 ralentiza).

    Returns:
        La señal estirada, o ``None`` si ``speed`` no es válido o ffmpeg falla.
    """
    if speed <= 0:
        return None
    return _filter_pcm(data, sample_rate, atempo_chain(speed), sample_rate)


def resample(data: Any, sample_rate: int, target_rate: int) -> Any | None:
    """Remuestrea la señal al rate objetivo sin cambiar el tono (``aresample``).

    Args:
        data: Señal mono de muestras float32.
        sample_rate: Frecuencia de muestreo de entrada en Hz.
        target_rate: Frecuencia de muestreo de salida en Hz.

    Returns:
        La señal remuestreada, la original si no hay que remuestrear, o
        ``None`` si ffmpeg falla.
    """
    if sample_rate == target_rate:
        return data
    return _filter_pcm(data, sample_rate, f"aresample={target_rate}", target_rate)
