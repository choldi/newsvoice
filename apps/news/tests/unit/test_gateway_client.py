"""Unit tests for the shared tts-api gateway dispatcher (:mod:`...services.gateway`).

The dispatcher is the single place that owns the request headers, the lazy
httpx client and the "non-2xx body -> typed error" translation for the digest
and the narration renderer, so its contract is asserted here once instead of
twice behind two service facades.
"""

import httpx
import pytest

from src.api.services.gateway import (
    DEFAULT_ACCEPT,
    GatewayClient,
    GatewayError,
    gateway_headers,
)
from src.core.config import get_settings

# --- headers ---------------------------------------------------------------


def test_headers_ask_for_audio_and_send_no_credential_without_a_key() -> None:
    """The synthesis endpoints answer with WAV, and no key means no credential."""
    assert gateway_headers() == {"Accept": DEFAULT_ACCEPT}
    assert "X-API-Key" not in gateway_headers("")


def test_headers_carry_the_key_when_configured() -> None:
    """A configured key travels as X-API-Key."""
    assert gateway_headers("secret")["X-API-Key"] == "secret"


def test_json_only_callers_can_narrow_the_accept_header() -> None:
    """The voice admin GUI reads JSON alone without duplicating the key rule."""
    assert gateway_headers("secret", accept="application/json") == {
        "Accept": "application/json",
        "X-API-Key": "secret",
    }


# --- construction ----------------------------------------------------------


def test_settings_supply_the_defaults_when_nothing_is_passed() -> None:
    """A dispatcher built from settings alone points at the configured gateway."""
    settings = get_settings()
    dispatcher = GatewayClient()

    assert dispatcher.base_url == settings.tts_api_base_url.rstrip("/")
    assert dispatcher.api_key == settings.tts_api_key
    assert dispatcher.timeout == float(settings.tts_request_timeout)
    assert dispatcher.headers == gateway_headers(settings.tts_api_key)


async def test_an_injected_client_is_never_closed_by_the_dispatcher() -> None:
    """Ownership stays with whoever built the client (the service or a test)."""
    injected = httpx.AsyncClient(base_url="http://gateway.test/api/v1")
    dispatcher = GatewayClient(client=injected)

    await dispatcher.aclose()

    assert injected.is_closed is False
    await injected.aclose()


# --- transport -------------------------------------------------------------


def _dispatcher(handler: object, **kwargs: object) -> tuple[GatewayClient, httpx.AsyncClient]:
    """Build a dispatcher over a mock transport, for assertion on the wire."""
    injected = httpx.AsyncClient(
        base_url="http://gateway.test/api/v1", transport=httpx.MockTransport(handler)
    )
    return GatewayClient(client=injected, **kwargs), injected


async def test_aclose_closes_the_client_the_dispatcher_built() -> None:
    """With nobody injecting a client, the dispatcher owns it and closes it."""
    dispatcher = GatewayClient(base_url="http://gateway.test/api/v1")
    built = await dispatcher._http()

    await dispatcher.aclose()

    assert built.is_closed is True


async def test_post_audio_sends_the_headers_and_returns_the_payload() -> None:
    """One POST carries the shared headers and hands back the WAV bytes."""
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["accept"] = request.headers.get("Accept", "")
        seen["key"] = request.headers.get("X-API-Key", "")
        return httpx.Response(200, content=b"RIFF")

    dispatcher, injected = _dispatcher(handler, api_key="secret")
    audio = await dispatcher.post_audio("tts", {"text": "hola"})
    await injected.aclose()

    assert audio == b"RIFF"
    assert seen["url"].endswith("/api/v1/tts")
    assert seen["accept"] == DEFAULT_ACCEPT
    assert seen["key"] == "secret"


async def test_post_audio_translates_a_non_2xx_body_into_a_gateway_error() -> None:
    """The gateway's nested error body becomes the shared error, not a status."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"detail": {"detail": "gone"}})

    dispatcher, injected = _dispatcher(handler)
    with pytest.raises(GatewayError) as excinfo:
        await dispatcher.post_audio("tts", {"text": "hola"})
    await injected.aclose()

    assert excinfo.value.status_code == 404
    assert str(excinfo.value) == "gone"


async def test_post_audio_rejects_an_empty_body() -> None:
    """A 200 with no bytes is a failed synthesis, not a playable clip."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"")

    dispatcher, injected = _dispatcher(handler)
    with pytest.raises(GatewayError, match="empty body"):
        await dispatcher.post_audio("tts", {"text": "hola"})
    await injected.aclose()


async def test_post_audio_reports_a_transport_failure_with_status_zero() -> None:
    """A request that never got an answer is distinguishable from a gateway 4xx."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    dispatcher, injected = _dispatcher(handler)
    with pytest.raises(GatewayError, match="refused") as excinfo:
        await dispatcher.post_audio("tts", {"text": "hola"})
    await injected.aclose()

    assert excinfo.value.status_code == 0


async def test_get_returns_a_non_2xx_response_to_the_caller() -> None:
    """The health probe degrades on a 503, so GET never raises on status alone."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"status": "degraded"})

    dispatcher, injected = _dispatcher(handler)
    response = await dispatcher.get("health")
    await injected.aclose()

    assert response.status_code == 503
    assert response.json() == {"status": "degraded"}


async def test_get_raises_a_transport_failure() -> None:
    """An unreachable gateway surfaces as the dispatcher's error type."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadError("boom", request=request)

    dispatcher, injected = _dispatcher(handler)
    with pytest.raises(GatewayError, match="boom"):
        await dispatcher.get("health")
    await injected.aclose()


async def test_the_configured_error_subclass_is_what_propagates() -> None:
    """Each service keeps catching its own type: the dispatcher raises that one."""

    class _ServiceError(GatewayError):
        """Error type a service registers with its dispatcher."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"detail": {"detail": "nope"}})

    dispatcher, injected = _dispatcher(handler, error_cls=_ServiceError)
    with pytest.raises(_ServiceError, match="nope"):
        await dispatcher.post_audio("tts", {"text": "hola"})
    await injected.aclose()
