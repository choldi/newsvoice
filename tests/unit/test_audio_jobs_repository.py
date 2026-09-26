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


async def test_start_job_defaults_to_tts_kind() -> None:
    await AudioJobsRepository().start_job("run-5", collection="tech")

    job = await AudioJobsRepository().get_job("run-5")
    assert job is not None
    assert job["kind"] == "tts"


async def test_narration_and_tts_jobs_coexist_for_the_same_run() -> None:
    repo = AudioJobsRepository()
    await repo.start_job("run-6", collection="tech")
    await repo.start_job("run-6", collection="tech", kind="narration")

    await repo.finish_job(
        "run-6",
        status="FINISHED_OK",
        audio_path="data/tts_cache/digest.m4a",
        elapsed_seconds=20.0,
    )
    await repo.finish_job(
        "run-6",
        status="FINISHED_OK",
        audio_path="data/narration/narrate_tech_20260926.m4a",
        elapsed_seconds=140.0,
        kind="narration",
    )

    tts_job = await repo.get_job("run-6")
    narration_job = await repo.get_job("run-6", kind="narration")
    assert tts_job is not None
    assert narration_job is not None
    assert tts_job["kind"] == "tts"
    assert tts_job["audio_path"] == "data/tts_cache/digest.m4a"
    assert narration_job["kind"] == "narration"
    assert narration_job["audio_path"] == "data/narration/narrate_tech_20260926.m4a"


async def test_status_updates_are_scoped_to_the_job_kind() -> None:
    repo = AudioJobsRepository()
    await repo.start_job("run-7")
    await repo.start_job("run-7", kind="narration")

    await repo.set_generating("run-7", kind="narration")

    tts_job = await repo.get_job("run-7")
    assert tts_job is not None
    assert tts_job["status"] == "STARTING"


async def test_list_jobs_filters_by_kind() -> None:
    repo = AudioJobsRepository()
    await repo.start_job("run-8", collection="tech")
    await repo.start_job("run-8", collection="tech", kind="narration")
    await repo.start_job("run-9", collection="sport")

    assert [job["kind"] for job in await repo.list_jobs(collection="tech")] == ["narration", "tts"]
    assert [job["run_id"] for job in await repo.list_jobs(kind="tts")] == ["run-9", "run-8"]
