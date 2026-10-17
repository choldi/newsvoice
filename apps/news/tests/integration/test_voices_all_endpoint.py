"""Integration tests for GET /api/admin/voices/all (Phase 9.1)."""

from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from src.api.main import app
from src.api.models.voices import VoiceProfile
from src.core.config import settings

AUTH_HEADERS = {"X-Admin-Token": "test-token"}
client = TestClient(app, headers=AUTH_HEADERS)


@pytest.fixture(autouse=True)
def _voice_sources(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin every catalog source so the response is machine-independent."""
    import src.core.voice_catalog as voice_catalog
    from src.api.services import tts as tts_service
    from src.api.services.voices import VoicesService

    monkeypatch.setattr(settings, "admin_api_token", "test-token")
    monkeypatch.setattr(
        tts_service,
        "_EDGE_VOICES",
        [{"ShortName": "es-ES-DelviraNeural", "Locale": "es-ES", "FriendlyName": "Elvira"}],
    )
    monkeypatch.setattr(
        VoicesService,
        "list_languages",
        AsyncMock(return_value=["en", "es"]),
    )
    monkeypatch.setattr(
        VoicesService,
        "list_stock",
        AsyncMock(return_value=[{"voice_id": "es-mx-male", "language": "es", "name": "Male ES"}]),
    )
    monkeypatch.setattr(
        VoicesService,
        "list_cloned",
        AsyncMock(return_value=[{"voice_id": "uuid-1", "language": "es", "name": "mario"}]),
    )
    monkeypatch.setattr(
        voice_catalog,
        "load_profiles",
        lambda path=None: [
            VoiceProfile.model_validate(
                {
                    "name": "anchor_m",
                    "label": "Anchor",
                    "language": "en",
                    "instruct": "Male, late 40s, warm baritone, broadcast clarity",
                    "ref_text": "Welcome to the daily briefing. " * 6,
                }
            )
        ],
    )
    monkeypatch.setattr(
        voice_catalog,
        "load_custom_profiles",
        lambda path=None: [
            VoiceProfile.model_validate(
                {
                    "name": "custom_es",
                    "label": "Custom ES",
                    "language": "es",
                    "instruct": "Female, young, bright and friendly narrator voice",
                    "ref_text": "Bienvenido al resumen de noticias del día. " * 3,
                }
            )
        ],
    )


def test_voices_all_returns_tagged_options() -> None:
    """Every source appears with its engines and category tags."""
    response = client.get("/api/admin/voices/all")

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "ok"
    by_id = {voice["id"]: voice for voice in payload["voices"]}
    assert by_id["es-ES-DelviraNeural"]["engines"] == ["edge"]
    assert by_id["es-mx-male"]["category"] == "stock"
    assert by_id["uuid-1"]["category"] == "cloned"
    assert by_id["anchor_m"]["category"] == "stock"
    assert by_id["anchor_m"]["engines"] == ["qwen3_tts", "zonos2", "voxcpm2"]
    assert by_id["custom_es"]["category"] == "generated"


def test_voices_all_filters_by_engine() -> None:
    """engine keeps only the voices that engine accepts."""
    response = client.get("/api/admin/voices/all", params={"engine": "omni_voice"})

    assert response.status_code == 200
    ids = [voice["id"] for voice in response.json()["voices"]]
    assert ids == ["es-mx-male", "uuid-1"]


def test_voices_all_filters_by_category() -> None:
    """category keeps only the voices of that origin."""
    response = client.get("/api/admin/voices/all", params={"category": "generated"})

    assert response.status_code == 200
    ids = [voice["id"] for voice in response.json()["voices"]]
    assert ids == ["custom_es"]


def test_voices_all_rejects_an_unknown_engine() -> None:
    """An engine outside TTSBackend is a 422, not an empty list."""
    response = client.get("/api/admin/voices/all", params={"engine": "nope"})

    assert response.status_code == 422
    assert "engine must be one of" in response.json()["detail"]


def test_voices_all_rejects_an_unknown_category() -> None:
    """A category outside stock/cloned/generated is a 422."""
    response = client.get("/api/admin/voices/all", params={"category": "fancy"})

    assert response.status_code == 422
    assert "category must be one of" in response.json()["detail"]


def test_voices_all_still_succeeds_when_the_gateway_is_down() -> None:
    """An unreachable gateway degrades to the other sources, not to a 500."""
    from src.api.services.voices import VoicesService

    with patch.object(
        VoicesService, "list_languages", new=AsyncMock(side_effect=RuntimeError("down"))
    ):
        response = client.get("/api/admin/voices/all")

    assert response.status_code == 200
    ids = {voice["id"] for voice in response.json()["voices"]}
    assert "es-mx-male" not in ids
    assert "es-ES-DelviraNeural" in ids
