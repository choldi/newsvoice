"""Unit tests for the Phase 7.11 document TTS activities (job bookkeeping)."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from temporalio.exceptions import ApplicationError
from temporalio.testing import ActivityEnvironment

from src.api.db.database import AudioJobsRepository
from src.api.services.doc_tts import job_text_path, read_job_meta, write_job_meta
from src.workers.doc_tts_activities import (
    DOC_TTS_JOB_COLLECTION,
    DOC_TTS_JOB_KIND,
    extract_document_activity,
    synthesize_document_activity,
)

RUN_ID = "doc-tts-act1"


@pytest.fixture(autouse=True)
def _doc_tts_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Keep every job artifact (meta, text, audio) inside the test's tmp_path."""
    monkeypatch.setattr("src.core.config.settings.doc_tts_dir", str(tmp_path / "doc_tts"))
    return tmp_path / "doc_tts"


async def _job(run_id: str = RUN_ID) -> dict:
    """Fetch the audio job row for a run."""
    job = await AudioJobsRepository().get_job(run_id, kind=DOC_TTS_JOB_KIND)
    assert job is not None, f"job {run_id} not found"
    return job


async def _start_job(run_id: str = RUN_ID) -> None:
    """Create the STARTING row the API writes at submit time."""
    await AudioJobsRepository().start_job(
        run_id, collection=DOC_TTS_JOB_COLLECTION, kind=DOC_TTS_JOB_KIND
    )


@pytest.fixture()
def environment() -> ActivityEnvironment:
    """An ActivityEnvironment with an activity context for heartbeats/loggers."""
    return ActivityEnvironment()


async def test_extract_writes_text_and_marks_the_job_generating(
    environment: ActivityEnvironment, tmp_path: Path
) -> None:
    """Extraction persists the text, updates the metadata and moves the job."""
    await _start_job()
    upload = tmp_path / "nota.txt"
    upload.write_text("Primera linea de la noticia.\n\nSegunda linea.\n")
    write_job_meta(RUN_ID, {"filename": "nota.txt"})

    result = await environment.run(
        extract_document_activity,
        {"run_id": RUN_ID, "source_path": str(upload), "filename": "nota.txt", "title": ""},
    )

    text_file = Path(result["text_path"])
    assert text_file == job_text_path(RUN_ID)
    assert "Primera linea" in text_file.read_text(encoding="utf-8")
    assert result["kind"] == "txt"
    assert result["char_count"] > 0
    assert result["title"] == "nota", "the filename stem titles an unmarked document"

    assert (await _job())["status"] == "GENERATING"
    meta = read_job_meta(RUN_ID)
    assert meta["title"] == "nota"
    assert meta["char_count"] == result["char_count"]


async def test_extract_prefers_the_operator_title(
    environment: ActivityEnvironment, tmp_path: Path
) -> None:
    """An explicit title from the submit form survives extraction."""
    await _start_job()
    upload = tmp_path / "documento.md"
    upload.write_text("# Apuntes de clase\n\nContenido.\n")

    result = await environment.run(
        extract_document_activity,
        {
            "run_id": RUN_ID,
            "source_path": str(upload),
            "filename": "documento.md",
            "title": "Clase 3",
        },
    )

    assert result["title"] == "Clase 3"
    assert read_job_meta(RUN_ID)["title"] == "Clase 3"
    assert "#" not in Path(result["text_path"]).read_text(encoding="utf-8")


async def test_extract_failure_marks_the_job_and_is_not_retryable(
    environment: ActivityEnvironment, tmp_path: Path
) -> None:
    """An unreadable document records FINISHED_ERROR and stops retries."""
    await _start_job()

    with pytest.raises(ApplicationError) as excinfo:
        await environment.run(
            extract_document_activity,
            {
                "run_id": RUN_ID,
                "source_path": str(tmp_path / "missing.pdf"),
                "filename": "missing.pdf",
                "title": "",
            },
        )

    assert excinfo.value.non_retryable is True
    job = await _job()
    assert job["status"] == "FINISHED_ERROR"
    assert "missing" in (job["error"] or "")


