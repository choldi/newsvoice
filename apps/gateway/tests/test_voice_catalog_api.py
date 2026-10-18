"""Tests del catálogo de voces que el gateway posee (P6)."""

from __future__ import annotations

import io
import json
import wave
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from tts_contract import VoiceCatalogError, VoiceProfile, load_catalog_profiles

from tts_api.api.v1.catalog import get_catalog_service
from tts_api.main import app
from tts_api.services.voice_catalog_service import (
    VoiceCatalogConflictError,
    VoiceCatalogNotFoundError,
    VoiceCatalogService,
    VoiceCatalogValidationError,
)

GATEWAY_ROOT = Path(__file__).resolve().parents[1]
COMMITTED_CATALOG = GATEWAY_ROOT / "voice_profiles.json"

REF_TEXT = "Hola, soy tu profe de ingles de siempre, y este es el texto de prueba. " * 3

PROFILE = {
    "name": "mi_voz_es",
    "label": "Mi voz",
    "language": "es",
    "instruct": "Femenina, 30s, tono cercano y agil, claridad de locucion",
    "ref_text": REF_TEXT,
}


def _wav(seconds: float = 0.5, sample_rate: int = 24000) -> bytes:
    """Build a mono 16-bit PCM WAV of the requested length."""
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as writer:
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(sample_rate)
        writer.writeframes(b"\x00\x00" * int(seconds * sample_rate))
    return buffer.getvalue()


def _write_catalog(path: Path, profiles: list[dict]) -> Path:
    """Write a committed catalog file with the given profiles."""
    path.write_text(json.dumps({"version": 2, "profiles": profiles}), encoding="utf-8")
    return path


def _service(tmp_path: Path, profiles: list[dict] | None = None) -> VoiceCatalogService:
    """Return a service bound to an isolated catalog under ``tmp_path``."""
    profiles_file = _write_catalog(tmp_path / "voice_profiles.json", profiles or [PROFILE])
    return VoiceCatalogService(
        profiles_file=profiles_file,
        catalog_dir=tmp_path / "catalog",
    )


@pytest.fixture
def service(tmp_path: Path) -> VoiceCatalogService:
    """Isolated catalog service with one committed profile."""
    return _service(tmp_path)


@pytest.fixture
def client(service: VoiceCatalogService):
    """Test client whose catalog dependency is bound to ``service``."""
    app.dependency_overrides[get_catalog_service] = lambda: service
    yield TestClient(app)
    app.dependency_overrides.pop(get_catalog_service, None)


# --- committed catalog -------------------------------------------------------


def test_the_committed_catalog_still_parses() -> None:
    """The catalog shipped with the gateway keeps every profile valid."""
    profiles = load_catalog_profiles(COMMITTED_CATALOG)

    assert len(profiles) == 32
    assert len({profile.name for profile in profiles}) == 32


# --- service -----------------------------------------------------------------


def test_catalog_reports_every_profile_as_pending(service: VoiceCatalogService) -> None:
    payload = service.catalog()

    assert payload.status == "ok"
    assert [profile.name for profile in payload.profiles] == ["mi_voz_es"]
    assert payload.missing == ["mi_voz_es"]
    assert payload.generated == []
    assert payload.custom == []
    assert payload.statuses["mi_voz_es"] == "pendiente"
    assert payload.manifest == {}


def test_store_reference_measures_and_records_the_audio(service: VoiceCatalogService) -> None:
    entry = service.store_reference(
        "mi_voz_es",
        _wav(seconds=0.5, sample_rate=24000),
        instruct=PROFILE["instruct"],
        ref_text=PROFILE["ref_text"],
        design_model="Qwen/Qwen3-TTS-12Hz-1.7B-VoiceDesign",
    )

    assert entry.file == "refs/mi_voz_es.wav"
    assert entry.sample_rate == 24000
    assert entry.duration_seconds == pytest.approx(0.5)
    assert len(entry.sha256) == 64

    payload = service.catalog()
    assert payload.generated == ["mi_voz_es"]
    assert payload.missing == []
    assert payload.statuses["mi_voz_es"] == "generada"
    assert service.read_reference("mi_voz_es") == _wav(seconds=0.5, sample_rate=24000)


