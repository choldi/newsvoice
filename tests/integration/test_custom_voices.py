"""Integration tests for custom voice creation and audio serving (Phase 9.3)."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock
from urllib.parse import unquote

import pytest
from fastapi.testclient import TestClient

from src.api.main import app
from src.api.models.voices import VoiceManifestEntry, VoiceProfile
from src.core.config import settings
from src.core.voice_catalog import (
    load_custom_profiles,
    load_profiles,
    save_custom_profile,
    sha256_text,
    write_manifest,
)

TOKEN = "secret-admin-token"
REF_TEXT = "Hola, soy tu profe de ingles de siempre. " * 4
INSTRUCT = "Femenina, 30s, tono cercano y agil, claridad de locucion"


@pytest.fixture(autouse=True)
def _voices_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Isolate the voices directory and auth, and keep the gateway out."""
    voices = tmp_path / "voices"
    voices.mkdir()
    monkeypatch.setattr(settings, "voices_dir", str(voices))
    monkeypatch.setattr(settings, "admin_api_token", TOKEN)
    monkeypatch.setattr(settings, "collections_file", str(tmp_path / "collections.toml"))
    from src.api.services.voices import VoicesService

    monkeypatch.setattr(VoicesService, "list_languages", AsyncMock(return_value=["es"]))
    monkeypatch.setattr(VoicesService, "list_stock", AsyncMock(return_value=[]))
    monkeypatch.setattr(VoicesService, "list_cloned", AsyncMock(return_value=[]))
    monkeypatch.setattr(
        "src.api.services.voice_catalog_admin.start_generate",
        AsyncMock(return_value="voice-catalog-test"),
    )
    return voices


def _client() -> TestClient:
    """Build a client authenticated through the login endpoint."""
    client = TestClient(app)
    response = client.post("/ui/login", data={"token": TOKEN}, follow_redirects=False)
    assert response.status_code == 302
    return client


def _api_client() -> TestClient:
    """Build a client authenticated through the admin token header."""
    return TestClient(app, headers={"X-Admin-Token": TOKEN})


def _payload(**overrides: object) -> dict[str, object]:
    """Build a valid REST payload for a custom voice."""
    payload: dict[str, object] = {
        "name": "mi_voz_es",
        "label": "Mi voz",
        "language": "es",
        "instruct": INSTRUCT,
        "ref_text": REF_TEXT,
        "generate": True,
    }
    payload.update(overrides)
    return payload


def _profile(name: str = "mi_voz_es") -> VoiceProfile:
    """Build a valid profile for direct saves in listing/audio tests."""
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


# --- REST ---------------------------------------------------------------------


def test_rest_create_custom_voice_saves_and_starts() -> None:
    response = _api_client().post("/api/admin/voices/custom", json=_payload())

    assert response.status_code == 201
    body = response.json()
    assert body["status"] == "ok"
    assert body["profile"]["name"] == "mi_voz_es"
    assert body["workflow_id"] == "voice-catalog-test"
    assert body["start_error"] == ""
    assert [profile.name for profile in load_custom_profiles()] == ["mi_voz_es"]


def test_rest_create_custom_voice_conflicts_on_a_duplicate_name() -> None:
    client = _api_client()
    assert client.post("/api/admin/voices/custom", json=_payload()).status_code == 201

    response = client.post("/api/admin/voices/custom", json=_payload())

    assert response.status_code == 409
    assert "already exists" in response.json()["detail"]


def test_rest_create_custom_voice_rejects_a_short_reference() -> None:
    response = _api_client().post("/api/admin/voices/custom", json=_payload(ref_text="too short"))

    assert response.status_code == 422
    assert load_custom_profiles() == []


def test_rest_create_custom_voice_rejects_an_unsafe_name() -> None:
    response = _api_client().post("/api/admin/voices/custom", json=_payload(name="Bad Name!"))

    assert response.status_code == 422
    assert load_custom_profiles() == []


