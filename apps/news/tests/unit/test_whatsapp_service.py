"""Unit tests for the WhatsAppService (Baileys gateway delivery).

Covers fail-closed behavior (unconfigured / non-allowlisted), JID conversion,
human delay + per-recipient cool-down pacing, typing indicator, and audio
(base64) sends.
"""

import base64
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from src.api.services.whatsapp import (
    WhatsAppConfigError,
    WhatsAppDeliveryError,
    WhatsAppNotAllowedError,
    WhatsAppService,
)

wa_config = SimpleNamespace(
    whatsapp_api_url="http://there:8085",
    whatsapp_api_token="secret-token",
    whatsapp_allowlist=["+34600000001", "+34600000002"],
    whatsapp_delay_mode="off",
    whatsapp_delay_custom_min_ms=2000,
    whatsapp_delay_custom_max_ms=8000,
    whatsapp_typing_indicator="composing",
    whatsapp_recipient_cooldown_ms=0,
)


class FakeResponse:
    def __init__(self, ok: bool = True) -> None:
        self.is_success = ok
        self.status_code = 200 if ok else 500
        self.text = "{}" if ok else "boom"


class FakeAsyncClient:
    def __init__(self, response: FakeResponse | None = None) -> None:
        self.posts: list[dict] = []
        self.response = response or FakeResponse()

    async def __aenter__(self) -> "FakeAsyncClient":
        return self

    async def __aexit__(self, *args: object) -> None:
        return None

    async def post(self, *args: object, **kwargs: object) -> FakeResponse:
        self.posts.append({"args": args, "kwargs": kwargs})
        return self.response


def _service(config: object = wa_config) -> WhatsAppService:
    return WhatsAppService(whatsapp_settings=config)  # type: ignore[arg-type]


def _message_posts(client: FakeAsyncClient) -> list[dict]:
    """Return only the /message posts (excludes /simulate typing calls)."""
    return [p for p in client.posts if p["args"][0].endswith("/message")]


def _simulate_posts(client: FakeAsyncClient) -> list[dict]:
    """Return the /simulate typing-indicator posts."""
    return [p for p in client.posts if p["args"][0].endswith("/simulate")]


async def test_send_text_fail_closed_when_unconfigured() -> None:
    """Without an API token the channel raises a config error (fail-closed)."""
    service = _service(SimpleNamespace(**{**wa_config.__dict__, "whatsapp_api_token": ""}))
    with pytest.raises(WhatsAppConfigError):
        await service.send_text("Hola", ["+34600000001"])


async def test_send_text_rejects_non_allowlisted_recipient() -> None:
    """Fail-closed: a number outside WHATSAPP_ALLOWLIST raises and sends nothing."""
    service = _service()
    client = FakeAsyncClient()
    with patch("src.api.services.whatsapp.httpx.AsyncClient", return_value=client):
        with pytest.raises(WhatsAppNotAllowedError):
            await service.send_text("Hola", ["+34699999999"])
    assert client.posts == []


async def test_send_text_allows_allowlisted_numbers_and_converts_jid() -> None:
    """Allowlisted E.164 numbers are sent as <digits>@c.us on the /message API."""
    service = _service()
    client = FakeAsyncClient()
    with patch("src.api.services.whatsapp.httpx.AsyncClient", return_value=client):
        result = await service.send_text("Hola", ["+34600000001"])
    assert result["status"] == "ok"
    assert result["recipients"] == 1

    sent = _message_posts(client)[0]["kwargs"]
    assert sent["json"]["chatId"] == "34600000001@c.us"
    assert sent["json"]["text"] == "Hola"
    assert sent["headers"]["Authorization"] == "Bearer secret-token"
    assert _message_posts(client)[0]["args"][0] == "http://there:8085/message"
    simulate_actions = [p["kwargs"]["json"]["action"] for p in _simulate_posts(client)]
    assert simulate_actions == ["composing", "paused"]


async def test_send_text_group_exempt_from_allowlist() -> None:
    """A group (whatsapp_group_id) needs no allowlist and becomes <id>@g.us."""
    service = _service()
    client = FakeAsyncClient()
    with patch("src.api.services.whatsapp.httpx.AsyncClient", return_value=client):
        result = await service.send_text("Hola", [], group_id="1234567890")
    assert result["recipients"] == 1
    assert _message_posts(client)[0]["kwargs"]["json"]["chatId"] == "1234567890@g.us"


async def test_send_text_deduplicates_recipients() -> None:
    """Duplicate receivers collapse into a single target before sending."""
    service = _service()
    client = FakeAsyncClient()
    with patch("src.api.services.whatsapp.httpx.AsyncClient", return_value=client):
        await service.send_text("Hola", ["+34600000001", "+34600000001"])
    assert len(_message_posts(client)) == 1