def test_catalog_reports_a_reference_whose_profile_drifted(
    service: VoiceCatalogService,
) -> None:
    service.store_reference(
        "mi_voz_es",
        _wav(),
        instruct=PROFILE["instruct"],
        ref_text=PROFILE["ref_text"],
    )

    drifted = dict(PROFILE, instruct="Otra descripción de persona bastante más larga")
    _write_catalog(service.profiles_file, [drifted])

    payload = service.catalog()
    assert payload.statuses["mi_voz_es"] == "desactualizada"
    assert payload.missing == ["mi_voz_es"]
    assert payload.generated == []


def test_store_reference_rejects_an_unknown_profile(service: VoiceCatalogService) -> None:
    with pytest.raises(VoiceCatalogNotFoundError, match="unknown voice profile"):
        service.store_reference(
            "no_existe",
            _wav(),
            instruct=PROFILE["instruct"],
            ref_text=PROFILE["ref_text"],
        )


def test_store_reference_rejects_a_non_wav_payload(service: VoiceCatalogService) -> None:
    with pytest.raises(VoiceCatalogValidationError, match="not a readable WAV"):
        service.store_reference(
            "mi_voz_es",
            b"not audio at all",
            instruct=PROFILE["instruct"],
            ref_text=PROFILE["ref_text"],
        )


def test_store_reference_rejects_an_oversized_duration(
    service: VoiceCatalogService,
) -> None:
    """A reference longer than ``MAX_REFERENCE_DURATION_SEC`` is refused."""
    with pytest.raises(VoiceCatalogValidationError, match="limit"):
        service.store_reference(
            "mi_voz_es",
            _wav(seconds=31.0),
            instruct=PROFILE["instruct"],
            ref_text=PROFILE["ref_text"],
        )


def test_store_reference_rejects_a_name_that_is_not_a_slug(
    service: VoiceCatalogService,
) -> None:
    with pytest.raises(VoiceCatalogValidationError, match="invalid voice name"):
        service.store_reference(
            "../etc/passwd",
            _wav(),
            instruct=PROFILE["instruct"],
            ref_text=PROFILE["ref_text"],
        )


def test_save_custom_appends_and_rejects_duplicates(service: VoiceCatalogService) -> None:
    custom = {**PROFILE, "name": "voz_custom"}

    service.save_custom(VoiceProfile.model_validate(custom))

    assert [profile.name for profile in service.custom_profiles()] == ["voz_custom"]
    assert service.catalog().custom == ["voz_custom"]

    with pytest.raises(VoiceCatalogConflictError, match="already exists"):
        service.save_custom(VoiceProfile.model_validate(custom))
    with pytest.raises(VoiceCatalogConflictError, match="already in the catalog"):
        service.save_custom(VoiceProfile.model_validate(PROFILE))


def test_reference_path_refuses_names_outside_the_catalog(service: VoiceCatalogService) -> None:
    assert service.reference_path("../../etc/passwd.wav") is None
    assert service.reference_path("no_existe") is None
    with pytest.raises(VoiceCatalogNotFoundError, match="not found"):
        service.read_reference("no_existe")


# --- endpoints ---------------------------------------------------------------


def test_get_catalog_serves_the_payload(client: TestClient) -> None:
    response = client.get("/api/v1/voices/catalog")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert [profile["name"] for profile in body["profiles"]] == ["mi_voz_es"]
    assert body["missing"] == ["mi_voz_es"]
    assert body["statuses"] == {"mi_voz_es": "pendiente"}


def test_reference_round_trip_over_http(client: TestClient) -> None:
    wav = _wav()

    stored = client.post(
        "/api/v1/voices/catalog/refs/mi_voz_es",
        files={"file": ("ref.wav", wav, "audio/wav")},
        data={"instruct": PROFILE["instruct"], "ref_text": PROFILE["ref_text"]},
    )

    assert stored.status_code == 201
    assert stored.json()["file"] == "refs/mi_voz_es.wav"

    fetched = client.get("/api/v1/voices/catalog/refs/mi_voz_es.wav")
    assert fetched.status_code == 200
    assert fetched.headers["content-type"].startswith("audio/wav")
    assert fetched.content == wav


def test_missing_reference_and_unknown_profile_answer_404(client: TestClient) -> None:

    assert client.get("/api/v1/voices/catalog/refs/mi_voz_es").status_code == 404
    assert client.get("/api/v1/voices/catalog/refs/%2e%2e%2fetc").status_code == 404
    assert (
        client.post(
            "/api/v1/voices/catalog/refs/no_existe",
            files={"file": ("ref.wav", _wav(), "audio/wav")},
            data={"instruct": PROFILE["instruct"], "ref_text": PROFILE["ref_text"]},
        ).status_code
        == 404
    )


