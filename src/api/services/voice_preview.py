"""Short audio previews for any selectable voice (Phase 9.4).

One click on "Probar" must play a few seconds of any voice of any engine
without going through a whole digest: resolve the voice with
:func:`voice_for`, synthesize a fixed phrase with
:func:`~src.api.services.tts_backends.synthesize`, and cache the result
under ``data/voices/samples/`` so the second play is instant.

When Modal is switched off — or its call fails — a catalog voice falls back
to its reference WAV, which is exactly what the Phase 9 prerrequisitos
documented: without ``MODAL_ENABLED``, "Probar" reproduces the local
reference.
"""

from __future__ import annotations

import re
from pathlib import Path

import structlog

from src.api.services.audio_files import atomic_write
from src.api.services.tts_backends import (
    TTSBackend,
    ext_for,
    is_modal,
    resolve_backend,
    synthesize,
    voice_for,
)
from src.api.services.voice_catalog_admin import reference_wav
from src.api.services.voice_engine import engines_for_voice
from src.core.config import get_settings
from src.core.voice_catalog import VoiceCatalogError, load_profiles, sha256_text

logger = structlog.get_logger(__name__)

#: Where cached samples live, under ``settings.voices_dir`` (gitignored).
SAMPLES_DIRNAME = "samples"

#: Preview budget: a sample must fail fast, never hold the click for minutes.
SAMPLE_TIMEOUT_SECONDS = 30.0

#: Engine order tried for a voice that several engines accept: Modal first
#: (it is the catalog's native engine), then the gateway, then Edge.
_PREFERENCE: tuple[TTSBackend, ...] = (
    TTSBackend.QWEN3_TTS,
    TTSBackend.ZONOS2,
    TTSBackend.OMNIVOICE,
    TTSBackend.EDGE,
)

#: Safe on-disk names for cached samples: slug, engine, digest, extension.
_SAMPLE_FILE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,80}\.(?:mp3|wav)$")

#: Fixed preview phrases per language (kept short so the click answers fast).
_SAMPLE_TEXTS = {
    "es": (
        "Hola, soy tu profe de inglés de siempre. "
        "Hoy practicamos el presente simple con ejemplos del día a día."
    ),
    "en": (
        "Hello! This is a quick voice preview from your news engine. Listen to how I sound today."
    ),
}

_MEDIA_TYPES = {".mp3": "audio/mpeg", ".wav": "audio/wav"}


class VoicePreviewError(RuntimeError):
    """No engine could produce a playable sample for this voice."""


def samples_dir() -> Path:
    """Return the directory holding cached preview samples.

    Returns:
        ``<voices_dir>/samples`` (created on first write).
    """
    return Path(get_settings().voices_dir) / SAMPLES_DIRNAME


def media_type_for(filename: str) -> str:
    """Return the media type of a cached sample file name.

    Args:
        filename: Sample file name (already validated by :func:`cached_sample`).

    Returns:
        ``audio/mpeg`` for ``.mp3``, ``audio/wav`` for ``.wav``.
    """
    return _MEDIA_TYPES.get(Path(filename).suffix, "application/octet-stream")


def cached_sample(filename: str) -> Path | None:
    """Resolve a cached sample path, refusing anything unsafe.

    Args:
        filename: Sample file name coming from a URL path.

    Returns:
        The path when the name is well-formed and the file exists inside the
        samples directory, None otherwise.
    """
    if not _SAMPLE_FILE.match(filename):
        return None
    root = samples_dir().resolve()
    path = (root / filename).resolve()
    if not path.is_relative_to(root):
        return None
    return path if path.is_file() else None


def sample_filename(voice: str, backend: TTSBackend, language: str, text: str) -> str:
    """Build the deterministic cache file name of one preview.

    Args:
        voice: Voice id as selected.
        backend: Engine that produced (or will produce) the audio.
        language: Language code used for the phrase.
        text: The preview phrase.

    Returns:
        ``<slug>-<engine>-<digest><ext>`` — different voice, engine or text
        always lands on a different file.
    """
    slug = re.sub(r"[^a-z0-9_-]+", "-", voice.lower()).strip("-")[:48] or "voz"
    digest = sha256_text(f"{backend.value}|{voice}|{language}|{text}")[:12]
    return f"{slug}-{backend.value}-{digest}{ext_for(backend)}"


