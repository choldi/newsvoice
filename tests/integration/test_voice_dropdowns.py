"""Integration tests for the unified voice dropdowns (Phase 9.5).

Every voice selector of the GUI is fed by the Phase 9 registry: the lesson
selects and the ``voice-list`` datalist list the voices of **every** engine
(edge, gateway, Modal), each option tagged with its engines and category,
while the document TTS select stays edge-only (user decision 6). The cast
editor renders native ``<select>`` rows swapped out-of-band when the
narration engine changes.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from src.api.main import app
from src.api.models.voices import VoiceOption, VoiceProfile
from src.core.config import settings
from src.core.voice_catalog import save_custom_profile

TOKEN = "secret-admin-token"
GATEWAY_STOCK = "es-mx-male"
CLONE_UUID = "123e4567-e89b-12d3-a456-426614174000"
MODAL_COMMITTED = "anchor_neutral_m"
MODAL_CUSTOM = "mi_voz_es"
EDGE_VOICE = VoiceOption(
    id="es-ES-DarioNeural",
    label="Dario (Neural)",
    language="es-ES",
    engines=["edge"],
    category="stock",
)


@pytest.fixture(autouse=True)
def _dropdown_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Isolate config, stub the gateway and pin both edge bindings."""
    monkeypatch.setattr(settings, "collections_file", str(tmp_path / "collections.toml"))
    monkeypatch.setattr(settings, "admin_api_token", TOKEN)
    monkeypatch.setattr(settings, "voices_dir", str(tmp_path / "voices"))
    monkeypatch.setattr(settings, "teacher_dir", str(tmp_path / "lessons"))
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
    save_custom_profile(
        VoiceProfile.model_validate(
            {
                "name": MODAL_CUSTOM,
                "label": "Profe propia",
                "language": "es",
                "instruct": "Femenina, 30s, tono cercano y agil, claridad de locucion.",
                "ref_text": "Hola, soy tu profe de ingles de siempre. " * 4,
            }
        )
    )
    with (
        patch(
            "src.api.services.voice_registry.edge_options",
            new=AsyncMock(return_value=(["en", "es"], [EDGE_VOICE])),
        ),
        patch(
            "src.api.routes_ui.edge_options",
            new=AsyncMock(return_value=(["en", "es"], [EDGE_VOICE])),
        ),
    ):
        yield


def _client() -> TestClient:
    """Build a client and authenticate it through the login endpoint."""
    client = TestClient(app)
    response = client.post("/ui/login", data={"token": TOKEN}, follow_redirects=False)
    assert response.status_code == 302
    return client


def _voice_list_of(page_text: str) -> str:
    """Extract the body of the tools page voice-list datalist."""
    return page_text.split('<datalist id="voice-list">', 1)[1].split("</datalist>", 1)[0]


# ---------------------------------------------------------------------------
# Lessons + voice-list: every engine, tagged
# ---------------------------------------------------------------------------


def test_lesson_dropdowns_merge_every_engine() -> None:
    """The lesson select and voice-list carry edge, gateway and Modal voices."""
    client = _client()

    fragment = client.get("/ui/fragments/lesson_voices", params={"mode": "dual"})
    assert fragment.status_code == 200
    for voice in (EDGE_VOICE.id, GATEWAY_STOCK, CLONE_UUID, MODAL_COMMITTED, MODAL_CUSTOM):
        assert f'value="{voice}"' in fragment.text, voice

    page = client.get("/ui/tools")
    assert page.status_code == 200
    listing = _voice_list_of(page.text)
    for voice in (EDGE_VOICE.id, GATEWAY_STOCK, MODAL_COMMITTED, MODAL_CUSTOM):
        assert f'value="{voice}"' in listing, voice


def test_doc_tts_stays_edge_only_beside_the_full_lesson_list() -> None:
    """The document select keeps the edge vocabulary (user decision 6)."""
    client = _client()
    page = client.get("/ui/tools")

    assert page.status_code == 200
    doc_form = page.text.split('action="/ui/tools/doc-tts"', 1)[1].split("</form>", 1)[0]
    assert '<select name="voice">' in doc_form
    assert EDGE_VOICE.id in doc_form
    assert GATEWAY_STOCK not in doc_form, "the gateway vocabulary must not leak in"
    assert MODAL_CUSTOM not in doc_form, "the Modal catalog must not leak in"


def test_lesson_options_tag_engines_and_categories() -> None:
    """Each option tells the engines that accept it and where it comes from."""
    client = _client()
    fragment = client.get("/ui/fragments/lesson_voices", params={"mode": "mixed"})

    assert fragment.status_code == 200
    assert "edge · stock" in fragment.text
    assert "omni_voice · stock" in fragment.text
    assert "omni_voice · cloned" in fragment.text
    assert "qwen3_tts/zonos2 · stock" in fragment.text, "committed catalog voice"
    assert "qwen3_tts/zonos2 · generated" in fragment.text, "operator-created voice"


def test_lesson_fragment_keeps_a_voice_out_of_every_catalogue() -> None:
    """An unknown id survives a re-render instead of being silently dropped."""
    client = _client()
    fragment = client.get(
        "/ui/fragments/lesson_voices",
        params={"mode": "dual", "voice": GATEWAY_STOCK, "voice2": "legacy_voice"},
    )

    assert fragment.status_code == 200
    assert 'value="legacy_voice" selected' in fragment.text
    assert "motores distintos" not in fragment.text, "unknown ids stay wildcards"


# ---------------------------------------------------------------------------
# Cast: native selects + per-row OOB refresh
# ---------------------------------------------------------------------------


def test_cast_fragment_clamps_the_row_count() -> None:
    """A crafted rows param can neither flood nor starve the response."""
    client = _client()

    huge = client.get(
        "/ui/fragments/cast_voices",
        params={"narr_backend": "qwen3_tts", "cast": "tech", "rows": "99"},
    )
    assert huge.status_code == 200
    assert huge.text.count('hx-swap-oob="innerHTML"') == 8

    empty = client.get(
        "/ui/fragments/cast_voices",
        params={"narr_backend": "omni_voice", "cast": "nueva", "rows": "0"},
    )
    assert empty.status_code == 200
    assert empty.text.count('hx-swap-oob="innerHTML"') == 2
    assert 'id="cast-voice-nueva-0"' in empty.text
