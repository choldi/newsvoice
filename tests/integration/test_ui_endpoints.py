"""Integration tests for the admin GUI pages (/ui, Jinja2 + HTMX)."""

from collections.abc import Iterator
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from src.api.main import app
from src.core.config import settings

TOKEN = "secret-admin-token"


@pytest.fixture(autouse=True)
def _ui_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point collections file and auth config at isolated values."""
    monkeypatch.setattr(settings, "collections_file", str(tmp_path / "collections.toml"))
    monkeypatch.setattr(settings, "admin_api_token", TOKEN)
    return tmp_path / "collections.toml"


OMNI_KEYWORDS = "omni_languages"
_SELECT_MARKER = '<select name="language"'
_TTS_FRAGMENT_URL = "/ui/fragments/tts_options"


def test_tts_options_fragment_renders_selects_when_enabled() -> None:
    """The TTS fragment renders selects populated from OmniVoice on demand."""
    client = _ui_client()
    _login(client)
    with (
        patch(
            "src.api.routes_ui.VoicesService.list_languages",
            new_callable=AsyncMock,
            return_value=["en", "es"],
        ),
        patch(
            "src.api.routes_ui.VoicesService.list_stock",
            new_callable=AsyncMock,
            return_value=[
                {"voice_id": "es-mx-male", "language": "es"},
                {"voice_id": "en-us-female", "language": "en"},
            ],
        ),
        patch(
            "src.api.routes_ui.VoicesService.list_cloned",
            new_callable=AsyncMock,
            return_value=[],
        ),
    ):
        fragment = client.get(f"{_TTS_FRAGMENT_URL}?tts_enabled=on&language=es&voice=es-mx-male")

    assert fragment.status_code == 200
    assert '<select name="language"' in fragment.text
    assert '<option value="es"' in fragment.text
    assert '<option value="en"' in fragment.text
    assert 'value="es-mx-male"' in fragment.text
    assert "selected" in fragment.text


def test_tts_options_fragment_renders_text_inputs_when_disabled() -> None:
    """Without TTS the fragment falls back to free-text language/voice inputs."""
    client = _ui_client()
    _login(client)

    fragment = client.get(f"{_TTS_FRAGMENT_URL}?tts_enabled=&language=es&voice=es-mx-male")

    assert fragment.status_code == 200
    assert '<select name="language"' not in fragment.text
    assert '<input type="text" name="language"' in fragment.text
    assert 'value="es"' in fragment.text
    assert 'value="es-mx-male"' in fragment.text


def test_collections_page_shows_omni_selects_for_tts_collection() -> None:
    """A collection with TTS enabled renders language/voice as OmniVoice selects."""
    client = _ui_client()
    _login(client)
    client.post(
        "/ui/collections",
        data={
            "name": "cast",
            "tts_enabled": "on",
            "language": "es",
            "voice": "es-mx-male",
            "feed_urls": "https://a/rss",
        },
    )
    with (
        patch(
            "src.api.routes_ui.VoicesService.list_languages",
            new_callable=AsyncMock,
            return_value=["en", "es"],
        ),
        patch(
            "src.api.routes_ui.VoicesService.list_stock",
            new_callable=AsyncMock,
            return_value=[{"voice_id": "es-mx-male", "language": "es"}],
        ),
        patch(
            "src.api.routes_ui.VoicesService.list_cloned",
            new_callable=AsyncMock,
            return_value=[],
        ),
    ):
        page = client.get("/ui/collections")

    assert page.status_code == 200
    assert '<select name="language"' in page.text


def test_collections_page_keeps_text_inputs_without_tts() -> None:
    """A collection without TTS keeps free-text language/voice inputs."""
    client = _ui_client()
    _login(client)
    client.post(
        "/ui/collections",
        data={"name": "texto", "language": "es", "voice": "voz1", "feed_urls": "https://a/rss"},
    )

    page = client.get("/ui/collections")

    assert page.status_code == 200
    assert _SELECT_MARKER not in page.text
    assert '<input type="text" name="language"' in page.text
    assert 'value="es"' in page.text
    assert 'value="voz1"' in page.text


@pytest.fixture(autouse=True)
def _no_omni_network() -> Iterator[None]:
    """Keep the GUI tests offline: stub the OmniVoice service methods."""
    with (
        patch(
            "src.api.routes_ui.VoicesService.list_languages",
            new_callable=AsyncMock,
            return_value=[],
        ),
        patch(
            "src.api.routes_ui.VoicesService.list_stock",
            new_callable=AsyncMock,
            return_value=[],
        ),
        patch(
            "src.api.routes_ui.VoicesService.list_cloned",
            new_callable=AsyncMock,
            return_value=[],
        ),
    ):
        yield


def _ui_client() -> TestClient:
    """Build a client that auto-follows redirects and stores cookies."""
    return TestClient(app)


def _login(client: TestClient, token: str = TOKEN) -> None:
    """Authenticate a client via the login endpoint."""
    response = client.post("/ui/login", data={"token": token}, follow_redirects=False)
    assert response.status_code == 302


def test_unauthenticated_ui_redirects_to_login() -> None:
    """/ui/* without a cookie redirects to the login page."""
    client = _ui_client()
    response = client.get("/ui/collections", follow_redirects=False)
    assert response.status_code == 302
    assert response.headers["location"] == "/ui/login"


def test_login_wrong_token_rejected() -> None:
    """POST /ui/login with a bad token redirects back to login with an error."""
    client = _ui_client()
    response = client.post("/ui/login", data={"token": "wrong"}, follow_redirects=False)
    assert response.status_code == 302
    assert response.headers["location"].startswith("/ui/login")
    assert "level=error" in response.headers["location"]


def test_login_sets_cookie_and_grants_access() -> None:
    """After login, /ui/collections renders the collections page."""
    client = _ui_client()
    _login(client)

    response = client.get("/ui/collections")
    assert response.status_code == 200
    assert "Colecciones" in response.text
    assert settings.admin_api_token not in response.text


def test_collections_page_lists_created_collection() -> None:
    """A collection created via the GUI form shows in the collections page."""
    client = _ui_client()
    _login(client)

    created = client.post(
        "/ui/collections",
        data={
            "name": "science",
            "time": "09:30",
            "feed_urls": "https://example.com/rss\nhttps://example2.com/rss",
            "tts_enabled": "on",
            "podcast_name": "CienciaCast",
            "email_recipients": "a@example.com\nb@example.com",
        },
        follow_redirects=False,
    )
    assert created.status_code == 302

    page = client.get("/ui/collections")
    assert "science" in page.text
    assert "CienciaCast" in page.text
    assert "09:30 UTC" in page.text


def test_email_recipients_round_trip_via_gui() -> None:
    """Creating and updating a collection preserves email recipients."""
    client = _ui_client()
    _login(client)
    client.post(
        "/ui/collections",
        data={
            "name": "tech",
            "feed_urls": "https://a/rss",
            "email_recipients": "a@example.com, b@example.com",
        },
    )

    created = client.get("/ui/collections")
    assert "a@example.com" in created.text
    assert "b@example.com" in created.text

    updated = client.post(
        "/ui/collections/tech/update",
        data={
            "new_name": "tech",
            "time": "08:00",
            "feed_urls": "https://a/rss",
            "email_recipients": "only@example.com\n a@example.com",
        },
        follow_redirects=False,
    )
    assert updated.status_code == 302

    page = client.get("/ui/collections")
    assert "only@example.com" in page.text
    assert "b@example.com" not in page.text


def test_custom_prompt_round_trip_via_gui() -> None:
    """A saved custom_prompt overrides the template and persists in the form."""
    client = _ui_client()
    _login(client)
    custom = "You are a senior tech editor. Emphasize startups and venture deals."
    client.post(
        "/ui/collections",
        data={
            "name": "tech",
            "feed_urls": "https://a/rss",
            "prompt_template": "finance",
            "custom_prompt": custom,
        },
    )

    page = client.get("/ui/collections")
    assert custom in page.text
    assert f'<textarea name="custom_prompt" rows="4">{custom}</textarea>' in page.text


def test_collections_page_duplicate_conflict() -> None:
    """Creating a duplicate collection flashes an error."""
    client = _ui_client()
    _login(client)
    payload = {"name": "tech", "feed_urls": "https://example.com/rss"}

    client.post("/ui/collections", data=payload)
    second = client.post("/ui/collections", data=payload, follow_redirects=False)

    assert second.status_code == 302
    location = second.headers["location"]
    assert "level=error" in location


def test_update_collection_via_gui() -> None:
    """Updating a collection via the GUI modifies its fields."""
    client = _ui_client()
    _login(client)
    client.post("/ui/collections", data={"name": "tech", "feed_urls": "https://a/rss"})

    response = client.post(
        "/ui/collections/tech/update",
        data={"new_name": "tech2", "time": "11:00", "feed_urls": "https://b/rss"},
        follow_redirects=False,
    )
    assert response.status_code == 302

    page = client.get("/ui/collections")
    assert "tech2" in page.text
    assert "tech-editar" not in page.text.replace("tech2", "")


def test_delete_collection_via_gui() -> None:
    """Deleting a collection via the GUI removes it."""
    client = _ui_client()
    _login(client)
    client.post("/ui/collections", data={"name": "tech", "feed_urls": "https://a/rss"})

    response = client.post("/ui/collections/tech/delete", follow_redirects=False)
    assert response.status_code == 302

    page = client.get("/ui/collections")
    assert ">tech</" not in page.text


def test_calendar_page_renders() -> None:
    """The calendar page renders the weekly grid."""
    client = _ui_client()
    _login(client)
    client.post(
        "/ui/collections", data={"name": "tech", "time": "10:00", "feed_urls": "https://a/rss"}
    )

    page = client.get("/ui/calendar")
    assert page.status_code == 200
    assert "Calendario semanal" in page.text
    assert "tech" in page.text


def test_history_and_errors_pages_render() -> None:
    """History and errors pages load and their fragments refresh."""
    client = _ui_client()
    _login(client)

    history = client.get("/ui/history")
    assert history.status_code == 200
    errors = client.get("/ui/errors")
    assert errors.status_code == 200

    fragment = client.get("/ui/fragments/history")
    assert fragment.status_code == 200
    assert "Sin ejecuciones registradas" in fragment.text


def test_voices_page_renders_with_mock_service() -> None:
    """Voices page renders stock and cloned lists from the voices service."""
    client = _ui_client()
    _login(client)
    with (
        patch(
            "src.api.routes_ui.VoicesService.list_stock",
            new_callable=AsyncMock,
            return_value=[{"voice_id": "es-mx-male", "language": "es"}],
        ),
        patch(
            "src.api.routes_ui.VoicesService.list_cloned",
            new_callable=AsyncMock,
            return_value=[{"voice_id": "mario", "language": "es"}],
        ),
    ):
        page = client.get("/ui/voices")

    assert page.status_code == 200
    assert "es-mx-male" in page.text
    assert "mario" in page.text


def test_run_now_button_triggers_pipeline() -> None:
    """POST /ui/collections/{name}/run starts the pipeline and redirects."""
    client = _ui_client()
    _login(client)
    client.post("/ui/collections", data={"name": "tech", "feed_urls": "https://a/rss"})

    with patch(
        "src.api.routes_ui.TemporalAdminService.trigger_collection",
        new_callable=AsyncMock,
        return_value="digest-pipeline-tech-20260101000000",
    ):
        response = client.post("/ui/collections/tech/run", follow_redirects=False)

    assert response.status_code == 302
    assert "workflow" in response.headers["location"] or "history" in response.headers["location"]


def test_resync_button() -> None:
    """POST /ui/schedules/resync reconciles schedules."""
    client = _ui_client()
    _login(client)
    with patch(
        "src.api.routes_ui.TemporalAdminService.resync_schedules",
        new_callable=AsyncMock,
        return_value={"created_or_updated": ["daily-digest-tech"], "removed": []},
    ):
        response = client.post("/ui/schedules/resync", follow_redirects=False)

    assert response.status_code == 302
    location = response.headers["location"].lower().replace("%20", " ").replace("+", " ")
    assert "resincronizados" in location


def test_logout_clears_cookie() -> None:
    """POST /ui/logout removes the admin cookie."""
    client = _ui_client()
    _login(client)
    assert client.get("/ui/collections").status_code == 200

    client.post("/ui/logout", follow_redirects=False)

    assert client.get("/ui/collections", follow_redirects=False).status_code == 302
