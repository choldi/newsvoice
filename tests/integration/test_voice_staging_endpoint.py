"""Integration tests for the reference staging endpoint (Phase 7.9)."""

import gzip
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from src.api.main import app
from src.api.routes import voice_staging as staging_routes
from src.api.services import voice_staging
from src.api.services.voice_staging import STAGING_KEY_HEADER
from src.core.audio import CANONICAL_FORMAT, build_wav
from src.core.config import settings

WAV = build_wav(b"\xff\x7f" * 2400, CANONICAL_FORMAT)
KEY = "staging-key"
client = TestClient(app)


@pytest.fixture(autouse=True)
def _staging_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Isolate the staging directory and turn staging on."""
    monkeypatch.setattr(settings, "voices_dir", str(tmp_path / "voices"))
    monkeypatch.setattr(settings, "voice_staging_api_key", KEY)
    monkeypatch.setattr(settings, "voice_staging_url", "http://server.test:8000")
    yield


def _post(
    name: str,
    body: bytes = gzip.compress(WAV),
    key: str | None = KEY,
):
    headers = {STAGING_KEY_HEADER: key} if key is not None else {}
    return client.post(f"/api/internal/voices/staged/{name}", content=body, headers=headers)


def test_stages_a_reference_for_the_store_activity() -> None:
    response = _post("anchor_neutral_m")

    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "name": "anchor_neutral_m",
        "bytes": len(gzip.compress(WAV)),
    }
    assert voice_staging.read_staged("anchor_neutral_m") == WAV


@pytest.mark.parametrize("key", [None, "", "wrong"])
def test_rejects_a_missing_or_wrong_key(key: str | None) -> None:
    response = _post("anchor_neutral_m", key=key)

    assert response.status_code == 401
    assert response.json()["detail"] == "invalid staging key"
    assert not voice_staging.staged_path("anchor_neutral_m").exists()


def test_fails_closed_while_the_key_is_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "voice_staging_api_key", "")

    response = _post("anchor_neutral_m")

    assert response.status_code == 503
    assert response.json()["detail"] == "voice staging is not configured"
    assert not voice_staging.staged_path("anchor_neutral_m").exists()


@pytest.mark.parametrize("name", ["Bad-Name", "../etc/passwd", "a" * 65])
def test_rejects_an_invalid_profile_name(name: str) -> None:
    response = _post(name)

    assert response.status_code in (404, 422)
    assert not voice_staging.staged_path("anchor_neutral_m").exists()


def test_rejects_an_oversized_payload(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(staging_routes, "MAX_REQUEST_BYTES", 16)

    response = _post("anchor_neutral_m", body=b"x" * 32)

    assert response.status_code == 413
    assert not voice_staging.staged_path("anchor_neutral_m").exists()


def test_rejects_an_empty_body() -> None:
    response = _post("anchor_neutral_m", body=b"")

    assert response.status_code == 422
    assert not voice_staging.staged_path("anchor_neutral_m").exists()