def test_catalog_status_includes_the_custom_voice() -> None:
    client = _api_client()
    assert client.post("/api/admin/voices/custom", json=_payload()).status_code == 201

    body = client.get("/api/admin/voices/catalog").json()

    assert len(body["profiles"]) == len(load_profiles())
    assert "mi_voz_es" in body["missing"]
    assert body["custom"] == ["mi_voz_es"]
    assert body["generated"] == []


# --- GUI ----------------------------------------------------------------------


def test_ui_voices_page_includes_the_custom_cards() -> None:
    response = _client().get("/ui/voices")

    assert response.status_code == 200
    assert 'action="/ui/voices/custom"' in response.text
    assert 'id="voices-generated"' in response.text
    assert 'hx-trigger="load, every 15s"' in response.text


def test_ui_create_custom_voice_flashes_success(monkeypatch: pytest.MonkeyPatch) -> None:
    start = AsyncMock(return_value="voice-catalog-ui")
    monkeypatch.setattr("src.api.services.voice_catalog_admin.start_generate", start)
    response = _client().post(
        "/ui/voices/custom",
        data={
            "name": "mi_voz_es",
            "label": "Mi voz",
            "language": "es",
            "instruct": INSTRUCT,
            "ref_text": REF_TEXT,
            "role_hint": "anchor",
            "generate": "on",
        },
        follow_redirects=False,
    )

    assert response.status_code == 302
    location = unquote(response.headers["location"])
    assert "Voz 'mi_voz_es' creada; diseño en cola" in location
    assert "level=ok" in location
    assert [profile.name for profile in load_custom_profiles()] == ["mi_voz_es"]
    start.assert_awaited_once_with(names=["mi_voz_es"], upload=False)


def test_ui_create_custom_voice_without_generate_skips_the_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    start = AsyncMock(return_value="voice-catalog-ui")
    monkeypatch.setattr("src.api.services.voice_catalog_admin.start_generate", start)
    response = _client().post(
        "/ui/voices/custom",
        data={"name": "mi_voz_es", "language": "es", "instruct": INSTRUCT, "ref_text": REF_TEXT},
        follow_redirects=False,
    )

    assert response.status_code == 302
    location = unquote(response.headers["location"])
    assert "creada (pendiente de generar)" in location
    start.assert_not_awaited()


def test_ui_create_custom_voice_flashes_a_validation_error() -> None:
    response = _client().post(
        "/ui/voices/custom",
        data={"name": "mi_voz_es", "language": "es", "instruct": "corto", "ref_text": REF_TEXT},
        follow_redirects=False,
    )

    assert response.status_code == 302
    location = unquote(response.headers["location"])
    assert "Voz no válida" in location
    assert "level=error" in location
    assert load_custom_profiles() == []


def test_ui_create_custom_voice_flashes_a_duplicate_name() -> None:
    save_custom_profile(_profile())
    response = _client().post(
        "/ui/voices/custom",
        data={
            "name": "mi_voz_es",
            "language": "es",
            "instruct": INSTRUCT,
            "ref_text": REF_TEXT,
            "generate": "on",
        },
        follow_redirects=False,
    )

    assert response.status_code == 302
    location = unquote(response.headers["location"])
    assert "already exists" in location
    assert "level=error" in location


def test_ui_create_custom_voice_requires_a_reference_text() -> None:
    response = _client().post(
        "/ui/voices/custom",
        data={"name": "mi_voz_es", "language": "es", "instruct": INSTRUCT, "ref_text": "\n \n"},
        follow_redirects=False,
    )

    assert response.status_code == 302
    location = unquote(response.headers["location"])
    assert "El texto de referencia es obligatorio" in location
    assert load_custom_profiles() == []


# --- Generated-voices fragment ------------------------------------------------


