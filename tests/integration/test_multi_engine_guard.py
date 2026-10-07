"""Integration tests for the multi-engine voice guard (Phase 9.2).

The guard runs on every surface that picks a voice: the admin REST models
(``CollectionIn``, ``NarrationSpec``, ``TTSIn``), the document TTS submit, the
lesson ``resolve_cast`` path and the GUI forms/fragments. The tests use real
committed catalogs (``voice_profiles.json``) and stub the gateway/edge
catalogues so the engine classification is deterministic.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from src.api.main import app
from src.api.models.voices import VoiceOption
from src.core.config import settings

TOKEN = "secret-admin-token"
AUTH_HEADERS = {"X-Admin-Token": TOKEN}
MODAL_NAME = "anchor_neutral_m"
GATEWAY_STOCK = "es-mx-male"
CLONE_UUID = "123e4567-e89b-12d3-a456-426614174000"


@pytest.fixture(autouse=True)
def _ui_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Isolate config and keep the gateway/edge networks out of the tests."""
    monkeypatch.setattr(settings, "collections_file", str(tmp_path / "collections.toml"))
    monkeypatch.setattr(settings, "admin_api_token", TOKEN)
    monkeypatch.setattr(settings, "doc_tts_dir", str(tmp_path / "doc_tts"))
    monkeypatch.setattr(settings, "teacher_dir", str(tmp_path / "lessons"))
    monkeypatch.setattr(settings, "narration_cache_dir", str(tmp_path / "narration"))
    monkeypatch.setattr(settings, "doc_tts_link_secret", "unit-test-secret")
    monkeypatch.setattr(settings, "teacher_enabled", True)
    monkeypatch.setattr(settings, "doc_tts_enabled", True)
    from src.api.services.voices import VoicesService

    monkeypatch.setattr(VoicesService, "list_languages", AsyncMock(return_value=["en", "es"]))
    monkeypatch.setattr(
        VoicesService,
        "list_stock",
        AsyncMock(return_value=[{"voice_id": GATEWAY_STOCK, "language": "es"}]),
    )
    monkeypatch.setattr(
        VoicesService,
        "list_cloned",
        AsyncMock(return_value=[{"voice_id": CLONE_UUID, "language": "es"}]),
    )
    yield tmp_path / "collections.toml"


def _ui_client() -> TestClient:
    """Build a client and authenticate it through the login endpoint."""
    client = TestClient(app)
    response = client.post("/ui/login", data={"token": TOKEN}, follow_redirects=False)
    assert response.status_code == 302
    return client


def _rest_client() -> TestClient:
    """Build a client carrying the admin token for the REST endpoints."""
    return TestClient(app, headers=AUTH_HEADERS)


def _collection_payload(**overrides: object) -> dict[str, object]:
    """Valid admin REST payload, mirroring test_admin_endpoints."""
    payload: dict[str, object] = {
        "name": "tech",
        "feed_urls": ["https://example.com/rss"],
        "time": "08:00",
        "tts_enabled": True,
        "language": "es",
        "voice": GATEWAY_STOCK,
        "prompt_template": "default",
        "enabled": True,
    }
    payload.update(overrides)
    return payload


def _flash_of(location: str) -> str:
    """Decode the flash message carried by a redirect location."""
    from urllib.parse import unquote

    return unquote(location)


# ---------------------------------------------------------------------------
# REST guards (CollectionIn / NarrationSpec / TTSIn)
# ---------------------------------------------------------------------------


def test_rest_create_rejects_a_modal_voice_for_the_gateway() -> None:
    """A Modal catalog name in a gateway collection is a 422, not a 08:00 bug."""
    client = _rest_client()
    response = client.post(
        "/api/admin/collections",
        json=_collection_payload(tts_backend="omni_voice", voice=MODAL_NAME),
    )

    assert response.status_code == 422
    assert "belongs to engine" in response.text


def test_rest_create_keeps_network_voices_on_the_gateway() -> None:
    """Gateway stock ids, clones and edge ShortNames keep round-tripping."""
    client = _rest_client()
    for index, voice in enumerate((GATEWAY_STOCK, CLONE_UUID, "es-MX-JorgeNeural")):
        response = client.post(
            "/api/admin/collections",
            json=_collection_payload(name=f"tech{index}", voice=voice, tts_backend="omni_voice"),
        )
        assert response.status_code == 201, voice


def test_rest_create_rejects_a_mixed_narration_cast() -> None:
    """The narration cast follows the same guard as the collection voice."""
    client = _rest_client()
    response = client.post(
        "/api/admin/collections",
        json=_collection_payload(
            narration_enabled=True,
            narration_spec={
                "language": "en",
                "tts_backend": "omni_voice",
                "roles": [
                    {"name": "anchor", "voice_id": GATEWAY_STOCK, "language": "es"},
                    {"name": "cohost", "voice_id": MODAL_NAME, "language": "en"},
                ],
            },
        ),
    )

    assert response.status_code == 422
    assert "mixed TTS engines" in response.text


