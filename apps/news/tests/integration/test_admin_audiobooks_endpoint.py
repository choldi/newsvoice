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


def _seed_running(workflow_id: str, *, status: str = "GENERATING") -> None:
    """Create a non-terminal audiobook job for the control endpoints."""

    async def _seed() -> None:
        repo = AudioJobsRepository()
        await repo.start_job(workflow_id, collection=JOB_COLLECTION, kind=JOB_KIND)
        await repo.set_status(workflow_id, status, kind=JOB_KIND)

    asyncio.run(_seed())


def test_submit_records_the_requested_chapter_range(
    _audiobook_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The range reaches both the workflow config and the job row."""
    with patch(
        "src.api.services.temporal_admin.TemporalAdminService.start_audiobook",
        new_callable=AsyncMock,
        return_value="audiobook-range1",
    ) as start:
        response = client.post(
            "/api/admin/audiobooks/submit",
            files={"file": ("libro.epub", EPUB_BYTES, "application/epub+zip")},
            data={
                "title": "Mi libro",
                "voice": "",
                "language": "es",
                "engine": "edge",
                "chapter_start": "2",
                "chapter_end": "5",
            },
        )

    assert response.status_code == 200
    config = start.call_args.args[0]
    assert (config.chapter_start, config.chapter_end) == (2, 5)
    job = _job(response.json()["workflow_id"])
    assert (job["chapter_start"], job["chapter_end"]) == (2, 5)


def test_submit_rejects_an_inverted_range(_audiobook_env: Path) -> None:
    """start > end is refused before any upload is stored."""
    response = client.post(
        "/api/admin/audiobooks/submit",
        files={"file": ("libro.epub", EPUB_BYTES, "application/epub+zip")},
        data={"chapter_start": "5", "chapter_end": "2"},
    )

    assert response.status_code == 422
    assert "mayor" in response.json()["detail"]


def test_latest_returns_the_newest_run(_audiobook_env: Path) -> None:
    """The polling panel asks for the last run without knowing its id."""
    from src.api.services.doc_tts import write_job_meta

    _seed_running("audiobook-latest1")
    write_job_meta("audiobook-latest1", {"title": "El más reciente"})

    response = client.get("/api/admin/audiobooks/latest")

    assert response.status_code == 200
    assert response.json()["workflow_id"] == "audiobook-latest1"
    assert response.json()["title"] == "El más reciente"


def test_latest_without_runs_is_404() -> None:
    assert client.get("/api/admin/audiobooks/latest").status_code == 404


def test_pause_and_resume_toggle_the_flag(_audiobook_env: Path) -> None:
    """Pause/resume write the atomic DB flag the worker polls."""
    _seed_running("audiobook-pause1")

    pause = client.post("/api/admin/audiobooks/audiobook-pause1/pause")
    assert pause.status_code == 200
    assert pause.json()["paused"] is True
    assert _job("audiobook-pause1")["paused"] == 1

    resume = client.post("/api/admin/audiobooks/audiobook-pause1/resume")
    assert resume.status_code == 200
    assert resume.json()["paused"] is False
    assert _job("audiobook-pause1")["paused"] == 0


def test_pause_unknown_run_is_404() -> None:
    assert client.post("/api/admin/audiobooks/audiobook-nope/pause").status_code == 404
    assert client.post("/api/admin/audiobooks/audiobook-nope/resume").status_code == 404


def test_pause_a_finished_run_is_409(_audiobook_env: Path) -> None:
    """Changing the flag after the render ended would be misleading."""

    async def _seed() -> None:
        repo = AudioJobsRepository()
        await repo.start_job("audiobook-done2", collection=JOB_COLLECTION, kind=JOB_KIND)
        await repo.finish_job(
            "audiobook-done2", status="FINISHED_OK", audio_path="/tmp/x.m4b", kind=JOB_KIND
        )

    asyncio.run(_seed())

    assert client.post("/api/admin/audiobooks/audiobook-done2/pause").status_code == 409
    assert client.post("/api/admin/audiobooks/audiobook-done2/resume").status_code == 409


def test_progress_endpoint_exposes_the_meta(_audiobook_env: Path) -> None:
    """The progress view mirrors chapters done/total and the chapter label."""
    from src.api.services.doc_tts import write_job_meta

    _seed_running("audiobook-progress1")
    write_job_meta(
        "audiobook-progress1",
        {
            "title": "Mi libro",
            "digest": "abcdefabcdef",
            "chapters_total": 20,
            "chapter_start": 3,
            "chapter_end": 8,
            "progress": {
                "done": 2,
                "total": 6,
                "chapter": 4,
                "chapter_title": "Capítulo 4",
            },
        },
    )

    body = client.get("/api/admin/audiobooks/audiobook-progress1/progress").json()

    assert body["chapters_done"] == 2
    assert body["chapters_total"] == 6
    assert body["chapters_rendered"] == 20
    assert (body["chapter_start"], body["chapter_end"]) == (3, 8)
    assert body["progress"]["chapter_title"] == "Capítulo 4"
    assert body["partial_url"] == "/api/admin/audiobooks/audiobook-progress1/partial"


def test_partial_build_failure_is_409(
    _audiobook_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No finished chapter yet is a conflict, not a server error."""
    from src.api.services.audiobook import AudiobookService

    _seed_running("audiobook-partial1")

    def _boom(self, **kwargs):
        raise RuntimeError("no finished chapters to build a partial audiobook from")

    monkeypatch.setattr(AudiobookService, "build_partial_m4b", _boom)

    response = client.get("/api/admin/audiobooks/audiobook-partial1/partial")

    assert response.status_code == 409
    assert "no finished chapters" in response.json()["detail"]


def test_partial_serves_the_preview_and_records_it(
    _audiobook_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A built preview is downloaded and its path stored on the job row."""
    from src.api.services.audiobook import AudiobookService

    _seed_running("audiobook-partial2")

    def _build(self, **kwargs):
        out = Path(settings.audiobook_dir) / "partial" / "libro_partial.m4b"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(b"partial-m4b")
        assert kwargs["up_to"] == 3
        return out, 3

    monkeypatch.setattr(AudiobookService, "build_partial_m4b", _build)

    response = client.get("/api/admin/audiobooks/audiobook-partial2/partial?up_to=3")

    assert response.status_code == 200
    assert response.headers["content-type"] == "audio/mp4"
    assert response.content == b"partial-m4b"
    assert _job("audiobook-partial2")["partial_path"].endswith("libro_partial.m4b")


def test_partial_unknown_run_is_404() -> None:
    assert client.get("/api/admin/audiobooks/audiobook-nope/partial").status_code == 404
