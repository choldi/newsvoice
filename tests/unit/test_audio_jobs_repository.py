"""Unit tests for AudioJobsRepository (TTS generation status)."""

from src.api.db.database import AudioJobsRepository


async def test_start_job_creates_starting_row() -> None:
    repo = AudioJobsRepository()
    await repo.start_job("run-1", collection="tech")

    job = await repo.get_job("run-1")
    assert job is not None
    assert job["run_id"] == "run-1"
    assert job["collection"] == "tech"
    assert job["status"] == "STARTING"
    assert job["error"] is None
    assert job["audio_path"] is None
    assert job["elapsed_seconds"] is None


async def test_start_job_is_idempotent_and_resets() -> None:
    repo = AudioJobsRepository()
    await repo.start_job("run-1")
    await repo.finish_job(
        "run-1",
        status="FINISHED_OK",
        audio_path="data/tts_cache/x.wav",
        elapsed_seconds=30.0,
    )

    await repo.start_job("run-1")

    rows = await repo.get_job("run-1")
    assert rows is not None
    assert rows["status"] == "STARTING"
    assert rows["audio_path"] is None
    assert rows["elapsed_seconds"] is None


async def test_set_generating_transitions_status() -> None:
    repo = AudioJobsRepository()
    await repo.start_job("run-2")
    await repo.set_generating("run-2")

    job = await repo.get_job("run-2")
    assert job is not None
    assert job["status"] == "GENERATING"


async def test_finish_job_ok_records_audio_and_elapsed() -> None:
    repo = AudioJobsRepository()
    await repo.start_job("run-3")
    await repo.finish_job(
        "run-3",
        status="FINISHED_OK",
        audio_path="data/tts_cache/z.wav",
        elapsed_seconds=912.0,
    )

    job = await repo.get_job("run-3")
    assert job is not None
    assert job["status"] == "FINISHED_OK"
    assert job["audio_path"] == "data/tts_cache/z.wav"
    assert job["elapsed_seconds"] == 912.0
    assert job["finished_at"] is not None
    assert job["error"] is None


async def test_finish_job_error_records_message() -> None:
    repo = AudioJobsRepository()
    await repo.start_job("run-4")
    await repo.finish_job(
        "run-4",
        status="FINISHED_ERROR",
        error="ReadTimeout('')",
        elapsed_seconds=120.5,
    )

    job = await repo.get_job("run-4")
    assert job is not None
    assert job["status"] == "FINISHED_ERROR"
    assert job["error"] == "ReadTimeout('')"
    assert job["elapsed_seconds"] == 120.5
    assert job["audio_path"] is None


async def test_get_job_returns_none_for_unknown_run() -> None:
    job = await AudioJobsRepository().get_job("missing-run")
    assert job is None