def test_fragment_reports_generated_pending_and_stale(tmp_path: Path) -> None:
    voices = tmp_path / "voices"
    for name in ("generada_es", "pendiente_es", "desactualizada_es"):
        save_custom_profile(_profile(name))
    write_manifest(
        {
            "generada_es": _entry(_profile("generada_es"), "refs/generada_es.wav"),
            "desactualizada_es": _entry(
                _profile("desactualizada_es"), "refs/desactualizada_es.wav"
            ),
        },
        voices / "manifest.json",
    )
    wav = voices / "refs" / "generada_es.wav"
    wav.parent.mkdir(parents=True, exist_ok=True)
    wav.write_bytes(b"RIFF....WAVE")
    stale = _entry(_profile("desactualizada_es"), "refs/desactualizada_es.wav")
    stale.instruct_sha256 = "0" * 64
    write_manifest(
        {
            "generada_es": _entry(_profile("generada_es"), "refs/generada_es.wav"),
            "desactualizada_es": stale,
        },
        voices / "manifest.json",
    )

    response = _client().get("/ui/fragments/voices_generated")

    assert response.status_code == 200
    text = response.text
    assert text.count("<audio") == 1
    assert "/ui/voices/audio/generada_es.wav" in text
    assert 'class="badge ok">generada</span>' in text
    assert 'class="badge running">pendiente</span>' in text
    assert 'class="badge terminated">desactualizada</span>' in text
    assert "personalizada" in text


def test_fragment_degrades_when_the_catalog_is_unreadable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def broken() -> list[dict[str, object]]:
        from src.core.voice_catalog import VoiceCatalogError

        raise VoiceCatalogError("voice catalog is not readable JSON: boom")

    monkeypatch.setattr("src.api.services.voice_catalog_admin.generated_voices", broken)

    response = _client().get("/ui/fragments/voices_generated")

    assert response.status_code == 200
    assert "No se pudo leer el catálogo" in response.text


# --- Audio endpoint -----------------------------------------------------------


def test_voice_audio_serves_the_reference_wav(tmp_path: Path) -> None:
    save_custom_profile(_profile("mi_voz_es"))
    wav = tmp_path / "voices" / "refs" / "mi_voz_es.wav"
    wav.parent.mkdir(parents=True, exist_ok=True)
    wav.write_bytes(b"RIFF....WAVE")

    response = _client().get("/ui/voices/audio/mi_voz_es.wav")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("audio/wav")
    assert response.content == b"RIFF....WAVE"


def test_voice_audio_prefers_the_manifest_entry(tmp_path: Path) -> None:
    profile = _profile("mi_voz_es")
    save_custom_profile(profile)
    write_manifest(
        {"mi_voz_es": _entry(profile, "refs/otra_ruta.wav")},
        tmp_path / "voices" / "manifest.json",
    )
    wav = tmp_path / "voices" / "refs" / "otra_ruta.wav"
    wav.parent.mkdir(parents=True, exist_ok=True)
    wav.write_bytes(b"RIFF-MANIFEST")

    response = _client().get("/ui/voices/audio/mi_voz_es.wav")

    assert response.status_code == 200
    assert response.content == b"RIFF-MANIFEST"


def test_voice_audio_404s_on_unknown_unsafe_and_missing_names(
    tmp_path: Path,
) -> None:
    client = _client()
    assert client.get("/ui/voices/audio/no_existe.wav").status_code == 404
    assert client.get("/ui/voices/audio/Bad%20Name.wav").status_code == 404
    assert client.get("/ui/voices/audio/..wav").status_code == 404

    save_custom_profile(_profile("mi_voz_es"))
    assert client.get("/ui/voices/audio/mi_voz_es.wav").status_code == 404


def test_voice_audio_refuses_a_manifest_entry_outside_the_voices_dir(
    tmp_path: Path,
) -> None:
    profile = _profile("mi_voz_es")
    save_custom_profile(profile)
    outside = tmp_path / "secret.wav"
    outside.write_bytes(b"TOP SECRET")
    write_manifest(
        {"mi_voz_es": _entry(profile, "../secret.wav")},
        tmp_path / "voices" / "manifest.json",
    )

    response = _client().get("/ui/voices/audio/mi_voz_es.wav")

    assert response.status_code == 404
    assert b"TOP SECRET" not in response.content
