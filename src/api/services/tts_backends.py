"""Digest TTS engine backends: edge, the tts-api gateway and Modal (Phase 8.2).

The digest used to be hard-wired to client-side ``edge-tts``. Phase 8 makes the
engine a per-collection choice (``tts_backend`` in ``collections.toml``), so the
dispatch, the per-backend voice resolution and the output format live here, in
one place, instead of spreading through :mod:`src.api.services.tts`.

Three families of backend, three output formats:

===========  ==========================================  ========
backend      call                                         output
===========  ==========================================  ========
edge         ``edge_tts.Communicate`` (local process)     ``.mp3``
omni_voice   ``POST {tts_api_base_url}/tts`` (tts-api)    ``.wav``
qwen3_tts /  ``modal_client.modal_tts()`` / ``_zonos2`` ``.wav``
zonos2 /     / ``_voxcpm2``
voxcpm2
===========  ==========================================  ========

Downstream steps are format-agnostic: ``AudioCompressService`` feeds whatever
ffmpeg can read, ``verify_audio`` decodes with ffmpeg, and the Telegram sender
picks the MIME type from the extension. The only code that cared about "MP3"
was the cache filename and the byte-wise chunk join, both made extension-aware.

The gateway contract (headers, the nested error body it answers with) is
shared with the narration renderer so the two cannot drift apart: the digest
and the podcast talk to the same ``tts-api`` process.

This module must stay importable from activities without pulling in Temporal.
"""

from __future__ import annotations

from typing import Any

import httpx
import structlog

from src.api.models.narration import TTSBackend
from src.core.config import get_settings
from src.core.tracing import trace_function

logger = structlog.get_logger(__name__)

#: Accept the operator-facing spellings of the gateway alongside the canonical
#: enum value: the service was renamed from ``omnivoice-api`` to ``tts-api``, so
#: both names appear in the docs.
BACKEND_ALIASES: dict[str, str] = {
    "edge": TTSBackend.EDGE,
    "edgetts": TTSBackend.EDGE,
    "omni_voice": TTSBackend.OMNIVOICE,
    "omnivoice": TTSBackend.OMNIVOICE,
    "gateway": TTSBackend.OMNIVOICE,
    "tts_api": TTSBackend.OMNIVOICE,
    "qwen3_tts": TTSBackend.QWEN3_TTS,
    "qwen3": TTSBackend.QWEN3_TTS,
    "zonos2": TTSBackend.ZONOS2,
    "voxcpm2": TTSBackend.VOXCPM2,
    "voxcpm": TTSBackend.VOXCPM2,
}

#: Every value a collection may declare for ``tts_backend``.
ALLOWED_BACKENDS: tuple[str, ...] = (
    TTSBackend.EDGE,
    TTSBackend.OMNIVOICE,
    TTSBackend.QWEN3_TTS,
    TTSBackend.ZONOS2,
    TTSBackend.VOXCPM2,
)

#: Edge ShortName -> gateway stock id. Inverse of ``tts.LEGACY_VOICE_ALIASES``,
#: which maps the other way, so a collection keeps its voice when the operator
#: flips the engine instead of silently falling back to a different one.
GATEWAY_VOICE_ALIASES: dict[str, str] = {
    "es-mx-jorgeneural": "es-mx-male",
    "es-mx-dalianeural": "es-mx-female",
    "en-us-arianeural": "en-us-female",
    "en-us-guyneural": "en-us-male",
    "en-gb-sonianeural": "en-gb-female",
    "en-gb-ryanneural": "en-gb-male",
}

#: Language -> gateway stock id used when the collection declares no voice.
#: Only the languages whose Edge default has a known gateway counterpart are
#: listed; anything else must set ``voice`` explicitly rather than receive a
#: stock voice nobody chose.
GATEWAY_VOICE_BY_LANGUAGE: dict[str, str] = {
    "es": "es-mx-male",
    "en": "en-us-male",
}


class TTSBackendError(RuntimeError):
    """A backend cannot serve this request for a reason that will not change.

    Unknown engine, a voice outside the chosen engine's catalogue: retrying the
    activity would fail the same way, so activities raise it as a non-retryable
    ``ApplicationError``.
    """


class TTSGatewayError(RuntimeError):
    """The tts-api gateway rejected a request or could not be reached.

    Attributes:
        status_code: HTTP status returned, or 0 for transport errors.
        error_type: Machine-readable type from the gateway body, e.g.
            'voice_not_found' or 'unsupported_language'.
        valid_voice_ids: Ids the gateway offered back when it did not know the
            requested one, so the message can say what *would* have worked.
    """

    def __init__(
        self,
        message: str,
        status_code: int = 0,
        error_type: str = "",
        valid_voice_ids: tuple[str, ...] = (),
    ) -> None:
        """Initialize the error.

        Args:
            message: Human-readable description.
            status_code: HTTP status, or 0 for transport failures.
            error_type: Gateway error type, when the body carried one.
            valid_voice_ids: Voice ids the gateway suggested.
        """
        super().__init__(message)
        self.status_code = status_code
        self.error_type = error_type
        self.valid_voice_ids = valid_voice_ids


