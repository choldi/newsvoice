"""Integration tests for the unified voice table (Phase 9.6).

The /ui/voices page replaces the three listing cards (generated stays as a
polling card) with one table over the Phase 9 registry: a filter row
(glob query, language prefix, category, engine, page size) and a pager,
all fed by GET /ui/fragments/voices_table. Designed voices carry their
manifest status and a link to the reference WAV.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from src.api.main import app
from src.api.models.voices import VoiceManifestEntry, VoiceOption, VoiceProfile
from src.core.config import settings
from src.core.voice_catalog import save_custom_profile, sha256_text, write_manifest

TOKEN = "secret-admin-token"
GATEWAY_STOCK = "es-mx-male"
CLONE_UUID = "123e4567-e89b-12d3-a456-426614174000"
GENERATED = "mi_voz_es"
PENDING = "pendiente_es"
EDGE_ES = VoiceOption(
    id="es-ES-DarioNeural",
    label="Dario (Neural)",
    language="es-ES",
    engines=["edge"],
    category="stock",
)
EDGE_EN = VoiceOption(
    id="en-US-GuyNeural",
    label="Guy (Neural)",
    language="en-US",
    engines=["edge"],
    category="stock",
)
INSTRUCT = "Femenina, 30s, tono cercano y agil, claridad de locucion."
REF_TEXT = "Hola, soy tu profe de ingles de siempre. " * 4


@pytest.fixture(autouse=True)
def _table_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Isolate config, stub the gateway/edge sources and seed the catalog."""
    voices = tmp_path / "voices"
    voices.mkdir()
    monkeypatch.setattr(settings, "voices_dir", str(voices))
    monkeypatch.setattr(settings, "admin_api_token", TOKEN)
    monkeypatch.setattr(settings, "collections_file", str(tmp_path / "collections.toml"))
    from src.api.services.voices import VoicesService

    monkeypatch.setattr(VoicesService, "list_languages", AsyncMock(return_value=["es"]))
    monkeypatch.setattr(
        VoicesService,
        "list_stock",
        AsyncMock(return_value=[{"voice_id": GATEWAY_STOCK, "language": "es", "name": "MX macho"}]),
    )
    monkeypatch.setattr(
        VoicesService,
        "list_cloned",
        AsyncMock(
            return_value=[
                {
                    "voice_id": CLONE_UUID,
                    "language": "es-ES",
                    "name": "Michael_sss en(glish) Voz clonada",
                }
            ]
        ),
    )
    for name in (GENERATED, PENDING):
        save_custom_profile(_profile(name))
    write_manifest(
        {GENERATED: _entry(_profile(GENERATED), f"refs/{GENERATED}.wav")},
        voices / "manifest.json",
    )
    wav = voices / "refs" / f"{GENERATED}.wav"
    wav.parent.mkdir(parents=True, exist_ok=True)
    wav.write_bytes(b"RIFF-REFERENCE")
    with patch(
        "src.api.services.voice_registry.edge_options",
        new=AsyncMock(return_value=(["en", "es"], [EDGE_ES, EDGE_EN])),
    ):
        yield


def _profile(name: str) -> VoiceProfile:
    """Build a valid custom profile for the seeded catalog voices."""
    return VoiceProfile.model_validate(
        {"name": name, "label": name, "language": "es", "instruct": INSTRUCT, "ref_text": REF_TEXT}
    )


def _entry(profile: VoiceProfile, file: str) -> VoiceManifestEntry:
    """Build a fresh manifest entry for a profile."""
    return VoiceManifestEntry(
        file=file,
        sha256="a" * 64,
        sample_rate=24000,
        duration_seconds=15.0,
        design_model="Qwen/Qwen3-TTS-12Hz-1.7B-VoiceDesign",
        instruct_sha256=sha256_text(profile.instruct),
        ref_text_sha256=sha256_text(profile.ref_text),
        created_at="2026-10-07T00:00:00Z",
    )


def _client() -> TestClient:
    """Build a client authenticated through the login endpoint."""
    client = TestClient(app)
    response = client.post("/ui/login", data={"token": TOKEN}, follow_redirects=False)
    assert response.status_code == 302
    return client


# ---------------------------------------------------------------------------
# Page
# ---------------------------------------------------------------------------