def test_rest_create_accepts_a_uniform_modal_cast() -> None:
    """Every voice in a Modal cast keeps the collection saveable."""
    client = _rest_client()
    response = client.post(
        "/api/admin/collections",
        json=_collection_payload(
            tts_backend="qwen3_tts",
            voice="",
            narration_enabled=True,
            narration_spec={
                "language": "en",
                "tts_backend": "qwen3_tts",
                "roles": [
                    {"name": "anchor", "voice_id": "anchor_neutral_m", "language": "en"},
                    {"name": "cohost", "voice_id": "anchor_warm_m", "language": "en"},
                ],
            },
        ),
    )

    assert response.status_code == 201


def test_tts_rejects_a_modal_voice_for_edge() -> None:
    """POST /api/admin/tts validates voice against the chosen/default engine."""
    client = _rest_client()
    response = client.post(
        "/api/admin/tts",
        json={"markdown": "Hola clase", "voice": MODAL_NAME, "backend": "edge"},
    )

    assert response.status_code == 422
    assert "belongs to engine" in response.text


def test_doc_tts_rejects_a_non_edge_voice() -> None:
    """Document TTS synthesizes through edge only (user decision, Fase 9)."""
    client = _rest_client()
    response = client.post(
        "/api/admin/documents/tts",
        files={"file": ("apuntes.txt", b"Un texto corto.", "text/plain")},
        data={"voice": MODAL_NAME},
    )

    assert response.status_code == 422
    assert "document TTS only accepts edge voices" in response.text


# ---------------------------------------------------------------------------
# Lesson path (resolve_cast)
# ---------------------------------------------------------------------------


def test_lesson_submit_rejects_mixed_engine_voices() -> None:
    """The dual-voice form cannot queue a class that mixes two engines."""
    client = _ui_client()
    response = client.post(
        "/ui/tools/lessons",
        files={"file": ("apuntes.md", b"# Clase\n\nHola.", "text/markdown")},
        data={
            "mode": "dual",
            "target_language": "en",
            "native_language": "es",
            "voice": GATEWAY_STOCK,
            "voice2": MODAL_NAME,
        },
        follow_redirects=False,
    )

    assert response.status_code == 302
    location = response.headers["location"]
    assert "level=error" in location
    assert "mixed TTS engines" in _flash_of(location)


# ---------------------------------------------------------------------------
# GUI create/update
# ---------------------------------------------------------------------------


def test_ui_create_flashes_a_mixed_engine_cast() -> None:
    """A modal voice typed into a gateway cast flashes the readable error."""
    client = _ui_client()
    response = client.post(
        "/ui/collections",
        data={
            "name": "tech",
            "feed_urls": "https://example.com/rss",
            "narration_enabled": "on",
            "role0_name": "anchor",
            "role0_voice": MODAL_NAME,
            "role0_lang": "en",
            "narr_backend": "omni_voice",
        },
        follow_redirects=False,
    )

    assert response.status_code == 302
    location = response.headers["location"]
    assert "level=error" in location
    assert "belongs to engine" in _flash_of(location)

    from src.api.services.collections_repo import CollectionsRepository

    assert CollectionsRepository().get("tech") is None, "nothing is stored"


def test_ui_create_accepts_gateway_and_unknown_voices() -> None:
    """Known-but-unknown ids stay wildcards: gateway ids and typed legacy ids."""
    client = _ui_client()
    for name, voice in (("tech", GATEWAY_STOCK), ("legacy", "voice_from_memory")):
        response = client.post(
            "/ui/collections",
            data={
                "name": name,
                "feed_urls": "https://example.com/rss",
                "narration_enabled": "on",
                "role0_name": "anchor",
                "role0_voice": voice,
                "role0_lang": "en",
                "narr_backend": "omni_voice",
            },
            follow_redirects=False,
        )
        assert response.status_code == 302, name
        assert "level=error" not in response.headers["location"], name
        assert "creada" in _flash_of(response.headers["location"]), name


def test_ui_advanced_json_mixed_cast_is_rejected_not_dropped() -> None:
    """A well-formed spec that fails validation must flash, not silently vanish."""
    client = _ui_client()
    mixed_spec = (
        '{"title":"X","language":"en","tts_backend":"omni_voice","roles":['
        f'{{"name":"a","voice_id":"{GATEWAY_STOCK}"}},'
        f'{{"name":"b","voice_id":"{MODAL_NAME}"}}]}}'
    )
    response = client.post(
        "/ui/collections",
        data={
            "name": "tech",
            "feed_urls": "https://example.com/rss",
            "narration_enabled": "on",
            "narration_spec": mixed_spec,
        },
        follow_redirects=False,
    )

    assert response.status_code == 302
    assert "level=error" in response.headers["location"]
    assert "mixed TTS engines" in _flash_of(response.headers["location"])

    from src.api.services.collections_repo import CollectionsRepository

    assert CollectionsRepository().get("tech") is None


