"""Integration tests for the tools voice pickers and their search (Phase 9.7).

The lesson and document-TTS forms now use a searchable picker instead of a
long ``<select>``: a glob query field (``Mich*clo``) and a language select
re-fetch the radio results over htmx, GET /ui/fragments/voice_picker renders
only those results, and the current selection survives the search because the
fragment keeps it (checked) even when it falls outside the match. The
document picker stays edge-only, the lesson pickers cover every engine.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from src.api.main import app
from src.api.models.voices import VoiceOption, VoiceProfile
from src.api.routes_ui import VOICE_PICKER_LIMIT
from src.core.config import settings
from src.core.voice_catalog import save_custom_profile

TOKEN = "secret-admin-token"
GATEWAY_STOCK = "es-mx-male"
CLONE_UUID = "123e4567-e89b-12d3-a456-426614174000"
MODAL_CUSTOM = "mi_voz_es"
EDGE_VOICE = VoiceOption(
    id="es-ES-DarioNeural",
    label="Dario (Neural)",
    language="es-ES",
    engines=["edge"],
    category="stock",
)


@pytest.fixture(autouse=True)
def _picker_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Isolate config, stub the gateway and pin the edge bindings."""
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
        AsyncMock(
            return_value=[
                {
                    "voice_id": CLONE_UUID,
                    "name": "Michael_sss en(glish) Voz clonada",
                    "language": "es",
                }
            ]
        ),
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
# The search fragment
# ---------------------------------------------------------------------------


def test_picker_finds_the_clone_with_the_glob_query() -> None:
    """``Mich*clo`` reaches a label that does not contain the literal text."""
    client = _client()

    results = client.get("/ui/fragments/voice_picker", params={"q": "Mich*clo"})

    assert results.status_code == 200
    assert f'value="{CLONE_UUID}"' in results.text
    assert "Michael_sss en(glish) Voz clonada" in results.text
    assert "1 coincidencia" in results.text


def test_picker_filters_by_language_prefix() -> None:
    """The language select folds locales to their ISO 639-1 prefix."""
    client = _client()

    english = client.get("/ui/fragments/voice_picker", params={"lang": "en"})
    spanish = client.get("/ui/fragments/voice_picker", params={"lang": "es"})

    assert english.status_code == 200
    assert 'value="anchor_neutral_m"' in english.text, "English committed profile"
    assert 'value="es_anchor_neutral_m"' not in english.text, "es_* profiles must not match en"
    assert spanish.status_code == 200
    assert 'value="es_anchor_neutral_m"' in spanish.text
    assert 'value="anchor_neutral_m"' not in spanish.text


def test_picker_restricts_the_vocabulary_by_engine() -> None:
    """The doc picker's engine hint keeps gateway ids out of the results."""
    client = _client()

    edge_only = client.get("/ui/fragments/voice_picker", params={"engines": "edge", "q": "Neural"})

    assert edge_only.status_code == 200
    assert 'value="es-ES-DarioNeural"' in edge_only.text
    assert GATEWAY_STOCK not in edge_only.text, "the gateway vocabulary must not leak in"


def test_picker_offers_the_literal_option_for_an_unknown_id() -> None:
    """A search with zero matches offers to send the query text as-is."""
    client = _client()

    results = client.get("/ui/fragments/voice_picker", params={"q": "voz fantasma"})

    assert results.status_code == 200
    assert 'value="voz fantasma" checked' in results.text
    assert "tal cual" in results.text


def test_picker_keeps_the_checked_voice_visible_after_a_search() -> None:
    """A selection outside the match survives the re-render, still checked."""
    client = _client()

    results = client.get("/ui/fragments/voice_picker", params={"q": "anchor", "voice": CLONE_UUID})

    assert results.status_code == 200
    assert f'value="{CLONE_UUID}" checked' in results.text
    assert 'value="anchor_neutral_m"' in results.text, "the match itself is still listed"


def test_picker_does_not_duplicate_the_literal_for_the_current_voice() -> None:
    """When the current voice is the query, it shows once, as the checked one."""
    client = _client()

    results = client.get(
        "/ui/fragments/voice_picker", params={"q": "voz fantasma", "voice": "voz fantasma"}
    )

    assert results.status_code == 200
    assert results.text.count('value="voz fantasma"') == 1
    assert 'value="voz fantasma" checked' in results.text
    assert "tal cual" not in results.text, "the literal option is redundant here"


def test_picker_marks_the_default_option() -> None:
    """The value='' option is checked until a real voice is picked."""
    client = _client()

    empty = client.get("/ui/fragments/voice_picker", params={"empty_label": "(por defecto)"})
    picked = client.get(
        "/ui/fragments/voice_picker",
        params={"empty_label": "(por defecto)", "voice": GATEWAY_STOCK},
    )

    assert empty.status_code == 200
    assert "(por defecto)" in empty.text
    assert 'value="" checked' in empty.text
    assert picked.status_code == 200
    assert f'value="{GATEWAY_STOCK}" checked' in picked.text
    assert 'value="" checked' not in picked.text


def test_picker_caps_the_results_and_reports_the_total() -> None:
    """The initial list is capped and the count hint asks the operator to type."""
    client = _client()

    results = client.get("/ui/fragments/voice_picker", params={"empty_label": "(por defecto)"})

    assert results.status_code == 200
    radios = results.text.count('type="radio"')
    assert radios == VOICE_PICKER_LIMIT + 1, "cap + the (por defecto) empty option"
    assert "28 voces · escribe para filtrar" in results.text


# ---------------------------------------------------------------------------
# The tools page wires the pickers
# ---------------------------------------------------------------------------


def test_tools_page_wires_searchable_pickers_and_drops_the_datalist() -> None:
    """Both tools forms mount a picker; the voice-list datalist is gone."""
    client = _client()
    page = client.get("/ui/tools")

    assert page.status_code == 200
    assert 'id="picker-doc"' in page.text
    assert 'id="picker-voice"' in page.text
    assert 'name="voice_q"' in page.text
    assert "voice-list" not in page.text, "the old datalist is retired"


def test_lesson_picker_results_carry_the_guard_listener() -> None:
    """A pick in the lessons block re-runs the guard fragment (Phase 9.2)."""
    client = _client()
    fragment = client.get("/ui/fragments/lesson_voices", params={"mode": "dual"})

    assert fragment.status_code == 200
    assert 'id="voice-results-voice"' in fragment.text
    assert 'id="voice-results-voice2"' in fragment.text
    listens = fragment.text.count('hx-get="/ui/fragments/lesson_voices"')
    assert listens == 2, "both lesson slots recompute the mixed-engine guard on pick"