def test_page_renders_the_unified_table_and_drops_the_old_cards() -> None:
    """The page swaps the stock/cloned/pick cards for the filterable table."""
    client = _client()
    page = client.get("/ui/voices")

    assert page.status_code == 200
    text = page.text
    assert 'id="voices-table"' in text
    for field in ('name="q"', 'name="lang"', 'name="category"', 'name="engine"', 'name="size"'):
        assert field in text, field
    # The first paint already carries rows and the per-row preview button.
    assert "<th>Voz</th>" in text
    assert 'name="voice" value="' in text
    assert 'hx-post="/ui/voices/test"' in text
    assert 'id="voice-sample"' in text
    assert "página 1 de" in text
    # Old listing cards are gone; the generated card and the forms stay.
    assert "<h2>Voces generadas</h2>" in text
    assert 'id="voices-generated"' in text
    assert "<h2>Voces clonadas</h2>" not in text
    assert "<h2>Voces de stock</h2>" not in text
    assert "<h2>Probar voces</h2>" not in text
    assert '<select name="voice">' not in text, "the 740-voice picker select is retired"
    assert 'action="/ui/voices/custom"' in text
    assert 'action="/ui/voices/clone"' in text


def test_page_offers_one_language_option_per_iso_prefix() -> None:
    """es-ES/es/es-MX fold into one 'es' bucket, en-US into 'en'."""
    client = _client()
    page = client.get("/ui/voices")

    assert page.status_code == 200
    assert '<option value="es"' in page.text
    assert '<option value="en"' in page.text


# ---------------------------------------------------------------------------
# Fragment: filters
# ---------------------------------------------------------------------------


def test_fragment_filters_by_glob_query() -> None:
    """'Mich*clo' finds the clone whose label matches the pattern."""
    client = _client()
    response = client.get("/ui/fragments/voices_table", params={"q": "Mich*clo"})

    assert response.status_code == 200
    text = response.text
    assert CLONE_UUID in text
    assert EDGE_ES.id not in text
    assert GATEWAY_STOCK not in text
    assert "1 voz" in text


def test_fragment_filters_by_language_prefix_category_and_engine() -> None:
    """Each structured filter narrows the listing on its own."""
    client = _client()

    english = client.get("/ui/fragments/voices_table", params={"lang": "en"})
    assert EDGE_EN.id in english.text
    assert EDGE_ES.id not in english.text

    generated = client.get("/ui/fragments/voices_table", params={"category": "generated"})
    assert GENERATED in generated.text
    assert PENDING in generated.text
    assert EDGE_ES.id not in generated.text

    edge = client.get("/ui/fragments/voices_table", params={"engine": "edge"})
    assert EDGE_ES.id in edge.text
    assert GATEWAY_STOCK not in edge.text
    assert GENERATED not in edge.text


def test_fragment_shows_the_empty_state_for_a_miss() -> None:
    """A query with no matches renders a row explaining it, never a 500."""
    client = _client()
    response = client.get("/ui/fragments/voices_table", params={"q": "zzz-ninguna"})

    assert response.status_code == 200
    assert "Ninguna voz coincide con los filtros" in response.text


# ---------------------------------------------------------------------------
# Fragment: design status and actions
# ---------------------------------------------------------------------------


def test_fragment_badges_the_design_status_and_links_the_reference() -> None:
    """A generada voice gets the ok badge and its WAV; a pendiente one neither."""
    client = _client()
    response = client.get("/ui/fragments/voices_table", params={"category": "generated"})

    assert response.status_code == 200
    text = response.text
    assert 'class="badge ok">generada</span>' in text
    assert f'href="/ui/voices/audio/{GENERATED}.wav"' in text
    assert 'class="badge running">pendiente</span>' in text
    assert f'href="/ui/voices/audio/{PENDING}.wav"' not in text


# ---------------------------------------------------------------------------
# Fragment: pagination
# ---------------------------------------------------------------------------


def test_fragment_paginates_and_links_carry_the_filters() -> None:
    """The pager reports the totals and every link repeats the filter state."""
    client = _client()
    response = client.get(
        "/ui/fragments/voices_table",
        params={"category": "generated", "size": "1", "page": "2"},
    )

    assert response.status_code == 200
    text = response.text
    assert "2 voces ·" in text
    assert "Página 2 / 2" in text
    assert "category=generated" in text and "size=1" in text
    assert "page=1" in text, "the first-page link ships the filters too"


def test_fragment_clamps_crafted_pages_and_sizes() -> None:
    """A junk page or size falls back instead of erroring or escaping."""
    client = _client()

    beyond = client.get(
        "/ui/fragments/voices_table",
        params={"category": "generated", "page": "99", "size": "1"},
    )
    assert beyond.status_code == 200
    assert "Página 2 / 2" in beyond.text, "page 99 clamps to the last page"

    junk = client.get("/ui/fragments/voices_table", params={"page": "abc", "size": "xyz"})
    assert junk.status_code == 200
    assert "página 1 de" in junk.text

    single = client.get(
        "/ui/fragments/voices_table", params={"q": "DarioNeural", "size": "1", "page": "5"}
    )
    assert single.status_code == 200
    assert "página 1 de 1" in single.text, "one match means no pager at all"
    assert "Dario (Neural)" in single.text
