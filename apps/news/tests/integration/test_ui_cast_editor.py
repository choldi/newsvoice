"""Integration tests for the structured narration cast editor (GUI, Phase 7)."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, patch
from urllib.parse import unquote

import pytest
from fastapi.testclient import TestClient

from src.api.db.database import RunsRepository
from src.api.main import app
from src.core.config import settings

TOKEN = "secret-admin-token"


@pytest.fixture(autouse=True)
def _ui_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Isolate collections/auth and keep the voice gateway out of tests."""
    monkeypatch.setattr(settings, "collections_file", str(tmp_path / "collections.toml"))
    monkeypatch.setattr(settings, "admin_api_token", TOKEN)
    from src.api.services.voices import VoicesService

    monkeypatch.setattr(VoicesService, "list_languages", AsyncMock(return_value=["es", "en"]))
    monkeypatch.setattr(
        VoicesService,
        "list_stock",
        AsyncMock(return_value=[{"voice_id": "es-mx-male", "language": "es"}]),
    )
    monkeypatch.setattr(
        VoicesService,
        "list_cloned",
        AsyncMock(return_value=[{"voice_id": "clone-1", "language": "es"}]),
    )
    return tmp_path / "collections.toml"


def _client() -> TestClient:
    """Build a client authenticated through the login endpoint."""
    client = TestClient(app)
    response = client.post("/ui/login", data={"token": TOKEN}, follow_redirects=False)
    assert response.status_code == 302
    return client


def _flash_of(location: str) -> str:
    """Decode the flash message carried by a redirect location."""
    return unquote(location)


def _write_spec_collection(path: Path) -> None:
    """Write a collection with a real NarrationSpec (like production gaming)."""
    path.write_text(
        "[[collections]]\n"
        'name = "gaming"\n'
        'time = "08:00"\n'
        'feed_urls = ["https://example.com/rss"]\n'
        "narration_enabled = true\n"
        'narration_strategy = "turns"\n'
        'narration_spec = { title = "Game Over", language = "en", '
        'roles = [{name="michael",voice_id="clone-1",speed=1.2,emotion="excited"},'
        '{name="dwight",voice_id="es-mx-male",speed=1.0,emotion="calm"}], '
        "pause_ms = 350 }\n"
    )


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def test_page_renders_the_structured_cast_for_a_spec_collection(_ui_env: Path) -> None:
    """A collection with a spec shows its roles in inputs, not just JSON."""
    _write_spec_collection(_ui_env)
    client = _client()
    response = client.get("/ui/collections")

    assert response.status_code == 200, "the page must not crash on a NarrationSpec"
    body = response.text
    # Two existing roles + one free row (cap is 8).
    card = body.split('<details class="card" id="d-gaming"')[1].split("</details>")[0]
    assert 'name="role0_name" value="michael"' in card
    assert '<div id="cast-voice-gaming-0"><select name="role0_voice"' in card
    assert 'value="clone-1" selected' in card, "the saved voice stays selected"
    assert 'name="role1_name" value="dwight"' in card
    assert 'value="excited"' in card
    assert 'name="narr_title" value="Game Over"' in card
    assert 'name="narr_pause_ms" min="0" max="5000" value="350"' in card
    # The advanced JSON mirrors the spec for the fallback path.
    assert '"Game Over"' in card


def test_page_keeps_a_voice_the_catalogue_lacks(_ui_env: Path) -> None:
    """A legacy voice id absent from every catalogue is kept, not dropped."""
    _write_spec_collection(_ui_env)
    path = Path(_ui_env)
    path.write_text(path.read_text().replace('voice_id="clone-1"', 'voice_id="legacy_voice"'))
    client = _client()
    response = client.get("/ui/collections")

    assert response.status_code == 200
    card = response.text.split('<details class="card" id="d-gaming"')[1].split("</details>")[0]
    assert '<option value="legacy_voice" selected>legacy_voice</option>' in card
    assert 'name="role1_name" value="dwight"' in card


def test_page_renders_free_rows_for_a_collection_without_spec(_ui_env: Path) -> None:
    """No spec means two empty rows so a dual cast can be typed from scratch."""
    _ui_env.write_text(
        '[[collections]]\nname = "plain"\ntime = "08:00"\nfeed_urls = ["https://example.com/rss"]\n'
    )
    client = _client()
    response = client.get("/ui/collections")

    assert response.status_code == 200
    card = response.text.split('<details class="card" id="d-plain"')[1].split("</details>")[0]
    assert 'name="role0_name" value=""' in card
    assert 'name="role1_name" value=""' in card


# ---------------------------------------------------------------------------
# Save round-trip
# ---------------------------------------------------------------------------


def _update_payload(**overrides: str) -> dict[str, str]:
    """Build an update payload mirroring the rendered edit form."""
    payload = {
        "name": "gaming",
        "feed_urls": "https://example.com/rss",
        "time": "08:00",
        "language": "en",
        "narration_enabled": "on",
        "narration_strategy": "turns",
        "narration_deliver_audio": "on",
        "main_story_count": "5",
        "headline_count": "5",
        "narr_title": "Game Over",
        "narr_language": "en",
        "narr_pause_ms": "350",
        "narr_speed": "1.0",
        "narr_backend": "omni_voice",
        "role0_name": "michael",
        "role0_voice": "clone-1",
        "role0_speed": "1.2",
        "role0_emotion": "excited",
        "role1_name": "dwight",
        "role1_voice": "es-mx-male",
        "role1_speed": "1.0",
        "role1_emotion": "calm",
        "role2_name": "",
        "role2_voice": "",
        "enabled": "on",
    }
    payload.update(overrides)
    return payload