def resolve_backend(value: str | None) -> TTSBackend:
    """Map a configured backend name to the enum.

    Args:
        value: Raw value from ``collections.toml`` / the API / settings.

    Returns:
        The matching backend. Empty falls back to the configured default.

    Raises:
        TTSBackendError: When the name is not one of :data:`ALLOWED_BACKENDS`.
    """
    raw = (value or "").strip().lower()
    if not raw:
        raw = (get_settings().tts_default_backend or "").strip().lower() or "edge"
    canonical = BACKEND_ALIASES.get(raw, raw)
    if canonical not in ALLOWED_BACKENDS:
        raise TTSBackendError(
            f"unknown tts backend {value!r}: expected one of {list(ALLOWED_BACKENDS)}"
        )
    return TTSBackend(canonical)


def ext_for(backend: TTSBackend) -> str:
    """Return the file extension the backend's audio must be stored with.

    Args:
        backend: Engine that produced (or will produce) the audio.

    Returns:
        '.mp3' for edge, '.wav' for the gateway and the Modal backends.
    """
    return ".mp3" if backend is TTSBackend.EDGE else ".wav"


def is_modal(backend: TTSBackend) -> bool:
    """Report whether a backend runs on Modal (and therefore costs money).

    Args:
        backend: Engine to classify.

    Returns:
        True for qwen3_tts, zonos2 and voxcpm2.
    """
    return backend in (TTSBackend.QWEN3_TTS, TTSBackend.ZONOS2, TTSBackend.VOXCPM2)


def wall_seconds_for(backend: TTSBackend, char_count: float) -> float:
    """Estimate the serial wall-clock seconds a synthesis of this size takes.

    Edge and the gateway are network engines measured directly in characters
    per second. Modal is modeled the way the Phase 7.8 budget already models it
    (``estimate_gpu_seconds``): audio seconds from the same throughput figure,
    then scaled by the measured real-time factor, so the time estimate and the
    cost estimate can never disagree about how long a call runs.

    Args:
        backend: Engine that will synthesize.
        char_count: Characters of speech text to synthesize.

    Returns:
        Seconds at concurrency 1; the caller divides by its parallelism.
    """
    if char_count <= 0:
        return 0.0
    settings = get_settings()
    if backend is TTSBackend.EDGE:
        return char_count / settings.tts_edge_chars_per_second
    if backend is TTSBackend.OMNIVOICE:
        return char_count / settings.tts_gateway_chars_per_second
    from src.core.budget import estimate_gpu_seconds

    return estimate_gpu_seconds(int(char_count))


# --- gateway plumbing --------------------------------------------------------


def gateway_headers(api_key: str = "") -> dict[str, str]:
    """Build the request headers the tts-api gateway expects.

    Args:
        api_key: Optional key; an empty value sends no credential.

    Returns:
        Header dict including Accept and, when configured, X-API-Key.
    """
    headers = {"Accept": "audio/wav, application/json"}
    if api_key:
        headers["X-API-Key"] = api_key
    return headers


def parse_gateway_error(response: httpx.Response) -> TTSGatewayError:
    """Translate a non-2xx gateway answer into a typed error.

    The gateway answers with a nested body
    (``{"detail": {"detail": ..., "error_type": ...}}``), so both the nested and
    the flat shape are handled; an unparseable body still yields a useful
    message instead of a bare status code.

    Args:
        response: The non-2xx response.

    Returns:
        A TTSGatewayError carrying status, type, message and any suggested
        voice ids.
    """
    error_type = ""
    message = ""
    valid_voice_ids: tuple[str, ...] = ()
    try:
        body: Any = response.json()
    except ValueError:
        body = None
    if isinstance(body, dict):
        detail = body.get("detail")
        if isinstance(detail, dict):
            error_type = str(detail.get("error_type", ""))
            message = str(detail.get("detail", ""))
            valid = detail.get("valid_voice_ids")
            if isinstance(valid, list):
                valid_voice_ids = tuple(str(item) for item in valid)
        elif isinstance(detail, str):
            message = detail
        if not message:
            message = str(body.get("error", ""))
    if not message:
        message = f"gateway returned {response.status_code}"
    if valid_voice_ids:
        message = f"{message} (valid voice ids: {', '.join(valid_voice_ids[:8])})"
    return TTSGatewayError(
        message,
        status_code=response.status_code,
        error_type=error_type,
        valid_voice_ids=valid_voice_ids,
    )