async def test_send_text_custom_delay_uses_configured_interval() -> None:
    """Custom delay mode sleeps a random value inside [min, max]."""
    config = SimpleNamespace(
        **{
            **wa_config.__dict__,
            "whatsapp_delay_mode": "custom",
            "whatsapp_typing_indicator": "none",
        }
    )
    service = _service(config)
    client = FakeAsyncClient()
    with (
        patch("src.api.services.whatsapp.httpx.AsyncClient", return_value=client),
        patch("src.api.services.whatsapp.asyncio.sleep", new_callable=AsyncMock) as mock_sleep,
    ):
        await service.send_text("Hola", ["+34600000001"])

    sleep_args = [call.args[0] for call in mock_sleep.await_args_list]
    assert sleep_args and all(2.0 <= duration <= 8.0 for duration in sleep_args)
    assert _simulate_posts(client) == []


async def test_send_text_delay_off_skips_sleep() -> None:
    """delay mode 'off' performs no pacing sleep."""
    service = _service()
    client = FakeAsyncClient()
    with (
        patch("src.api.services.whatsapp.httpx.AsyncClient", return_value=client),
        patch("src.api.services.whatsapp.asyncio.sleep", new_callable=AsyncMock) as mock_sleep,
    ):
        await service.send_text("Hola", ["+34600000001"])
    assert mock_sleep.await_count == 0
    assert len(_message_posts(client)) == 1


async def test_send_text_long_message_paces_per_recipient() -> None:
    """Long digests are chunked and each send against one JID is paced."""
    config = SimpleNamespace(
        **{
            **wa_config.__dict__,
            "whatsapp_delay_mode": "off",
            "whatsapp_recipient_cooldown_ms": 300,
        }
    )
    service = _service(config)
    client = FakeAsyncClient()
    long_message = "## Story\n\n" + ("Some paragraph text for the digest.\n\n" * 300)
    with (
        patch("src.api.services.whatsapp.httpx.AsyncClient", return_value=client),
        patch("src.api.services.whatsapp.asyncio.sleep", new_callable=AsyncMock) as mock_sleep,
    ):
        result = await service.send_text(long_message, ["+34600000001"])
    assert mock_sleep.await_count > 0
    assert len(_message_posts(client)) > 1
    assert result["recipients"] == 1


async def test_send_audio_posts_base64_and_caption(tmp_path: Path) -> None:
    """send_audio base64-encodes the file and strips emotion tags from caption."""
    audio = tmp_path / "digest.m4a"
    audio.write_bytes(b"\x00fake-m4a\xff")
    service = _service()
    client = FakeAsyncClient()
    with patch("src.api.services.whatsapp.httpx.AsyncClient", return_value=client):
        result = await service.send_audio(str(audio), ["+34600000001"], caption="[happy] Top")
    assert result["status"] == "ok"
    assert result["recipients"] == 1

    payload = _message_posts(client)[0]["kwargs"]["json"]
    media = payload["media"]
    assert base64.b64decode(media["data"]) == b"\x00fake-m4a\xff"
    assert media["caption"] == "Top"
    assert media["ptt"] is False
    assert media["type"] == "audio"
    assert payload["chatId"] == "34600000001@c.us"


async def test_send_audio_missing_file_raises() -> None:
    """A missing audio file raises FileNotFoundError before any request."""
    service = _service()
    client = FakeAsyncClient()
    with patch("src.api.services.whatsapp.httpx.AsyncClient", return_value=client):
        with pytest.raises(FileNotFoundError):
            await service.send_audio("/tmp/nope.m4a", ["+34600000001"])
    assert client.posts == []


async def test_send_audio_oversized_raises(tmp_path: Path) -> None:
    """Audio above the size gate is rejected (base64 payload safety)."""
    audio = tmp_path / "big.m4a"
    audio.write_bytes(b"x" * 20)
    service = _service()
    with patch("src.api.services.whatsapp.WHATSAPP_AUDIO_MAX_BYTES", 8):
        with pytest.raises(ValueError):
            await service.send_audio(str(audio), ["+34600000001"])


async def test_request_raises_delivery_error_on_http_failure() -> None:
    """A non-2xx gateway response raises WhatsAppDeliveryError."""
    service = _service()
    client = FakeAsyncClient(response=FakeResponse(ok=False))
    with patch("src.api.services.whatsapp.httpx.AsyncClient", return_value=client):
        with pytest.raises(WhatsAppDeliveryError):
            await service.send_text("Hola", ["+34600000001"])