# ---------------------------------------------------------------------------
# Cast datalist fragment + wiring
# ---------------------------------------------------------------------------


def test_cast_voices_fragment_follows_the_engine() -> None:
    """The OOB payload offers the vocabulary of the selected engine."""
    client = _ui_client()
    params = {"cast": "tech", "rows": "3"}

    modal = client.get("/ui/fragments/cast_voices", params={"narr_backend": "qwen3_tts", **params})
    assert modal.status_code == 200
    assert MODAL_NAME in modal.text
    assert GATEWAY_STOCK not in modal.text
    assert modal.text.count('hx-swap-oob="innerHTML"') == 3
    for row in range(3):
        assert f'id="cast-voice-tech-{row}"' in modal.text

    gateway = client.get(
        "/ui/fragments/cast_voices", params={"narr_backend": "omni_voice", **params}
    )
    assert gateway.status_code == 200
    assert f'value="{GATEWAY_STOCK}"' in gateway.text
    assert MODAL_NAME not in gateway.text

    fallback = client.get("/ui/fragments/cast_voices", params={"narr_backend": "banana", **params})
    assert fallback.status_code == 200
    assert f'value="{GATEWAY_STOCK}"' in fallback.text


def test_collections_page_wires_the_cast_selects() -> None:
    """Both forms get per-row selects refreshed by the hx-fetchable fragment."""
    (tmp_toml := Path(settings.collections_file)).parent.mkdir(parents=True, exist_ok=True)
    tmp_toml.write_text(
        '[[collections]]\nname = "tech"\ntime = "08:00"\n'
        'feed_urls = ["https://example.com/rss"]\n'
        "narration_enabled = true\n"
        'narration_spec = { language = "en", roles = [{name="a",voice_id="es-mx-male"}] }\n'
    )
    client = _ui_client()
    page = client.get("/ui/collections")

    assert page.status_code == 200
    # Every cast row is a <select>; no input+datalist pair remains.
    assert '<div id="cast-voice-tech-0"><select name="role0_voice"' in page.text
    assert '<div id="cast-voice-nueva-0"><select name="role0_voice"' in page.text
    assert 'list="cast-voices' not in page.text, "the cast datalists are gone"
    # The engine select targets the sink span and asks for its row count.
    assert page.text.count('hx-get="/ui/fragments/cast_voices?cast=') >= 2
    assert 'id="cast-voices-tech"' in page.text
    assert 'id="cast-voices-nueva"' in page.text
    assert page.text.count('hx-target="#cast-voices-') >= 2
    # The initial options follow the engine of each form without any fetch.
    assert f'value="{GATEWAY_STOCK}"' in page.text


# ---------------------------------------------------------------------------
# Lesson fragment: the out-of-band submit guard
# ---------------------------------------------------------------------------


def test_lesson_fragment_disables_the_submit_for_mixed_voices() -> None:
    """A dual pick from two engines arrives with the warning and a dead button."""
    client = _ui_client()
    fragment = client.get(
        "/ui/fragments/lesson_voices",
        params={"mode": "dual", "voice": GATEWAY_STOCK, "voice2": MODAL_NAME},
    )

    assert fragment.status_code == 200
    assert "motores distintos" in fragment.text
    assert 'class="primary" disabled' in fragment.text
    assert 'id="lesson-submit"' in fragment.text, "the OOB button must arrive"
    assert 'name="current_mode" value="dual"' in fragment.text


def test_lesson_fragment_keeps_the_submit_for_one_engine() -> None:
    """Gateway stock + clone are one engine (omni_voice): the button stays live."""
    client = _ui_client()
    fragment = client.get(
        "/ui/fragments/lesson_voices",
        params={"mode": "dual", "voice": GATEWAY_STOCK, "voice2": CLONE_UUID},
    )

    assert fragment.status_code == 200
    assert "motores distintos" not in fragment.text
    assert 'class="primary" disabled' not in fragment.text
    assert 'id="lesson-submit"' in fragment.text


# ---------------------------------------------------------------------------
# Tools page: doc-TTS dropdown is edge-only
# ---------------------------------------------------------------------------


def test_doc_tts_dropdown_only_offers_edge_voices() -> None:
    """The document voice picker shows edge ShortNames, never gateway ids."""
    edge_option = VoiceOption(
        id="es-ES-DarioNeural",
        label="Dario (Neural)",
        language="es-ES",
        engines=["edge"],
        category="stock",
    )
    client = _ui_client()
    with patch(
        "src.api.routes_ui.edge_options",
        new=AsyncMock(return_value=(["es"], [edge_option])),
    ):
        page = client.get("/ui/tools")

    assert page.status_code == 200
    doc_form = page.text.split('action="/ui/tools/doc-tts"', 1)[1].split("</form>", 1)[0]
    assert 'id="picker-doc"' in doc_form
    assert "es-ES-DarioNeural" in doc_form
    assert GATEWAY_STOCK not in doc_form, "the gateway vocabulary must not leak in"