def test_update_saves_the_structured_cast(_ui_env: Path) -> None:
    """Posting the rows persists them as the collection's NarrationSpec."""
    _write_spec_collection(_ui_env)
    client = _client()
    response = client.post(
        "/ui/collections/gaming/update", data=_update_payload(), follow_redirects=False
    )
    assert response.status_code == 302
    assert "actualizada" in _flash_of(response.headers["location"])

    from src.api.services.collections_repo import CollectionsRepository

    collection = CollectionsRepository().get("gaming")
    assert collection is not None
    spec = collection.narration_spec
    assert spec is not None
    assert [r.name for r in spec.roles] == ["michael", "dwight"]
    assert spec.roles[0].voice_id == "clone-1"
    assert spec.roles[0].speed == 1.2
    assert spec.roles[0].emotion == "excited"
    assert spec.pause_ms == 350
    assert spec.title == "Game Over"


def test_update_rejects_a_half_filled_role(_ui_env: Path) -> None:
    """A name without a voice is a save error, not a silently dropped role."""
    _write_spec_collection(_ui_env)
    client = _client()
    response = client.post(
        "/ui/collections/gaming/update",
        data=_update_payload(role1_voice=""),
        follow_redirects=False,
    )
    assert "van juntos" in _flash_of(response.headers["location"])

    from src.api.services.collections_repo import CollectionsRepository

    collection = CollectionsRepository().get("gaming")
    assert collection is not None
    assert [r.name for r in collection.narration_spec.roles] == ["michael", "dwight"]


def test_update_keeps_fields_the_editor_does_not_expose(_ui_env: Path) -> None:
    """Music beds and limits from the base spec survive a structured save."""
    _write_spec_collection(_ui_env)
    # Give the existing spec a music bed through the legacy JSON (base).
    from src.api.models.narration import NarrationSpec, SpeakerRole
    from src.api.services.collections_repo import CollectionsRepository

    repo = CollectionsRepository()
    collection = repo.get("gaming")
    assert collection is not None
    collection.narration_spec = NarrationSpec(
        title="Game Over",
        language="en",
        roles=[SpeakerRole(name="michael", voice_id="clone-1")],
        pause_ms=350,
        max_chars_per_request=2500,
    )
    repo.save_all([collection])

    client = _client()
    client.post("/ui/collections/gaming/update", data=_update_payload(), follow_redirects=False)

    saved = repo.get("gaming")
    assert saved is not None
    assert saved.narration_spec.max_chars_per_request == 2500
    assert [r.name for r in saved.narration_spec.roles] == ["michael", "dwight"]


def test_legacy_json_textarea_still_works(_ui_env: Path) -> None:
    """With no structured row filled, the advanced JSON textarea wins."""
    _write_spec_collection(_ui_env)
    client = _client()
    payload = _update_payload(role0_name="", role0_voice="", role1_name="", role1_voice="")
    payload["narration_spec"] = (
        '{"title":"Legacy","language":"es","roles":[{"name":"solo","voice_id":"clone-1"}]}'
    )
    client.post("/ui/collections/gaming/update", data=payload, follow_redirects=False)

    from src.api.services.collections_repo import CollectionsRepository

    collection = CollectionsRepository().get("gaming")
    assert collection is not None
    assert collection.narration_spec.title == "Legacy"
    assert [r.name for r in collection.narration_spec.roles] == ["solo"]


# ---------------------------------------------------------------------------
# Render button
# ---------------------------------------------------------------------------


def test_narrate_button_without_spec_explains(_ui_env: Path) -> None:
    """The per-collection render button requires a NarrationSpec."""
    _ui_env.write_text(
        '[[collections]]\nname = "plain"\ntime = "08:00"\nfeed_urls = ["https://example.com/rss"]\n'
    )
    client = _client()
    response = client.post("/ui/collections/plain/narrate", follow_redirects=False)

    assert response.status_code == 302
    assert "NarrationSpec" in _flash_of(response.headers["location"])


async def test_narrate_button_starts_the_render(_ui_env: Path) -> None:
    """The button renders the latest digest with the collection's cast."""
    _write_spec_collection(_ui_env)
    await RunsRepository().record_run(
        workflow_id="digest-pipeline-gaming-20261003120000",
        collection="gaming",
        status="ok",
        article_count=3,
        cluster_count=1,
    )
    client = _client()
    with patch(
        "src.api.routes_ui.TemporalAdminService.start_narration_render",
        new_callable=AsyncMock,
        return_value={
            "workflow_id": "narration-gaming-1",
            "collection": "gaming",
            "status": "started",
        },
    ) as start:
        response = client.post("/ui/collections/gaming/narrate", follow_redirects=False)

    assert response.status_code == 302
    location = _flash_of(response.headers["location"])
    assert "narration-gaming-1" in location
    assert "digest-pipeline-gaming-20261003120000" in location
    kwargs = start.call_args.kwargs
    assert kwargs["run_id"] == "digest-pipeline-gaming-20261003120000"
    assert kwargs["strategy"] == "turns"
    assert [r.name for r in kwargs["spec"].roles] == ["michael", "dwight"]


def test_narrate_button_without_runs_explains(_ui_env: Path) -> None:
    """A collection that never ran cannot source a script yet."""
    _write_spec_collection(_ui_env)
    client = _client()
    response = client.post("/ui/collections/gaming/narrate", follow_redirects=False)

    assert "genera un digesto" in _flash_of(response.headers["location"])
