"""HTTP client for the voice catalog the gateway owns (P6).

Since P6 the gateway owns ``voice_profiles.json``, the generated manifest, the
reference WAVs and the operator-created profiles; news-engine keeps no catalog
file of its own. This module is the only place that talks to
``/voices/catalog``:

- :func:`fetch_catalog` refreshes an in-process snapshot of
  :class:`~tts_contract.VoiceCatalogResponse` when it is older than
  ``max_age``, so the GUI, the dropdowns and the Temporal activities share one
  parse of a payload they all need;
- :func:`fetch_catalog_blocking` serves that same snapshot to the synchronous
  readers (:func:`src.core.voice_catalog.load_profiles` and friends). It talks
  to the gateway only while the snapshot is still empty — once per process —
  so a warm reader never waits on it;
- :func:`fetch_reference_audio`, :func:`store_reference` and
  :func:`save_custom_profile` move the WAVs and the custom profiles, which
  used to be local files under ``settings.voices_dir``.

Every failure reaches the caller as :class:`VoiceCatalogRequestError`, a
subclass of the :class:`VoiceCatalogError` the readers already catch, so a
gateway outage degrades exactly like a missing catalog file did before P6.
"""

from __future__ import annotations

import time
from typing import Any

import httpx
from tts_contract import (
    VoiceCatalogError,
    VoiceCatalogResponse,
    VoiceManifestEntry,
    VoiceProfile,
    parse_gateway_error,
)

from src.api.services.gateway import GatewayClient, GatewayError, gateway_headers
from src.core.config import get_settings

__all__ = [
    "CATALOG_PATH",
    "DEFAULT_MAX_AGE",
    "VoiceCatalogRequestError",
    "cached_catalog",
    "fetch_catalog",
    "fetch_catalog_blocking",
    "fetch_reference_audio",
    "invalidate_catalog",
    "prime_catalog",
    "save_custom_profile",
    "store_reference",
]

#: Gateway path of the catalog, relative to ``settings.tts_api_base_url``.
CATALOG_PATH = "voices/catalog"

#: How long a snapshot is served without asking the gateway again. Long enough
#: that a burst of dropdown renders costs one request, short enough that a
#: voice created in one window shows up in the next.
DEFAULT_MAX_AGE = 15.0

#: Budget for the one blocking read a cold synchronous reader performs: it may
#: run on an event loop that must not stall on an unreachable gateway.
COLD_TIMEOUT_SECONDS = 5.0


class VoiceCatalogRequestError(VoiceCatalogError):
    """A catalog operation failed over HTTP: refused, or never answered.

    Attributes:
        retryable: False when the same request cannot succeed later (4xx the
            gateway meant, or an answer that broke the contract), True for
            transport failures, timeouts, 429 and 5xx. Temporal turns it into
            ``non_retryable`` on the activities that write the catalog.
    """

    def __init__(self, message: str, *, retryable: bool = True) -> None:
        """Initialize the error.

        Args:
            message: Human-readable description, normally the gateway's own.
            retryable: Whether a retry could succeed.
        """
        super().__init__(message)
        self.retryable = retryable


_snapshot: VoiceCatalogResponse | None = None
_snapshot_at: float = 0.0


def prime_catalog(payload: VoiceCatalogResponse) -> None:
    """Install a catalog payload as the current snapshot.

    Used at startup and by tests, which have no gateway to ask.

    Args:
        payload: Catalog payload to remember.
    """
    global _snapshot, _snapshot_at
    _snapshot = payload
    _snapshot_at = time.monotonic()


def invalidate_catalog() -> None:
    """Drop the snapshot so the next read talks to the gateway again."""
    global _snapshot, _snapshot_at
    _snapshot = None
    _snapshot_at = 0.0


def cached_catalog() -> VoiceCatalogResponse | None:
    """Return the current snapshot, when one has been loaded.

    Returns:
        The snapshot, or None when this process never read the catalog.
    """
    return _snapshot


async def fetch_catalog(*, max_age: float = DEFAULT_MAX_AGE) -> VoiceCatalogResponse:
    """Return the catalog, refreshing the snapshot when it is older than max_age.

    Args:
        max_age: Age in seconds after which the gateway is asked again; 0
            forces a fresh read (the admin GUI poll uses it).

    Returns:
        The catalog payload.

    Raises:
        VoiceCatalogError: When the gateway refused the catalog, keeping its
            own message (``duplicate voice profile names: [...]`` still reads
            well on the admin page).
        VoiceCatalogRequestError: When the gateway cannot be reached or
            answers something that is not a catalog payload.
    """
    if _fresh(max_age):
        assert _snapshot is not None  # noqa: S101 - guarded by _fresh
        return _snapshot
    return _install(await _request_catalog())


