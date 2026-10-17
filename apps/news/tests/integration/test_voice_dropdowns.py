"""Integration tests for the unified voice pickers (Phases 9.5 + 9.7).

Every voice selector of the GUI is fed by the Phase 9 registry: the lesson
pickers and the ``voice_picker`` fragment list the voices of **every** engine
(edge, gateway, Modal), each result tagged with its engines and category,
while the document TTS picker stays edge-only (user decision 6). The cast
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


# ---------------------------------------------------------------------------
# Lessons + search pickers: every engine, tagged
# ---------------------------------------------------------------------------


def test_lesson_pickers_merge_every_engine() -> None:
    """The pickers find edge, gateway and Modal voices with one search."""
    client = _client()
    for voice in (EDGE_VOICE.id, GATEWAY_STOCK, CLONE_UUID, MODAL_COMMITTED, MODAL_CUSTOM):
        found = client.get("/ui/fragments/voice_picker", params={"q": voice})
        assert found.status_code == 200
        assert f'value="{voice}"' in found.text, voice

    page = client.get("/ui/tools")
    assert page.status_code == 200
    assert f'value="{MODAL_COMMITTED}"' in page.text, (
        "the default mixed/en picker lists English voices"
    )
    assert f'value="{MODAL_CUSTOM}"' not in page.text, (
        "the default mixed/en picker filters out 'es' voices (Phase 9.8)"
    )


def test_doc_tts_stays_edge_only_beside_the_full_lesson_list() -> None:
    """The document picker keeps the edge vocabulary (user decision 6)."""
    client = _client()
    page = client.get("/ui/tools")

    assert page.status_code == 200
    doc_form = page.text.split('action="/ui/tools/doc-tts"', 1)[1].split("</form>", 1)[0]
    assert 'id="picker-doc"' in doc_form
    assert f'value="{EDGE_VOICE.id}"' in doc_form
    assert GATEWAY_STOCK not in doc_form, "the gateway vocabulary must not leak in"
    assert MODAL_CUSTOM not in doc_form, "the Modal catalog must not leak in"


def test_lesson_options_tag_engines_and_categories() -> None:
    """Each result tells the engines that accept it and where it comes from."""
    expected = {
        EDGE_VOICE.id: "edge · stock",
        GATEWAY_STOCK: "omni_voice · stock",
        CLONE_UUID: "omni_voice · cloned",
        MODAL_COMMITTED: "qwen3_tts/zonos2/voxcpm2 · stock",
        MODAL_CUSTOM: "qwen3_tts/zonos2/voxcpm2 · generated",
    }
    client = _client()
    for voice, tag in expected.items():
        results = client.get("/ui/fragments/voice_picker", params={"q": voice})
        assert results.status_code == 200
        assert f'value="{voice}"' in results.text, voice
        assert tag in results.text, voice


def test_lesson_picker_carries_the_typed_filter_across_rerenders() -> None:
    """The voice_q parameters survive a lesson fragment re-render."""
    client = _client()
    fragment = client.get(
        "/ui/fragments/lesson_voices",
        params={
            "mode": "mixed",
            "voice_q": "anchor",
            "native_language": "es",
            "target_language": "en",
        },
    )

    assert fragment.status_code == 200
    assert 'name="voice_q" value="anchor"' in fragment.text
    assert 'name="voice_lang"' not in fragment.text, "the form's language select replaces it"
    assert 'name="voice2_lang"' not in fragment.text, "the form's language select replaces it"
    assert 'id="voice-results-voice"' in fragment.text
    assert 'hx-get="/ui/fragments/lesson_voices"' in fragment.text, "the guard listens on picks"


def test_lesson_fragment_keeps_a_voice_out_of_every_catalogue() -> None:
    """An unknown id survives a re-render instead of being silently dropped."""
    client = _client()
    fragment = client.get(
        "/ui/fragments/lesson_voices",
        params={"mode": "dual", "voice": GATEWAY_STOCK, "voice2": "legacy_voice"},
    )

    assert fragment.status_code == 200
    assert 'value="legacy_voice" checked' in fragment.text
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