def test_invalid_reference_audio_answers_422(client: TestClient) -> None:
    response = client.post(
        "/api/v1/voices/catalog/refs/mi_voz_es",
        files={"file": ("ref.wav", b"not audio", "audio/wav")},
        data={"instruct": PROFILE["instruct"], "ref_text": PROFILE["ref_text"]},
    )

    assert response.status_code == 422
    assert "not a readable WAV" in response.json()["detail"]


def test_create_custom_voice_over_http(client: TestClient) -> None:

    created = client.post(
        "/api/v1/voices/catalog/custom",
        json={**PROFILE, "name": "voz_custom"},
    )
    assert created.status_code == 201
    assert created.json()["name"] == "voz_custom"

    conflict = client.post(
        "/api/v1/voices/catalog/custom",
        json={**PROFILE, "name": "voz_custom"},
    )
    assert conflict.status_code == 409
    assert "already exists" in conflict.json()["detail"]

    body = client.get("/api/v1/voices/catalog").json()
    assert "voz_custom" in body["custom"]


# --- archivos de runtime corruptos -------------------------------------------


def test_corrupt_custom_profiles_file_is_reported(service: VoiceCatalogService) -> None:
    service.custom_profiles_path.parent.mkdir(parents=True, exist_ok=True)
    service.custom_profiles_path.write_text("{not json", encoding="utf-8")

    with pytest.raises(VoiceCatalogError, match="not readable JSON"):
        service.custom_profiles()


def test_custom_profiles_file_without_a_profiles_list(service: VoiceCatalogService) -> None:
    service.custom_profiles_path.parent.mkdir(parents=True, exist_ok=True)
    service.custom_profiles_path.write_text(json.dumps({"voices": []}), encoding="utf-8")

    with pytest.raises(VoiceCatalogError, match="needs a 'profiles' list"):
        service.custom_profiles()


def test_custom_profiles_file_rejects_an_invalid_profile(service: VoiceCatalogService) -> None:
    service.custom_profiles_path.parent.mkdir(parents=True, exist_ok=True)
    service.custom_profiles_path.write_text(
        json.dumps({"profiles": [{**PROFILE, "language": "spanish"}]}), encoding="utf-8"
    )

    with pytest.raises(VoiceCatalogError, match="is invalid"):
        service.custom_profiles()


def test_custom_profiles_file_rejects_duplicate_names(service: VoiceCatalogService) -> None:
    service.custom_profiles_path.parent.mkdir(parents=True, exist_ok=True)
    service.custom_profiles_path.write_text(
        json.dumps({"profiles": [PROFILE, PROFILE]}), encoding="utf-8"
    )

    with pytest.raises(VoiceCatalogError, match="duplicate custom voice profile names"):
        service.custom_profiles()


def test_save_custom_never_clobbers_a_corrupt_file(service: VoiceCatalogService) -> None:
    service.custom_profiles_path.parent.mkdir(parents=True, exist_ok=True)
    service.custom_profiles_path.write_text("{not json", encoding="utf-8")

    with pytest.raises(VoiceCatalogError, match="not readable JSON"):
        service.save_custom(VoiceProfile.model_validate({**PROFILE, "name": "voz_custom"}))

    assert service.custom_profiles_path.read_text(encoding="utf-8") == "{not json"


def test_corrupt_manifest_is_reported(service: VoiceCatalogService) -> None:
    service.manifest_path.parent.mkdir(parents=True, exist_ok=True)
    service.manifest_path.write_text("{not json", encoding="utf-8")

    with pytest.raises(VoiceCatalogError, match="manifest is not readable JSON"):
        service.manifest()


def test_manifest_that_is_not_an_object_is_reported(service: VoiceCatalogService) -> None:
    service.manifest_path.parent.mkdir(parents=True, exist_ok=True)
    service.manifest_path.write_text("[]", encoding="utf-8")

    with pytest.raises(VoiceCatalogError, match="must be a JSON object"):
        service.manifest()


def test_get_catalog_answers_500_when_the_custom_file_is_unreadable(
    client: TestClient, service: VoiceCatalogService
) -> None:
    service.custom_profiles_path.parent.mkdir(parents=True, exist_ok=True)
    service.custom_profiles_path.write_text("{not json", encoding="utf-8")

    response = client.get("/api/v1/voices/catalog")

    assert response.status_code == 500
    assert "not readable JSON" in response.json()["detail"]