def fetch_catalog_blocking() -> VoiceCatalogResponse:
    """Return the catalog to a synchronous reader, priming it when cold.

    A warm snapshot is returned as-is: the synchronous readers of ``src.core``
    never block on the gateway, they see the data as fresh as the last async
    reader made it.

    Returns:
        The catalog payload.

    Raises:
        VoiceCatalogError: When the cold read fails. Callers that tolerate a
            missing catalog already catch this type.
    """
    if _snapshot is None:
        return _install(_request_catalog_blocking())
    return _snapshot


def _dispatcher() -> GatewayClient:
    """Return the gateway dispatcher used for one catalog call.

    Tests inject an httpx transport here instead of standing up a gateway.

    Returns:
        A dispatcher bound to ``settings.tts_api_base_url``.
    """
    return GatewayClient()


def _blocking_http() -> httpx.Client:
    """Return the client for the one read a cold synchronous reader performs.

    Tests inject an httpx transport here too, so no test ever opens a socket.

    Returns:
        A short-budget client bound to ``settings.tts_api_base_url``.
    """
    settings = get_settings()
    return httpx.Client(
        base_url=settings.tts_api_base_url.rstrip("/"),
        timeout=COLD_TIMEOUT_SECONDS,
        headers=gateway_headers(settings.tts_api_key),
    )


async def fetch_reference_audio(name: str) -> bytes | None:
    """Return one profile's reference WAV, or None when it does not exist.

    Args:
        name: Profile name, optionally with a ``.wav`` suffix.

    Returns:
        The WAV bytes, or None when the name is unsafe, unknown or has no
        reference stored.

    Raises:
        VoiceCatalogRequestError: When the gateway cannot be reached or
            answers anything but the audio.
    """
    path = f"{CATALOG_PATH}/refs/{_stem(name)}"
    try:
        async with _dispatcher() as dispatcher:
            response = await dispatcher.get(path)
    except GatewayError as exc:
        raise _unreachable(exc) from exc
    if response.status_code == httpx.codes.NOT_FOUND:
        return None
    if response.status_code >= 400:
        raise _refused(response.status_code, _message(response))
    return response.content


async def store_reference(
    name: str,
    wav: bytes,
    *,
    instruct: str,
    ref_text: str,
    design_model: str = "",
) -> VoiceManifestEntry:
    """Upload a designed reference and return the manifest entry it became.

    The gateway measures and digests the audio itself, so the entry it returns
    describes the bytes it stored rather than the ones news-engine reported.

    Args:
        name: Profile name (slug, without the ``.wav`` suffix).
        wav: Raw WAV bytes of the reference.
        instruct: Profile instruct at generation time, for the drift guard.
        ref_text: Profile transcript at generation time, for the drift guard.
        design_model: Model that produced the reference.

    Returns:
        The stored manifest entry.

    Raises:
        VoiceCatalogError: When the profile is unknown (404), the audio is
            refused (422) or the name is taken (409); the gateway's message is
            kept.
        VoiceCatalogRequestError: When the gateway cannot be reached.
    """
    path = f"{CATALOG_PATH}/refs/{_stem(name)}"
    try:
        async with _dispatcher() as dispatcher:
            response = await dispatcher.post_form(
                path,
                files={"file": (f"{_stem(name)}.wav", wav, "audio/wav")},
                data={
                    "instruct": instruct,
                    "ref_text": ref_text,
                    "design_model": design_model,
                },
            )
    except GatewayError as exc:
        raise _failed(exc) from exc
    invalidate_catalog()
    return _decode(response, VoiceManifestEntry)


async def save_custom_profile(profile: VoiceProfile) -> VoiceProfile:
    """Create an operator-authored profile in the gateway's custom catalog.

    Args:
        profile: Validated profile to append.

    Returns:
        The saved profile.

    Raises:
        VoiceCatalogError: When the name is already taken (409).
        VoiceCatalogRequestError: When the gateway cannot be reached.
    """
    try:
        async with _dispatcher() as dispatcher:
            response = await dispatcher.post_json(
                f"{CATALOG_PATH}/custom", profile.model_dump(mode="json")
            )
    except GatewayError as exc:
        raise _failed(exc) from exc
    invalidate_catalog()
    return _decode(response, VoiceProfile)


