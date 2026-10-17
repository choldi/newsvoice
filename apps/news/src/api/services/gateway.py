"""Single HTTP dispatcher for the tts-api gateway.

The digest dispatcher (:mod:`src.api.services.tts_backends`), the narration
renderer (:mod:`src.api.services.narration_tts`) and the voice admin GUI
(:mod:`src.api.services.voices`) all talk to the same ``tts-api`` process.
Each used to hand-roll the request headers, the base-URL/timeout resolution and
the "turn a non-2xx body into a typed error" dance, so a change to the gateway's
protocol had to be repeated in every copy.

Headers, the lazy client, the error unwrapping (via
:func:`tts_contract.parse_gateway_error`) and the shared error base live here
once. What is genuinely per-caller stays per-caller:

- their base URL and API key (they are different settings: the narration
  renderer reads ``narration_*``, everything else reads ``tts_api_*``);
- their concurrency policy (the narration renderer wraps every call in a
  semaphore sized by ``narration_max_concurrency``);
- their exception type (Temporal treats ``NarrationGatewayError`` as
  non-retryable, so the narration renderer must keep raising that type).

Two gateway callers deliberately stay outside this module:

- the voice admin GUI keeps its own httpx usage because it answers to a
  different contract: ``Accept: application/json``, multipart uploads and
  httpx-native ``HTTPStatusError``/``HTTPError``, which its cache fallback
  catches as ``httpx.HTTPError``. It shares :func:`gateway_headers` only;
- the monitoring disk probe carries no credential, uses a hard 10 s budget and
  reports failure as ``skipped`` instead of raising.
"""

from __future__ import annotations

from typing import Any

import httpx
from tts_contract import parse_gateway_error as parse_gateway_error_body

from src.core.config import get_settings

__all__ = ["DEFAULT_ACCEPT", "GatewayClient", "GatewayError", "gateway_headers"]

#: What the synthesis endpoints answer with: WAV bytes, JSON when they refuse.
DEFAULT_ACCEPT = "audio/wav, application/json"


class GatewayError(RuntimeError):
    """A tts-api request failed: transport error, non-2xx answer or empty body.

    Services subclass it so a caller can catch its own type (``TTSGatewayError``
    on the digest path, ``NarrationGatewayError`` on the narration path) or the
    common base when it only cares that "the gateway said no".

    Attributes:
        status_code: HTTP status returned, or 0 for transport errors.
        error_type: Machine-readable type from the gateway body, e.g.
            'voice_not_found' or 'unsupported_language', or '' when the body
            carried none.
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


def gateway_headers(api_key: str = "", *, accept: str = DEFAULT_ACCEPT) -> dict[str, str]:
    """Build the request headers the tts-api gateway expects.

    Args:
        api_key: Optional key; an empty value sends no credential.
        accept: Accept header value; JSON-only callers pass ``application/json``.

    Returns:
        Header dict including Accept and, when configured, X-API-Key.
    """
    headers = {"Accept": accept}
    if api_key:
        headers["X-API-Key"] = api_key
    return headers


class GatewayClient:
    """HTTP transport for one tts-api base URL.

    The underlying ``httpx.AsyncClient`` is built on the first request and
    reused afterwards, so a service can hold one client for its whole life
    without paying a connection per call. :meth:`aclose` closes it only when
    this instance built it: an injected test client stays under the caller's
    control.

    Args:
        base_url: Gateway base URL. Defaults to ``settings.tts_api_base_url``.
        api_key: Key sent as X-API-Key; ``None`` reads ``settings.tts_api_key``.
        timeout: Per-request timeout in seconds; ``None`` reads
            ``settings.tts_request_timeout``.
        client: Injected HTTP client (tests pass a mock transport). It is not
            closed by :meth:`aclose` and its own base URL and headers win.
        error_cls: Type raised on transport errors, non-2xx answers and empty
            bodies; must accept ``(message, status_code, error_type,
            valid_voice_ids)``.
    """

    def __init__(
        self,
        base_url: str | None = None,
        *,
        api_key: str | None = None,
        timeout: float | None = None,
        client: httpx.AsyncClient | None = None,
        error_cls: type[GatewayError] = GatewayError,
    ) -> None:
        """Initialize the dispatcher (see the class docstring for the arguments)."""
        settings = get_settings()
        self.base_url = (base_url or settings.tts_api_base_url).rstrip("/")
        self.api_key = settings.tts_api_key if api_key is None else api_key
        self.timeout = float(settings.tts_request_timeout if timeout is None else timeout)
        self.error_cls = error_cls
        self._client = client
        self._owns_client = client is None

    @property
    def headers(self) -> dict[str, str]:
        """Headers sent with every request of this dispatcher."""
        return gateway_headers(self.api_key)

    async def _http(self) -> httpx.AsyncClient:
        """Return the client to send with, building it on first use.

        Returns:
            The httpx client, bound to this dispatcher's base URL, headers and
            timeout when this instance owns it.
        """
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=self.base_url, timeout=self.timeout, headers=self.headers
            )
        return self._client

    def _error(self, response: httpx.Response) -> GatewayError:
        """Translate a non-2xx answer into the configured error type.

        Args:
            response: The non-2xx response.

        Returns:
            An instance of ``error_cls`` carrying status, type, message and any
            suggested voice ids, with the message shaped by
            :func:`tts_contract.parse_gateway_error`.
        """
        try:
            body: Any = response.json()
        except ValueError:
            body = None
        info = parse_gateway_error_body(body, status_code=response.status_code)
        return self.error_cls(
            info.message,
            status_code=response.status_code,
            error_type=info.error_type,
            valid_voice_ids=tuple(info.valid_voice_ids),
        )

    async def get(self, path: str) -> httpx.Response:
        """GET a gateway endpoint, returning the response whatever its status.

        Status handling is left to the caller: the narration health probe
        degrades on a 503 instead of raising, while the voice listing treats
        any non-2xx as an error.

        Args:
            path: Path relative to the dispatcher's base URL (e.g. ``health``).

        Returns:
            The response, including non-2xx ones.

        Raises:
            GatewayError: When the request never got an answer.
        """
        client = await self._http()
        try:
            return await client.get(path, headers=self.headers)
        except httpx.HTTPError as exc:
            raise self.error_cls(f"gateway request to {path} failed: {exc}") from exc

    async def post_audio(self, path: str, payload: dict[str, Any]) -> bytes:
        """POST a JSON body to a gateway endpoint that answers with WAV bytes.

        Args:
            path: Path relative to the dispatcher's base URL (e.g. ``tts``).
            payload: JSON body.

        Returns:
            The WAV payload returned by the gateway.

        Raises:
            GatewayError: On transport errors, non-2xx answers or an empty body.
        """
        client = await self._http()
        try:
            response = await client.post(path, json=payload, headers=self.headers)
        except httpx.HTTPError as exc:
            raise self.error_cls(f"gateway request to {path} failed: {exc}") from exc
        if response.status_code >= 400:
            raise self._error(response)
        if not response.content:
            raise self.error_cls(f"gateway returned an empty body for {path}")
        return response.content

    async def aclose(self) -> None:
        """Close the underlying client when this instance built it."""
        if self._client is not None and self._owns_client:
            await self._client.aclose()
            self._client = None

    async def __aenter__(self) -> GatewayClient:
        """Enter the async context (no connection is opened yet)."""
        return self

    async def __aexit__(self, *_exc: object) -> None:
        """Leave the async context, closing an owned client."""
        await self.aclose()
