"""Integration tests for the admin GUI pages (/ui, Jinja2 + HTMX)."""

from collections.abc import Iterator
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from src.api.main import app
from src.api.routes_ui import STATUS_LABELS
from src.api.services.collections_repo import CollectionsRepository
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
    """The TTS fragment renders selects populated from the chosen engine."""
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
        fragment = client.get(
            f"{_TTS_FRAGMENT_URL}"
            "?tts_enabled=on&tts_backend=omni_voice&language=es&voice=es-mx-male"
        )

    assert fragment.status_code == 200
    assert '<select name="tts_backend"' in fragment.text
    assert '<select name="language"' in fragment.text
    assert '<option value="es"' in fragment.text
    assert '<option value="en"' in fragment.text
    assert 'value="es-mx-male"' in fragment.text
    assert "selected" in fragment.text


def test_tts_options_fragment_follows_the_selected_engine() -> None:
    """Each engine brings its own voice vocabulary, so the list must follow it."""
    client = _ui_client()
    _login(client)
    with (
        patch(
            "src.api.routes_ui.VoicesService.list_languages",
            new_callable=AsyncMock,
            return_value=["es"],
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
        gateway = client.get(f"{_TTS_FRAGMENT_URL}?tts_enabled=on&tts_backend=omni_voice")

    # edge is stubbed offline by the conftest fixture, so it must NOT offer the
    # gateway's stock ids: a wrong id would only fail at 08:00 during a run.
    edge = client.get(f"{_TTS_FRAGMENT_URL}?tts_enabled=on&tts_backend=edge")

    assert gateway.status_code == edge.status_code == 200
    assert '<option value="es-mx-male"' in gateway.text
    assert '<select name="voice"' in gateway.text
    assert '<option value="es-mx-male"' not in edge.text
    assert '<input type="text" name="voice"' in edge.text


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
    """A gateway collection renders language/voice from the gateway catalogue."""
    client = _ui_client()
    _login(client)
    client.post(
        "/ui/collections",
        data={
            "name": "cast",
            "tts_enabled": "on",
            "tts_backend": "omni_voice",
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


def test_delivery_channels_round_trip_via_gui() -> None:
    """Creating with delivery_channels and updating preserves the selection."""
    client = _ui_client()
    _login(client)
    client.post(
        "/ui/collections",
        data={
            "name": "tech",
            "feed_urls": "https://a/rss",
            "email_recipients": "a@example.com",
            "delivery_channels": ["telegram", "email"],
        },
    )

    page = client.get("/ui/collections")
    telegram_checkbox = 'name="delivery_channels" value="telegram" checked'
    email_checkbox = 'name="delivery_channels" value="email" checked'
    # Both the "nueva" form (always telegram) and the tech edit form render it.
    assert page.text.count(telegram_checkbox) == 2
    assert page.text.count(email_checkbox) == 1

    updated = client.post(
        "/ui/collections/tech/update",
        data={
            "new_name": "tech",
            "time": "08:00",
            "feed_urls": "https://a/rss",
            "email_recipients": "a@example.com",
            "delivery_channels": "email",
        },
        follow_redirects=False,
    )
    assert updated.status_code == 302

    page = client.get("/ui/collections")
    # tech dropped telegram; only the "nueva" form still renders it checked.
    assert page.text.count(telegram_checkbox) == 1
    assert page.text.count(email_checkbox) == 1


def test_whatsapp_round_trip_via_gui() -> None:
    """Creating and updating a collection preserves its WhatsApp destinations."""
    client = _ui_client()
    _login(client)
    client.post(
        "/ui/collections",
        data={
            "name": "tech",
            "feed_urls": "https://a/rss",
            "delivery_channels": ["telegram", "whatsapp"],
            "whatsapp_recipients": "+34600111222, +34600333444",
            "whatsapp_group_id": "1234567890",
        },
    )

    page = client.get("/ui/collections")
    assert "+34600111222" in page.text
    assert "+34600333444" in page.text
    assert 'name="whatsapp_group_id" value="1234567890"' in page.text
    assert 'name="delivery_channels" value="whatsapp" checked' in page.text

    updated = client.post(
        "/ui/collections/tech/update",
        data={
            "new_name": "tech",
            "time": "08:00",
            "feed_urls": "https://a/rss",
            "delivery_channels": ["telegram", "whatsapp"],
            "whatsapp_recipients": "+34600555555",
            "whatsapp_group_id": "9876543210",
        },
        follow_redirects=False,
    )
    assert updated.status_code == 302

    page = client.get("/ui/collections")
    assert "+34600555555" in page.text
    assert 'name="whatsapp_group_id" value="9876543210"' in page.text
    assert "+34600111222" not in page.text


def test_logic_delivery_round_trip_via_gui() -> None:
    """The logic delivery block survives a GUI create and a GUI update."""
    client = _ui_client()
    _login(client)
    client.post(
        "/ui/collections",
        data={
            "name": "tech",
            "feed_urls": "https://a/rss",
            "logic_channels": ["telegram", "whatsapp"],
            "logic_chat_id": "-100123456",
            "logic_email_recipients": "logic@example.com",
            "logic_whatsapp_recipients": "+34600777777",
            "logic_whatsapp_group_id": "555000111",
        },
    )

    page = client.get("/ui/collections")
    assert 'name="logic_chat_id" value="-100123456"' in page.text
    assert "logic@example.com" in page.text
    assert "+34600777777" in page.text
    assert 'name="logic_whatsapp_group_id" value="555000111"' in page.text
    assert 'name="logic_channels" value="whatsapp" checked' in page.text

    updated = client.post(
        "/ui/collections/tech/update",
        data={
            "new_name": "tech",
            "time": "08:00",
            "feed_urls": "https://a/rss",
            "logic_channels": ["telegram", "whatsapp"],
            "logic_chat_id": "-100999888",
            "logic_whatsapp_recipients": "+34600888888",
        },
        follow_redirects=False,
    )
    assert updated.status_code == 302

    page = client.get("/ui/collections")
    assert 'name="logic_chat_id" value="-100999888"' in page.text
    assert "+34600888888" in page.text
    assert "+34600777777" not in page.text


def test_logic_delivery_preserved_when_form_omits_the_block() -> None:
    """An update that does not send the logic block keeps the stored config."""
    client = _ui_client()
    _login(client)
    client.post(
        "/ui/collections",
        data={
            "name": "tech",
            "feed_urls": "https://a/rss",
            "logic_channels": ["telegram"],
            "logic_chat_id": "-100123456",
        },
    )

    updated = client.post(
        "/ui/collections/tech/update",
        data={
            "new_name": "tech",
            "time": "09:00",
            "feed_urls": "https://a/rss",
        },
        follow_redirects=False,
    )
    assert updated.status_code == 302

    page = client.get("/ui/collections")
    assert 'name="logic_chat_id" value="-100123456"' in page.text
    assert 'name="logic_channels" value="telegram" checked' in page.text


def test_logic_delivery_cleared_when_block_is_emptied() -> None:
    """Sending the logic block with every field empty disables the destination."""
    client = _ui_client()
    _login(client)
    client.post(
        "/ui/collections",
        data={
            "name": "tech",
            "feed_urls": "https://a/rss",
            "logic_channels": ["telegram"],
            "logic_chat_id": "-100123456",
        },
    )

    updated = client.post(
        "/ui/collections/tech/update",
        data={
            "new_name": "tech",
            "time": "08:00",
            "feed_urls": "https://a/rss",
            "logic_chat_id": "",
            "logic_email_recipients": "",
            "logic_whatsapp_recipients": "",
            "logic_whatsapp_group_id": "",
        },
        follow_redirects=False,
    )
    assert updated.status_code == 302

    page = client.get("/ui/collections")
    assert "-100123456" not in page.text
    assert page.text.count('name="logic_channels" value="telegram" checked') == 0


def test_delivery_channels_default_to_telegram_when_unset() -> None:
    """With no delivery_channels checkboxes, creation defaults to telegram."""
    client = _ui_client()
    _login(client)
    client.post(
        "/ui/collections",
        data={"name": "tech", "feed_urls": "https://a/rss"},
    )

    page = client.get("/ui/collections")
    telegram_checkbox = 'name="delivery_channels" value="telegram" checked'
    email_checkbox = 'name="delivery_channels" value="email" checked'
    assert page.text.count(telegram_checkbox) == 2
    assert page.text.count(email_checkbox) == 0


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


def test_runs_page_renders() -> None:
    """Runs page loads and its fragment renders the executions table."""
    client = _ui_client()
    _login(client)

    page = client.get("/ui/runs")
    assert page.status_code == 200
    assert "Ejecuciones" in page.text

    with patch(
        "src.api.routes_ui.TemporalAdminService.list_executions",
        new_callable=AsyncMock,
        return_value=[],
    ):
        fragment = client.get("/ui/fragments/runs")

    assert fragment.status_code == 200
    assert "Sin ejecuciones registradas" in fragment.text


def test_runs_fragment_shows_running_execution_with_stop_button() -> None:
    """A running execution shows the running badge and a Detener button."""
    from datetime import UTC, datetime

    client = _ui_client()
    _login(client)
    now = datetime(2026, 9, 15, 8, 0, tzinfo=UTC)
    with patch(
        "src.api.routes_ui.TemporalAdminService.list_executions",
        new_callable=AsyncMock,
        return_value=[
            {
                "workflow_id": "digest-pipeline-tech-20260915080000",
                "status": "running",
                "start_time": now,
                "close_time": None,
            }
        ],
    ):
        fragment = client.get("/ui/fragments/runs")

    assert fragment.status_code == 200
    assert "en ejecución" in fragment.text
    assert "Detener" in fragment.text


def test_terminate_run_endpoint() -> None:
    """POST /ui/runs/{id}/terminate terminates and redirects with a flash."""
    client = _ui_client()
    _login(client)
    with patch(
        "src.api.routes_ui.TemporalAdminService.terminate_workflow",
        new_callable=AsyncMock,
    ):
        response = client.post(
            "/ui/runs/digest-pipeline-tech-20260915080000/terminate",
            follow_redirects=False,
        )

    assert response.status_code == 302
    assert "detenida" in response.headers["location"].lower()


def test_voices_page_renders_with_mock_service() -> None:
    """The unified table shows the stock and cloned rows of the service."""
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
        # The table paginates, so the rows are asserted through the filter.
        stock = client.get("/ui/fragments/voices_table", params={"q": "es-mx-male"})
        cloned = client.get("/ui/fragments/voices_table", params={"category": "cloned"})

    assert page.status_code == 200
    assert 'id="voices-table"' in page.text
    assert stock.status_code == 200
    assert "es-mx-male" in stock.text
    assert cloned.status_code == 200
    assert "mario" in cloned.text


def test_run_now_button_triggers_pipeline() -> None:
    """POST /ui/collections/{name}/run starts the pipeline and redirects."""
    client = _ui_client()
    _login(client)
    client.post("/ui/collections", data={"name": "tech", "feed_urls": "https://a/rss"})

    with (
        patch(
            "src.api.routes_ui.TemporalAdminService.is_collection_running",
            new_callable=AsyncMock,
            return_value=False,
        ),
        patch(
            "src.api.routes_ui.TemporalAdminService.trigger_collection",
            new_callable=AsyncMock,
            return_value="digest-pipeline-tech-20260101000000",
        ),
    ):
        response = client.post("/ui/collections/tech/run", follow_redirects=False)

    assert response.status_code == 302
    assert "workflow%20lanzado" in response.headers["location"].lower()


def test_run_now_skips_when_already_running() -> None:
    """POST run while already running does not start a new workflow."""
    client = _ui_client()
    _login(client)
    client.post("/ui/collections", data={"name": "tech", "feed_urls": "https://a/rss"})

    with (
        patch(
            "src.api.routes_ui.TemporalAdminService.is_collection_running",
            new_callable=AsyncMock,
            return_value=True,
        ),
        patch(
            "src.api.routes_ui.TemporalAdminService.trigger_collection",
            new_callable=AsyncMock,
        ) as trigger,
    ):
        response = client.post("/ui/collections/tech/run", follow_redirects=False)

    assert response.status_code == 302
    trigger.assert_not_awaited()
    assert "ejecuci%c3%b3n%20en%20curso" in response.headers["location"].lower()


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


def test_run_detail_shows_the_verification_ledger() -> None:
    """Every verification verdict (7.13/7.14) is auditable from the run detail."""
    import asyncio

    from src.api.db.database import VerificationsRepository

    async def _seed() -> int:
        from src.api.db.database import RunsRepository

        run_id = await RunsRepository().record_run(
            workflow_id="digest-pipeline-tech-verify",
            collection="tech",
            status="verification_failed",
            article_count=5,
            cluster_count=2,
            digest_preview="# Digest",
            error="Digest verification failed",
        )
        repo = VerificationsRepository()
        await repo.record(
            run_id="digest-pipeline-tech-verify",
            collection="tech",
            target="digest",
            method="faithfulness",
            verdict="invalid",
            score=0.5,
            threshold=0.9,
            passed=False,
            details={"reason": "claim contradicted"},
        )
        await repo.record(
            run_id="digest-pipeline-tech-verify",
            collection="tech",
            target="tts_audio",
            method="asr_wer",
            verdict="invalid",
            score=0.9,
            threshold=0.3,
            passed=False,
            details={"wer": 0.9, "reason": "transcription diverges"},
            model="base",
        )
        return run_id

    run_id = asyncio.run(_seed())
    client = _ui_client()
    _login(client)
    response = client.get(
        f"/ui/fragments/run_detail?workflow_id=digest-pipeline-tech-verify&run_id={run_id}"
    )

    assert response.status_code == 200
    body = response.text
    assert "Verificaciones" in body
    assert "faithfulness" in body
    assert "asr_wer" in body
    assert "0.500" in body
    assert "0.900" in body
    assert "WER ↓" in body
    assert "claim contradicted" in body


def test_verification_failed_status_is_labelled_and_styled() -> None:
    """The GUI both labels and colour-codes a run blocked by verification."""
    assert STATUS_LABELS["verification_failed"] == "verificación fallida"
    base = (Path("src/api/templates") / "base.html").read_text(encoding="utf-8")
    assert ".badge.verification_failed" in base


def test_collections_gui_round_trips_the_tts_backend() -> None:
    """The engine chosen in the form survives create, edit and the TOML file."""
    client = _ui_client()
    _login(client)
    client.post(
        "/ui/collections",
        data={
            "name": "motor",
            "tts_enabled": "on",
            "tts_backend": "omni_voice",
            "feed_urls": "https://a/rss",
        },
    )

    page = client.get("/ui/collections")
    assert '<select name="tts_backend"' in page.text
    assert '<option value="omni_voice" selected>' in page.text

    client.post(
        "/ui/collections/motor/update",
        data={
            "new_name": "motor",
            "tts_enabled": "on",
            "tts_backend": "qwen3_tts",
            "feed_urls": "https://a/rss",
        },
    )

    repo = CollectionsRepository()
    assert repo.get("motor").tts_backend == "qwen3_tts"


def _emit_log(event: str, component: str = "ui.test") -> None:
    """Write one structured log record into the in-memory ring buffer."""
    import structlog

    from src.core import logging as logging_ext

    logging_ext.configure_logging("api")
    structlog.get_logger("ui-test").info(event, component=component)


def test_logs_page_renders() -> None:
    """The observability page loads with the logs panel container."""
    client = _ui_client()
    _login(client)
    page = client.get("/ui/logs")
    assert page.status_code == 200
    assert "Observabilidad" in page.text
    assert 'id="logs-panel"' in page.text


def test_logs_fragment_renders_records() -> None:
    """The logs fragment renders rows for records in the ring buffer."""
    from src.core import logging as logging_ext

    logging_ext.clear_ring_buffer()
    _emit_log("ui-fragment-needle")

    client = _ui_client()
    _login(client)
    fragment = client.get("/ui/fragments/logs", params={"component": "ui.test"})
    assert fragment.status_code == 200
    assert "ui-fragment-needle" in fragment.text


def test_log_levels_fragment_and_update() -> None:
    """The level controls render and POST persists a new component level."""
    from src.core import logging as logging_ext

    client = _ui_client()
    _login(client)

    levels = client.get("/ui/fragments/log_levels")
    assert levels.status_code == 200
    assert 'name="component"' in levels.text

    update = client.post(
        "/ui/fragments/log_level",
        data={"component": "ui.test", "level": "DEBUG"},
    )
    assert update.status_code == 200
    assert logging_ext.get_component_levels().get("ui.test") == "DEBUG"


def test_traces_fragment_and_detail() -> None:
    """The traces fragment lists a span and the detail endpoint renders it."""
    from src.core import tracing

    tracing.clear_spans()
    tracer = tracing.get_tracer("ui.trace")
    with tracer.start_as_current_span("ui-root-span") as span:
        trace_id = format(span.get_span_context().trace_id, "032x")

    client = _ui_client()
    _login(client)

    listing = client.get("/ui/fragments/traces")
    assert listing.status_code == 200
    assert trace_id in listing.text

    detail = client.get("/ui/fragments/trace_detail", params={"trace_id": trace_id})
    assert detail.status_code == 200
    assert "ui-root-span" in detail.text


def test_export_fragment_reports_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Exporting writes a file and the fragment reports its name."""
    from src.core import logging as logging_ext
    from src.core.config import settings as core_settings

    monkeypatch.setattr(core_settings, "obs_export_dir", str(tmp_path))
    logging_ext.clear_ring_buffer()
    _emit_log("ui-export-needle")

    client = _ui_client()
    _login(client)
    fragment = client.get("/ui/fragments/export", params={"fmt": "jsonl", "component": "ui.test"})
    assert fragment.status_code == 200
    assert "Exportados" in fragment.text
    assert list(tmp_path.glob("logs-*.jsonl"))