async def gateway_tts(
    text: str,
    voice_id: str,
    language: str,
    *,
    api_base_url: str | None = None,
    api_key: str | None = None,
    timeout: float | None = None,
    client: httpx.AsyncClient | None = None,
) -> bytes:
    """Synthesize one text through the tts-api gateway.

    Args:
        text: Speech-ready plain text.
        voice_id: Gateway stock id or clone UUID.
        language: ISO 639-1 language code.
        api_base_url: Gateway base URL; defaults to ``settings.tts_api_base_url``.
        api_key: Gateway key; defaults to ``settings.tts_api_key``.
        timeout: Per-request timeout; defaults to ``settings.tts_request_timeout``.
        client: Injected client (tests pass a mock transport).

    Returns:
        The WAV payload returned by the gateway.

    Raises:
        TTSGatewayError: On transport errors, non-2xx answers or an empty body.
    """
    settings = get_settings()
    base = (api_base_url or settings.tts_api_base_url).rstrip("/")
    key = settings.tts_api_key if api_key is None else api_key
    limit = timeout if timeout is not None else float(settings.tts_request_timeout)

    if client is not None:
        return await _post_gateway(client, text, voice_id, language)

    async with httpx.AsyncClient(
        base_url=base, timeout=limit, headers=gateway_headers(key)
    ) as owned:
        return await _post_gateway(owned, text, voice_id, language)


async def _post_gateway(
    client: httpx.AsyncClient, text: str, voice_id: str, language: str
) -> bytes:
    """POST one synthesis request to the gateway's ``tts`` endpoint.

    Args:
        client: Client already bound to the gateway base URL and headers.
        text: Speech-ready plain text.
        voice_id: Gateway stock id or clone UUID.
        language: ISO 639-1 language code.

    Returns:
        The raw WAV payload.

    Raises:
        TTSGatewayError: On transport errors, non-2xx answers or an empty body.
    """
    try:
        response = await client.post(
            "tts",
            json={"text": text, "voice_id": voice_id, "language": language, "speed": 1.0},
        )
    except httpx.HTTPError as exc:
        raise TTSGatewayError(f"gateway request to tts failed: {exc}") from exc
    if response.status_code >= 400:
        raise parse_gateway_error(response)
    if not response.content:
        raise TTSGatewayError("gateway returned an empty body for tts")
    return response.content


# --- voice resolution --------------------------------------------------------


def gateway_voice_for_language(language: str) -> str:
    """Return the default gateway stock id for a language.

    Args:
        language: ISO 639-1 language code.

    Returns:
        The stock id configured for that language.

    Raises:
        TTSBackendError: When no gateway id is known for the language, which is
            a signal to set ``voice`` on the collection rather than to guess.
    """
    try:
        return GATEWAY_VOICE_BY_LANGUAGE[language.strip().lower()]
    except KeyError:
        raise TTSBackendError(
            f"no default tts-api voice for language {language!r}: "
            "set 'voice' on the collection to a stock id from /api/v1/voices/stock"
        ) from None


async def voice_for(backend: TTSBackend, voice: str, language: str) -> str:
    """Resolve a configured voice against the chosen engine's catalogue.

    Args:
        backend: Engine that will synthesize.
        voice: Voice as configured on the collection. May be empty.
        language: ISO 639-1 language driving the per-backend default.

    Returns:
        The engine's own voice identifier.

    Raises:
        TTSBackendError: When the engine has no usable voice for this
            configuration (unmapped language, a Modal ref that is not in the
            catalog, an unreadable catalog).
    """
    requested = (voice or "").strip()
    if backend is TTSBackend.EDGE:
        # Imported lazily: src.api.services.tts imports this module.
        from src.api.services.tts import resolve_edge_voice

        return await resolve_edge_voice(requested, language)

    if backend is TTSBackend.OMNIVOICE:
        if not requested:
            return gateway_voice_for_language(language)
        mapped = GATEWAY_VOICE_ALIASES.get(requested.lower())
        return mapped or requested

    return _modal_voice_for(requested, language)


def _looks_like_foreign_voice(voice: str) -> bool:
    """Report whether a voice id belongs to another engine's vocabulary.

    ``pipeline_config_for`` fills an empty collection voice with the global
    ``TTS_VOICE`` default, which is an Edge ShortName. Switching such a
    collection to Modal must select the catalog's default for its language
    rather than fail on a name that was never meant to reach this engine.

    Args:
        voice: Voice id as configured.

    Returns:
        True for Edge ShortNames and tts-api stock ids.
    """
    lowered = voice.lower()
    return (
        voice.endswith("Neural")
        or lowered in GATEWAY_VOICE_ALIASES
        or lowered in set(GATEWAY_VOICE_ALIASES.values())
    )


