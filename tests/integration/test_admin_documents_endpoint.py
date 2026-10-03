"""Integration tests for the Phase 7.11 document TTS endpoints."""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from src.api.db.database import AudioJobsRepository
from src.api.main import app
from src.api.services.signed_links import build_signed_path
from src.core.config import settings

AUTH_HEADERS = {"X-Admin-Token": "test-token"}
client = TestClient(app, headers=AUTH_HEADERS)
anon_client = TestClient(app)

JOB_KIND = "doc_tts"
JOB_COLLECTION = "documents"
PLAIN_TXT = b"Primera linea de la noticia.\n\nSegunda linea con mas detalle para el resumen.\n"


@pytest.fixture(autouse=True)
def _doc_tts_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Isolate storage and enable auth + signed links for every test."""
    monkeypatch.setattr(settings, "doc_tts_dir", str(tmp_path / "doc_tts"))
    monkeypatch.setattr(settings, "admin_api_token", "test-token")
    monkeypatch.setattr(settings, "doc_tts_link_secret", "unit-test-secret")
    monkeypatch.setattr(settings, "doc_tts_enabled", True)
    return tmp_path / "doc_tts"


def _upload(
    filename: str,
    content: bytes,
    *,
    client_: TestClient | None = None,
    **form: str,
) -> object:
    """POST a document to the submit endpoint."""
    target = client_ or client
    return target.post(
        "/api/admin/documents/tts",
        files={"file": (filename, content, "application/octet-stream")},
        data=form or None,
    )


# ---------------------------------------------------------------------------
# Submit
# ---------------------------------------------------------------------------


def test_submit_requires_the_admin_token(_doc_tts_env: Path) -> None:
    """Without the token the endpoint answers 401."""
    response = _upload("nota.txt", PLAIN_TXT, client_=anon_client)
    assert response.status_code == 401


def test_submit_starts_the_workflow_and_records_the_job(_doc_tts_env: Path) -> None:
    """A valid upload answers 202, stores the file and opens the job row."""
    with patch(
        "src.api.services.temporal_admin.TemporalAdminService.start_document_tts",
        new_callable=AsyncMock,
    ) as start:
        response = _upload(
            "clase_de_espanol.txt",
            PLAIN_TXT,
            title="Clase de espanol",
            language="es",
        )

    assert response.status_code == 202
    body = response.json()
    assert body["status"] == "ok"
    assert body["workflow_id"].startswith("doc-tts-")
    assert body["title"] == "Clase de espanol"
    assert body["filename"] == "clase_de_espanol.txt"
    assert body["status_url"] == f"/api/admin/documents/tts/{body['workflow_id']}"

    # The workflow started on the dedicated queue with the stored upload path.
    config = start.call_args.args[0]
    assert start.call_args.args[1] == body["workflow_id"]
    assert config.filename == "clase_de_espanol.txt"
    assert config.title == "Clase de espanol"
    assert config.language == "es"
    assert Path(config.source_path).is_file()
    assert Path(config.source_path).parent.name == "uploads"

    # Metadata and job row exist before the worker runs.
    from src.api.services.doc_tts import read_job_meta

    meta = read_job_meta(body["workflow_id"])
    assert meta["title"] == "Clase de espanol"
    assert meta["filename"] == "clase_de_espanol.txt"
    job = _job(body["workflow_id"])
    assert job["status"] == "STARTING"
    assert job["collection"] == JOB_COLLECTION
    assert job["kind"] == JOB_KIND


def test_submit_rejects_unsupported_types(_doc_tts_env: Path) -> None:
    """A .docx never reaches Temporal."""
    response = _upload("informe.docx", b"not really a docx")
    assert response.status_code == 415
    assert "unsupported file type" in response.json()["detail"]


def test_submit_rejects_oversized_files(
    _doc_tts_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Files beyond doc_tts_max_file_bytes are refused with 413."""
    monkeypatch.setattr(settings, "doc_tts_max_file_bytes", 1024)
    response = _upload("grande.txt", b"x" * 2048)
    assert response.status_code == 413


def test_submit_rejects_empty_files(_doc_tts_env: Path) -> None:
    """An empty upload is a 422."""
    response = _upload("vacio.txt", b"")
    assert response.status_code == 422


def test_submit_is_disabled_by_the_kill_switch(_doc_tts_env: Path, monkeypatch) -> None:
    """DOC_TTS_ENABLED=false closes the endpoint."""
    monkeypatch.setattr(settings, "doc_tts_enabled", False)
    response = _upload("nota.txt", PLAIN_TXT)
    assert response.status_code == 403
    assert "disabled" in response.json()["detail"]


def test_submit_requires_a_link_secret(_doc_tts_env: Path, monkeypatch) -> None:
    """Without DOC_TTS_LINK_SECRET the job would be unusable: fail fast."""
    monkeypatch.setattr(settings, "doc_tts_link_secret", "")
    response = _upload("nota.txt", PLAIN_TXT)
    assert response.status_code == 503
    assert "DOC_TTS_LINK_SECRET" in response.json()["detail"]


def test_submit_temporal_failure_is_502_and_marks_the_job(
    _doc_tts_env: Path,
) -> None:
    """A Temporal outage leaves an explained job row and no orphan upload."""
    with patch(
        "src.api.services.temporal_admin.TemporalAdminService.start_document_tts",
        new_callable=AsyncMock,
        side_effect=RuntimeError("temporal unreachable"),
    ):
        response = _upload("nota.txt", PLAIN_TXT)

    assert response.status_code == 502
    uploads = list((_doc_tts_env / "uploads").glob("*.txt"))
    assert uploads == [], "the upload is removed when no workflow will read it"


# ---------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------


