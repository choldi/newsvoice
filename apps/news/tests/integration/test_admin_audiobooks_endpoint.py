"""Integration tests for the admin audiobook M4B endpoints."""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from src.api.db.database import AudioJobsRepository
from src.api.main import app
from src.core.config import settings

AUTH_HEADERS = {"X-Admin-Token": "test-token"}
client = TestClient(app, headers=AUTH_HEADERS)
anon_client = TestClient(app)

JOB_KIND = "audiobook"
JOB_COLLECTION = "audiobooks"
EPUB_BYTES = b"PK-fake-epub"


@pytest.fixture(autouse=True)
def _audiobook_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Isolate audiobook and shared document storage for every test."""
    monkeypatch.setattr(settings, "doc_tts_dir", str(tmp_path / "doc_tts"))
    monkeypatch.setattr(settings, "audiobook_dir", str(tmp_path / "audiobooks"))
    monkeypatch.setattr(settings, "admin_api_token", "test-token")
    return tmp_path


def _job(workflow_id: str) -> dict:
    job = asyncio.run(AudioJobsRepository().get_job(workflow_id, kind=JOB_KIND))
    assert job is not None, f"job {workflow_id} not found"
    return job


def test_submit_requires_the_admin_token() -> None:
    """Without the token the endpoint answers 401."""
    response = anon_client.post(
        "/api/admin/audiobooks/submit",
        files={"file": ("libro.epub", EPUB_BYTES, "application/epub+zip")},
    )
    assert response.status_code == 401


def test_submit_uses_the_fixed_path_and_records_the_job(_audiobook_env: Path) -> None:
    """A valid upload returns /api/admin/... (not /api/api/...) and starts it."""
    with patch(
        "src.api.services.temporal_admin.TemporalAdminService.start_audiobook",
        new_callable=AsyncMock,
        return_value="audiobook-1",
    ) as start:
        response = client.post(
            "/api/admin/audiobooks/submit",
            files={"file": ("libro.epub", EPUB_BYTES, "application/epub+zip")},
            data={"title": "Mi libro", "voice": "", "language": "es", "engine": "edge"},
        )

    assert response.status_code == 200
    body = response.json()
    workflow_id = body["workflow_id"]
    assert workflow_id.startswith("audiobook-")
    assert body["status_url"] == f"/api/admin/audiobooks/{workflow_id}"
    assert "/api/api/" not in body["status_url"]

    config = start.call_args.args[0]
    assert start.call_args.args[1] == workflow_id
    assert config.filename == "libro.epub"
    assert config.title == "Mi libro"
    assert Path(config.source_path).is_file()
    assert Path(config.source_path).parent.name == "uploads"

    from src.api.services.doc_tts import read_job_meta

    meta = read_job_meta(workflow_id)
    assert meta["title"] == "Mi libro"
    assert meta["filename"] == "libro.epub"
    assert meta["kind"] == "epub"

    job = _job(workflow_id)
    assert job["status"] == "STARTING"
    assert job["collection"] == JOB_COLLECTION
    assert job["kind"] == JOB_KIND


def test_status_unknown_job_is_404() -> None:
    """A missing run id answers 404 instead of an empty 200 body."""
    assert client.get("/api/admin/audiobooks/audiobook-nope").status_code == 404


def test_submit_rejects_modal_only_engines(_audiobook_env: Path) -> None:
    """zonos2/voxcpm2 render on Modal only, so a book-length run refuses them."""
    for engine in ("zonos2", "voxcpm2"):
        response = client.post(
            "/api/admin/audiobooks/submit",
            files={"file": ("libro.epub", EPUB_BYTES, "application/epub+zip")},
            data={"title": "Mi libro", "voice": "", "language": "es", "engine": engine},
        )

        assert response.status_code == 422, engine
        assert "cannot run on Modal" in response.json()["detail"]


def test_status_and_download_use_the_fixed_path(_audiobook_env: Path) -> None:
    """A finished job exposes the audio and serves the M4B download."""
    from src.api.services.doc_tts import write_job_meta

    workflow_id = "audiobook-done1"
    audio = Path(settings.audiobook_dir) / "audio" / f"{workflow_id}.m4b"
    audio.parent.mkdir(parents=True, exist_ok=True)
    audio.write_bytes(b"fake-m4b")
    write_job_meta(
        workflow_id,
        {"title": "Mi libro", "filename": "libro.epub", "kind": "epub", "mode": "edge"},
    )

    async def _seed() -> None:
        repo = AudioJobsRepository()
        await repo.start_job(workflow_id, collection=JOB_COLLECTION, kind=JOB_KIND)
        await repo.finish_job(
            workflow_id,
            status="FINISHED_OK",
            audio_path=str(audio),
            kind=JOB_KIND,
        )

    asyncio.run(_seed())

    body = client.get(f"/api/admin/audiobooks/{workflow_id}").json()
    assert body["title"] == "Mi libro"
    assert body["audio_path"] == str(audio)
    assert body["download_url"] == f"/api/admin/audiobooks/{workflow_id}/download"

    download = client.get(body["download_url"])
    assert download.status_code == 200
    assert download.headers["content-type"] == "audio/mp4"
    assert download.content == b"fake-m4b"


def test_download_before_render_is_404() -> None:
    """A known job without audio cannot be downloaded yet."""
    assert client.get("/api/admin/audiobooks/audiobook-pending/download").status_code == 404