def _modal_voice_for(voice: str, language: str) -> str:
    """Resolve a reference voice against the local voice catalog.

    Args:
        voice: Configured ``voice_ref``; empty (or a foreign-engine id) picks
            the language's default profile.
        language: ISO 639-1 language code.

    Returns:
        A profile name that exists in ``voice_profiles.json``.

    Raises:
        TTSBackendError: When the catalog is missing, a deliberately configured
            voice is not in it, or the language has no enabled profile.
    """
    # Imported lazily so a digest that never uses Modal never reads the catalog.
    from src.core.voice_catalog import VoiceCatalogError, load_profiles

    try:
        profiles = load_profiles()
    except VoiceCatalogError as exc:
        raise TTSBackendError(str(exc)) from exc
    enabled = [profile for profile in profiles if profile.enabled]
    if voice and voice not in {profile.name for profile in profiles}:
        if not _looks_like_foreign_voice(voice):
            raise TTSBackendError(
                f"voice {voice!r} is not in the voice catalog: "
                f"expected one of {sorted(p.name for p in profiles)}"
            )
        logger.info(
            "tts_voice_belongs_to_another_engine",
            voice=voice,
            backend=str(TTSBackend.QWEN3_TTS),
            language=language,
        )
        voice = ""
    if not voice:
        for profile in enabled:
            if profile.language == language:
                return profile.name
        raise TTSBackendError(
            f"no enabled voice profile for language {language!r}: "
            f"set 'voice' to one of {sorted(p.name for p in enabled)}"
        )
    return voice


# --- synthesis ---------------------------------------------------------------


@trace_function(component="tts.backends")
async def synthesize(
    backend: TTSBackend,
    text: str,
    voice: str,
    language: str,
    *,
    timeout: float | None = None,
) -> bytes:
    """Synthesize ``text`` with the chosen engine.

    Args:
        backend: Engine to use.
        text: Speech-ready plain text (already truncated and markdown-stripped).
        voice: Engine-specific voice id, already resolved by :func:`voice_for`.
        language: ISO 639-1 language code.
        timeout: Per-request timeout for the HTTP backends.

    Returns:
        Raw audio bytes in the backend's native format (see :func:`ext_for`).

    Raises:
        TTSBackendError: If the engine cannot be used (Modal disabled, catalog
            missing).
        RuntimeError: If synthesis produced no audio.
        TTSGatewayError: If the gateway rejected the request.
    """
    if backend is TTSBackend.EDGE:
        from src.api.services.tts import synthesize_mp3

        return await synthesize_mp3(text, voice, timeout=None if timeout is None else int(timeout))

    if backend is TTSBackend.OMNIVOICE:
        return await gateway_tts(
            text, voice, language, timeout=timeout if timeout is not None else None
        )

    return await _modal_synthesize(backend, text, voice, language)


async def _modal_synthesize(backend: TTSBackend, text: str, voice_ref: str, language: str) -> bytes:
    """Synthesize through Modal, enforcing the Phase 7.8 guards first.

    Args:
        backend: qwen3_tts, zonos2 or voxcpm2.
        text: Speech-ready plain text.
        voice_ref: Reference voice name from the catalog.
        language: ISO 639-1 language code.

    Returns:
        The WAV payload returned by Modal.

    Raises:
        TTSBackendError: When Modal is off, zonos2/voxcpm2 is gated,
            credentials are missing or the call fails.
    """
    settings = get_settings()
    if not settings.modal_enabled:
        raise TTSBackendError(f"tts backend {backend} requires modal, but modal is disabled")
    if backend is TTSBackend.ZONOS2 and not settings.zonos2_enabled:
        raise TTSBackendError("tts backend zonos2 is behind the benchmark gate and not enabled")
    if backend is TTSBackend.VOXCPM2 and not settings.voxcpm2_enabled:
        raise TTSBackendError("tts backend voxcpm2 is behind the benchmark gate and not enabled")
    if not settings.modal_token_id or not settings.modal_token_secret:
        raise TTSBackendError("modal credentials are not configured")

    from src.core.modal_client import modal_tts, modal_voxcpm2_tts, modal_zonos2_tts

    try:
        if backend is TTSBackend.ZONOS2:
            return await modal_zonos2_tts(text=text, voice_ref=voice_ref, language=language)
        if backend is TTSBackend.VOXCPM2:
            return await modal_voxcpm2_tts(text=text, voice_ref=voice_ref, language=language)
        return await modal_tts(text=text, voice_ref=voice_ref, language=language)
    except Exception as exc:  # noqa: BLE001 - re-raised as a typed backend error
        raise TTSBackendError(f"modal tts call failed: {exc}") from exc