def test_status_unknown_job_is_404(_doc_tts_env: Path) -> None:
    """Foreign or missing ids answer 404."""
    assert client.get("/api/admin/documents/tts/doc-tts-nope").status_code == 404
    assert client.get("/api/admin/documents/tts/../../etc").status_code == 404


def test_status_reports_generation_progress(_doc_tts_env: Path) -> None:
    """A running job answers GENERATING without an audio link."""
    from src.api.services.doc_tts import write_job_meta

    await_ = _start_job("doc-tts-run1")
    assert await_["status"] == "STARTING"
    write_job_meta("doc-tts-run1", {"title": "Libro", "kind": "pdf", "char_count": 999})
    asyncio.run(_job_repo().set_generating("doc-tts-run1", kind=JOB_KIND))

    body = client.get("/api/admin/documents/tts/doc-tts-run1").json()

    assert body["job_status"] == "GENERATING"
    assert body["title"] == "Libro"
    assert body["char_count"] == 999
    assert body["audio_url"] is None


def test_status_returns_a_signed_link_when_finished(_doc_tts_env: Path) -> None:
    """A finished job answers FINISHED_OK with a signed URL."""
    from src.api.services.doc_tts import write_job_meta

    audio_path = _finished_job("doc-tts-done1")
    write_job_meta(
        "doc-tts-done1",
        {"title": "Libro", "kind": "pdf", "char_count": 4321, "duration_estimate": 288.0},
    )

    body = client.get("/api/admin/documents/tts/doc-tts-done1").json()

    assert body["job_status"] == "FINISHED_OK"
    assert body["audio_url"].startswith("/api/audio/doc/doc-tts-done1?exp=")
    assert body["audio_url"] == build_signed_path("doc-tts-done1")
    assert body["duration_estimate"] == 288.0
    assert audio_path.is_file()


def test_status_reports_a_failure_with_its_reason(_doc_tts_env: Path) -> None:
    """FINISHED_ERROR carries the reason the worker recorded."""
    asyncio.run(_job_repo().start_job("doc-tts-bad", collection=JOB_COLLECTION, kind=JOB_KIND))
    asyncio.run(
        _job_repo().finish_job(
            "doc-tts-bad", status="FINISHED_ERROR", error="unreadable PDF", kind=JOB_KIND
        )
    )

    body = client.get("/api/admin/documents/tts/doc-tts-bad").json()

    assert body["job_status"] == "FINISHED_ERROR"
    assert body["error"] == "unreadable PDF"
    assert body["audio_url"] is None


# ---------------------------------------------------------------------------
# Signed audio link
# ---------------------------------------------------------------------------


def test_audio_link_serves_the_file_without_the_admin_token(_doc_tts_env: Path) -> None:
    """The signed link works from a browser with no admin credentials."""
    _finished_job("doc-tts-link1")
    path = build_signed_path("doc-tts-link1")

    response = anon_client.get(path)

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("audio/mpeg")
    assert response.content.startswith(b"ID3")


def test_audio_link_rejects_a_forged_signature(_doc_tts_env: Path) -> None:
    """A flipped signature answers 403, not the audio."""
    _finished_job("doc-tts-link2")
    path = build_signed_path("doc-tts-link2")
    forged = path[:-2] + ("00" if not path.endswith("00") else "11")

    assert anon_client.get(forged).status_code == 403


def test_audio_link_rejects_an_expired_link(_doc_tts_env: Path, monkeypatch) -> None:
    """A link past its TTL answers 403."""
    _finished_job("doc-tts-link3")
    monkeypatch.setattr(settings, "doc_tts_link_ttl_seconds", 60)
    path = build_signed_path("doc-tts-link3", ttl=-5)

    assert anon_client.get(path).status_code == 403


def test_audio_link_requires_query_parameters(_doc_tts_env: Path) -> None:
    """A bare path (no exp/sig) is a validation error, not an open door."""
    _finished_job("doc-tts-link4")
    assert anon_client.get("/api/audio/doc/doc-tts-link4").status_code == 422


def test_audio_link_unknown_job_is_404(_doc_tts_env: Path) -> None:
    """A valid signature over a job that never existed answers 404."""
    path = build_signed_path("doc-tts-ghost")
    assert anon_client.get(path).status_code == 404


def test_audio_link_pending_job_is_404(_doc_tts_env: Path) -> None:
    """Signing a job that has not finished does not unlock anything."""
    _start_job("doc-tts-pending")
    path = build_signed_path("doc-tts-pending")
    assert anon_client.get(path).status_code == 404


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _job_repo() -> AudioJobsRepository:
    """Return a repository bound to the test database."""
    return AudioJobsRepository()


def _job(workflow_id: str) -> dict:
    """Fetch a job row synchronously."""
    job = asyncio.run(_job_repo().get_job(workflow_id, kind=JOB_KIND))
    assert job is not None, f"job {workflow_id} not found"
    return job


def _start_job(workflow_id: str) -> dict:
    """Insert a STARTING job row and return it."""
    asyncio.run(_job_repo().start_job(workflow_id, collection=JOB_COLLECTION, kind=JOB_KIND))
    return _job(workflow_id)


def _finished_job(workflow_id: str) -> Path:
    """Create a finished job row plus its audio file; return the file path."""
    from src.api.services.audio_files import atomic_write

    _start_job(workflow_id)
    audio_dir = Path(settings.doc_tts_dir) / "audio"
    audio_path = audio_dir / f"{workflow_id}.mp3"
    atomic_write(audio_path, b"ID3" + b"\x00" * 32)
    asyncio.run(
        _job_repo().finish_job(
            workflow_id, status="FINISHED_OK", audio_path=str(audio_path), kind=JOB_KIND
        )
    )
    return audio_path