# --- internals ---------------------------------------------------------------


def _fresh(max_age: float) -> bool:
    """Report whether the snapshot is recent enough to serve as-is.

    Args:
        max_age: Maximum snapshot age in seconds.

    Returns:
        True when a snapshot exists and is younger than ``max_age``.
    """
    return _snapshot is not None and time.monotonic() - _snapshot_at <= max_age


def _install(response: httpx.Response) -> VoiceCatalogResponse:
    """Validate a catalog answer and make it the current snapshot.

    Args:
        response: The catalog response.

    Returns:
        The parsed payload.

    Raises:
        VoiceCatalogError: When the gateway answered with an error.
        VoiceCatalogRequestError: When the body is not a catalog payload.
    """
    if response.status_code >= 400:
        raise _refused(response.status_code, _message(response))
    payload = _decode(response, VoiceCatalogResponse)
    prime_catalog(payload)
    return payload


def _decode(response: httpx.Response, model: type[Any]) -> Any:
    """Validate a 2xx JSON answer as one of the shared catalog models.

    Args:
        response: The response to decode.
        model: Pydantic model to validate the body against.

    Returns:
        The validated model instance.

    Raises:
        VoiceCatalogRequestError: When the body is not the promised JSON.
    """
    try:
        body: Any = response.json()
    except ValueError as exc:
        raise VoiceCatalogRequestError(
            f"gateway answered without a JSON body for {response.request.url.path}: {exc}",
            retryable=False,
        ) from exc
    try:
        return model.model_validate(body)
    except ValueError as exc:
        raise VoiceCatalogRequestError(
            f"gateway answered with an unusable voice catalog: {exc}",
            retryable=False,
        ) from exc


def _stem(name: str) -> str:
    """Strip the optional ``.wav`` suffix the audio routes append.

    Args:
        name: Profile name, with or without the suffix.

    Returns:
        The bare profile name.
    """
    return name[:-4] if name.lower().endswith(".wav") else name


async def _request_catalog() -> httpx.Response:
    """GET the catalog payload.

    Returns:
        The response, whatever its status.

    Raises:
        VoiceCatalogRequestError: When the request never got an answer.
    """
    try:
        async with _dispatcher() as dispatcher:
            return await dispatcher.get(CATALOG_PATH)
    except GatewayError as exc:
        raise _unreachable(exc) from exc


def _request_catalog_blocking() -> httpx.Response:
    """GET the catalog payload from a synchronous reader.

    Returns:
        The response, whatever its status.

    Raises:
        VoiceCatalogRequestError: When the request never got an answer.
    """
    try:
        with _blocking_http() as client:
            return client.get(CATALOG_PATH)
    except httpx.HTTPError as exc:
        raise VoiceCatalogRequestError(
            f"gateway request to {CATALOG_PATH} failed: {exc}", retryable=True
        ) from exc


def _message(response: httpx.Response) -> str:
    """Return the message a gateway error body carries.

    Args:
        response: A non-2xx response.

    Returns:
        The ``detail`` the gateway reported, or a status-based fallback.
    """
    try:
        body: Any = response.json()
    except ValueError:
        body = None
    return parse_gateway_error(body, status_code=response.status_code).message


def _refused(status_code: int, message: str) -> VoiceCatalogRequestError:
    """Build the error for an answer the gateway meant as a refusal.

    Args:
        status_code: HTTP status of the answer.
        message: The gateway's own message.

    Returns:
        The error, retryable only when the status could still change later.
    """
    return VoiceCatalogRequestError(
        message, retryable=status_code in {408, 425, 429} or status_code >= 500
    )


def _failed(exc: GatewayError) -> VoiceCatalogRequestError:
    """Build the error for a POST the dispatcher already unwrapped.

    Args:
        exc: The dispatcher error, carrying the gateway's status and message.

    Returns:
        The error, marked retryable only when a retry could succeed.
    """
    if not exc.status_code:
        return VoiceCatalogRequestError(str(exc), retryable=True)
    return _refused(exc.status_code, str(exc))


def _unreachable(exc: GatewayError) -> VoiceCatalogRequestError:
    """Build the error for a request that never got an answer.

    Args:
        exc: The dispatcher error.

    Returns:
        The error, always retryable.
    """
    return VoiceCatalogRequestError(str(exc), retryable=True)