async def test_extract_rejects_a_payload_without_run_id(
    environment: ActivityEnvironment,
) -> None:
    """Without an id there is no job row to report on: fail loudly."""
    with pytest.raises(ApplicationError, match="missing run_id"):
        await environment.run(
            extract_document_activity, {"source_path": "/tmp/x.txt", "filename": "x.txt"}
        )


async def test_synthesize_closes_the_job_with_the_audio_path(
    environment: ActivityEnvironment, tmp_path: Path
) -> None:
    """Synthesis records FINISHED_OK and the metadata the status endpoint reads."""
    await _start_job()
    text_file = job_text_path(RUN_ID)
    text_file.parent.mkdir(parents=True, exist_ok=True)
    text_file.write_text("Una frase que se convertira en audio.", encoding="utf-8")

    # The service returns a frozen dataclass; a SimpleNamespace matches it.
    from types import SimpleNamespace

    with patch(
        "src.workers.doc_tts_activities.DocTTSService",
        return_value=AsyncMock(
            synthesize_document=AsyncMock(
                return_value=SimpleNamespace(
                    audio_path=str(tmp_path / "audio.mp3"),
                    voice="es-ES-ElviraNeural",
                    language="es",
                    title="Doc",
                    char_count=37,
                    chunk_count=1,
                    duration_estimate=2.47,
                    cached=False,
                )
            )
        ),
    ):
        result = await environment.run(
            synthesize_document_activity,
            {
                "run_id": RUN_ID,
                "text_path": str(text_file),
                "title": "Doc",
                "voice": "",
                "language": "es",
            },
        )

    assert result["audio_path"].endswith("audio.mp3")
    assert result["chunk_count"] == 1
    job = await _job()
    assert job["status"] == "FINISHED_OK"
    assert job["audio_path"] == str(tmp_path / "audio.mp3")
    meta = read_job_meta(RUN_ID)
    assert meta["voice"] == "es-ES-ElviraNeural"
    assert meta["chunk_count"] == 1
    assert meta["duration_estimate"] == 2.47


async def test_synthesize_failure_marks_the_job_error(
    environment: ActivityEnvironment, tmp_path: Path
) -> None:
    """A failing engine records FINISHED_ERROR instead of leaving it stuck."""
    await _start_job()
    text_file = job_text_path(RUN_ID)
    text_file.parent.mkdir(parents=True, exist_ok=True)
    text_file.write_text("Texto.", encoding="utf-8")

    with patch(
        "src.workers.doc_tts_activities.DocTTSService",
        return_value=AsyncMock(
            synthesize_document=AsyncMock(side_effect=RuntimeError("edge down"))
        ),
    ):
        with pytest.raises(ApplicationError) as excinfo:
            await environment.run(
                synthesize_document_activity,
                {
                    "run_id": RUN_ID,
                    "text_path": str(text_file),
                    "title": "",
                    "voice": "",
                    "language": "",
                },
            )

    assert excinfo.value.non_retryable is True
    job = await _job()
    assert job["status"] == "FINISHED_ERROR"
    assert "edge down" in (job["error"] or "")


async def test_extract_records_the_wall_clock_forecast(
    environment: ActivityEnvironment, tmp_path: Path
) -> None:
    """Phase 8.5: the ETA is priced before any audio exists, and is persisted."""
    await _start_job()
    upload = tmp_path / "largolargo.txt"
    upload.write_text("\n\n".join(f"Frase numero {i} con suficiente texto." for i in range(40)))

    result = await environment.run(
        extract_document_activity,
        {
            "run_id": RUN_ID,
            "source_path": str(upload),
            "filename": "largolargo.txt",
            "title": "Largo",
            "voice": "es-MX-JorgeNeural",
            "language": "es",
        },
    )

    assert result["estimate"] is not None, "the forecast must survive the await"
    assert result["estimate"]["wall_seconds"] > 0
    assert result["estimate"]["audio_duration_seconds"] > 0
    assert read_job_meta(RUN_ID)["estimate"]["wall_seconds"] == result["estimate"]["wall_seconds"]