def _language_hint(voice: str) -> str:
    """Guess the language of a voice id without touching the network.

    Args:
        voice: Voice id as selected.

    Returns:
        Two-letter code: the catalog profile's language when the voice is a
        profile name, the locale prefix of an Edge/gateway id when it has
        one, else the configured default language.
    """
    try:
        profiles = load_profiles()
    except VoiceCatalogError:
        profiles = []
    for profile in profiles:
        if profile.name == voice:
            return profile.language
    match = re.match(r"^([A-Za-z]{2})[-_]", voice)
    if match:
        return match.group(1).lower()
    return get_settings().tts_language or "es"


def _sample_text(language: str) -> str:
    """Return the preview phrase for a language.

    Args:
        language: Language code, possibly with a region ('es-MX').

    Returns:
        The phrase; Spanish when the language has no phrase of its own.
    """
    return _SAMPLE_TEXTS.get(language.split("-")[0].lower(), _SAMPLE_TEXTS["es"])


def _candidates(voice: str) -> list[TTSBackend]:
    """Return the engines to try for a voice, in preference order.

    Args:
        voice: Voice id as selected.

    Returns:
        The accepting engines in preference order.

    Raises:
        VoicePreviewError: When the id belongs to no engine's vocabulary —
            previewing it would silently play a different voice.
    """
    engines = engines_for_voice(voice)
    if not engines:
        raise VoicePreviewError(f"voz desconocida: {voice!r} no pertenece a ningún motor")
    ordered = [backend for backend in _PREFERENCE if backend.value in engines]
    if voice.endswith("Neural"):
        ordered.sort(key=lambda backend: backend is not TTSBackend.EDGE)
    return ordered or [resolve_backend(None)]


def _result(voice: str, backend: TTSBackend, url: str, note: str) -> dict[str, str]:
    """Build the preview response payload shared by every outcome.

    Args:
        voice: Voice id that was previewed.
        backend: Engine used (or attempted) for the preview.
        url: Playable URL: a cached sample or the reference audio.
        note: Human-readable outcome shown next to the player.

    Returns:
        Dict rendered by ``partials/_voice_sample.html``.
    """
    return {"voice": voice, "engine": backend.value, "url": url, "note": note}


async def preview(voice: str) -> dict[str, str]:
    """Produce (or reuse) a short playable sample for one voice.

    Args:
        voice: Voice id as selected in the "Probar voces" picker.

    Returns:
        Dict with ``voice``, ``engine``, ``url`` and ``note``.

    Raises:
        VoicePreviewError: When the voice is unknown or every engine failed.
    """
    name = (voice or "").strip()
    if not name:
        raise VoicePreviewError("selecciona una voz para probar")
    language = _language_hint(name)
    text = _sample_text(language)
    errors: list[str] = []
    for backend in _candidates(name):
        filename = sample_filename(name, backend, language, text)
        cached = cached_sample(filename)
        if cached is not None:
            return _result(name, backend, f"/ui/voices/sample/{filename}", "muestra en caché")
        if is_modal(backend) and not get_settings().modal_enabled:
            if reference_wav(name) is not None:
                return _result(
                    name,
                    backend,
                    f"/ui/voices/audio/{name}.wav",
                    "wav de referencia (modal apagado)",
                )
            errors.append(f"{backend.value}: modal apagado y sin wav de referencia")
            continue
        try:
            resolved = await voice_for(backend, name, language)
            audio = await synthesize(
                backend, text, resolved, language, timeout=SAMPLE_TIMEOUT_SECONDS
            )
        except Exception as exc:  # noqa: BLE001 - the next engine must still be tried
            logger.info(
                "voice_preview_engine_failed",
                voice=name,
                backend=backend.value,
                error=str(exc),
            )
            if is_modal(backend) and reference_wav(name) is not None:
                return _result(
                    name,
                    backend,
                    f"/ui/voices/audio/{name}.wav",
                    "wav de referencia (fallo de síntesis)",
                )
            errors.append(f"{backend.value}: {exc}")
            continue
        if not audio:
            errors.append(f"{backend.value}: sin audio")
            continue
        target = samples_dir() / filename
        atomic_write(target, audio)
        return _result(name, backend, f"/ui/voices/sample/{filename}", "muestra sintetizada")
    raise VoicePreviewError(
        "no se pudo probar la voz: " + ("; ".join(errors) or "sin motores disponibles")
    )
