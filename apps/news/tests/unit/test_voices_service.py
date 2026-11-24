"""Unit tests for the OmniVoice voices service."""

import asyncio
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from src.api.services.voices import VoicesService, reset_voice_catalog_cache

STOCK = [
    {
        "voice_id": "es-mx-male",
        "language": "es",
        "gender": "male",
        "name": "Spanish MX Male",
        "engine": "",
    },
    {
        "voice_id": "en-us-female",
        "language": "en",
        "gender": "female",
        "name": "English US Female",
        "engine": "",
    },
]


@pytest.mark.asyncio
async def test_list_stock_returns_and_caches() -> None:
    """Stock voices are fetched once and served from the local cache."""
    service = VoicesService(api_base_url="http://tts.test/api/v1")

    with patch.object(service, "_get", new_callable=AsyncMock, return_value=STOCK) as mock_get:
        first = await service.list_stock(force=True)
        cached = await service.list_stock(force=True)
        assert first == STOCK
        assert cached == STOCK
        assert mock_get.call_count == 2  # force bypasses cache each time


@pytest.mark.asyncio
async def test_list_stock_shares_cache_between_service_instances() -> None:
    """Concurrent instances dedupe one gateway request through the shared cache."""
    first = VoicesService(api_base_url="http://tts.test/api/v1")
    second = VoicesService(api_base_url="http://tts.test/api/v1")

    with (
        patch.object(first, "_get", new_callable=AsyncMock, return_value=STOCK) as first_get,
        patch.object(second, "_get", new_callable=AsyncMock, return_value=STOCK) as second_get,
    ):
        results = await asyncio.gather(first.list_stock(), second.list_stock())

    assert results == [STOCK, STOCK]
    assert first_get.await_count + second_get.await_count == 1


@pytest.mark.asyncio
async def test_catalog_cache_reset_forces_a_new_request() -> None:
    """Resetting the shared cache makes the next read hit the gateway again."""
    service = VoicesService(api_base_url="http://tts.test/api/v1")
    with patch.object(service, "_get", new_callable=AsyncMock, return_value=STOCK) as mock_get:
        await service.list_stock()
        reset_voice_catalog_cache()
        await service.list_stock()

    assert mock_get.await_count == 2


@pytest.mark.asyncio
async def test_list_stock_filters_by_language() -> None:
    """language filter narrows the stock list."""
    service = VoicesService(api_base_url="http://tts.test/api/v1")
    with patch.object(service, "_get", new_callable=AsyncMock, return_value=STOCK):
        es = await service.list_stock(language="es", force=True)
        assert len(es) == 1
        assert es[0]["voice_id"] == "es-mx-male"


@pytest.mark.asyncio
async def test_list_stock_handles_failure() -> None:
    """A failing fetch falls back to the previous cache instead of raising."""
    service = VoicesService(api_base_url="http://tts.test/api/v1")
    with patch.object(service, "_get", new_callable=AsyncMock, return_value=STOCK):
        await service.list_stock(force=True)
    with patch.object(
        service,
        "_get",
        new_callable=AsyncMock,
        side_effect=httpx.ConnectError("unreachable"),
    ):
        fallback = await service.list_stock(force=True)
        assert fallback == STOCK


@pytest.mark.asyncio
async def test_list_stock_with_engine_uses_query_params() -> None:
    """An engine-specific read asks the gateway for that engine's catalogue."""
    service = VoicesService(api_base_url="http://tts.test/api/v1")

    with patch.object(service, "_get", new_callable=AsyncMock, return_value=STOCK) as mock_get:
        voices = await service.list_stock(language="es", engine="kokoro")

    assert voices == STOCK
    mock_get.assert_awaited_once_with("voices/stock", params={"engine": "kokoro", "language": "es"})


@pytest.mark.asyncio
async def test_list_cloned_dedupes_concurrent_reads_without_persistent_cache() -> None:
    """Clones are fetched once per burst but never survive past the request."""
    first = VoicesService(api_base_url="http://tts.test/api/v1")
    second = VoicesService(api_base_url="http://tts.test/api/v1")
    clones = [{"voice_id": "clone-1", "name": "Clone", "language": "es"}]

    with (
        patch.object(first, "_get", new_callable=AsyncMock, return_value=clones) as first_get,
        patch.object(second, "_get", new_callable=AsyncMock, return_value=clones) as second_get,
    ):
        await asyncio.gather(first.list_cloned(), second.list_cloned())

    assert first_get.await_count + second_get.await_count == 1


@pytest.mark.asyncio
async def test_clone_voice_posts_form() -> None:
    """clone_voice POSTs the reference audio and returns the API JSON."""
    service = VoicesService(api_base_url="http://tts.test/api/v1")
    expected = {
        "voice_id": "uuid-v4",
        "name": "mi-voz",
        "language": "es",
        "engines": "auto",
        "ref_text": None,
        "message": "",
    }

    class FakeResponse:
        def json(self):
            return expected

        def raise_for_status(self) -> None:
            return

    with patch("src.api.services.voices.httpx.AsyncClient") as mock_client_cls:
        mock_client = mock_client_cls.return_value
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.post = AsyncMock(return_value=FakeResponse())

        result = await service.clone_voice("mi-voz", "es", b"\x52\x49\x46\x46", "ref.wav")

        assert result == expected
        mock_client.post.assert_called_once()
        kwargs = mock_client.post.call_args.kwargs
        assert kwargs["data"] == {"name": "mi-voz", "language": "es", "engines": "auto"}
        assert "reference_audio" in kwargs["files"]


@pytest.mark.asyncio
async def test_clone_voice_includes_ref_text_when_provided() -> None:
    """clone_voice includes ref_text in the form data when provided."""
    service = VoicesService(api_base_url="http://tts.test/api/v1")
    expected = {
        "voice_id": "uuid-v4",
        "name": "mi-voz",
        "language": "es",
        "engines": "auto",
        "ref_text": None,
        "message": "",
    }

    class FakeResponse:
        def json(self):
            return expected

        def raise_for_status(self) -> None:
            return

    with patch("src.api.services.voices.httpx.AsyncClient") as mock_client_cls:
        mock_client = mock_client_cls.return_value
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.post = AsyncMock(return_value=FakeResponse())

        result = await service.clone_voice(
            "mi-voz", "es", b"\x52\x49\x46\x46", "ref.wav", ref_text="Hola mundo"
        )

        assert result == expected
        mock_client.post.assert_called_once()
        kwargs = mock_client.post.call_args.kwargs
        assert kwargs["data"] == {
            "name": "mi-voz",
            "language": "es",
            "engines": "auto",
            "ref_text": "Hola mundo",
        }
        assert "reference_audio" in kwargs["files"]
