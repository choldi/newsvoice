"""Unit tests for NarrationRepository and the narration columns of runs."""

from src.api.db.database import NarrationRepository, RunsRepository
from src.api.models.narration import NarrationScript


def _script(text: str = "Buenos dias, hoy hablamos de tecnologia") -> NarrationScript:
    """Build a minimal valid script."""
    return NarrationScript.model_validate(
        {
            "title": "Edicion del dia",
            "source": "llm",
            "segments": [{"role": "anchor", "text": text}],
        }
    )


async def test_save_and_get_script_round_trip() -> None:
    repo = NarrationRepository()
    script = _script()

    script_id = await repo.save_script("run-1", script, collection="tech")

    stored = await repo.get_script(script_id)
    assert stored == script
    assert stored is not None
    assert stored.title == "Edicion del dia"


async def test_get_script_returns_none_for_unknown_id() -> None:
    assert await NarrationRepository().get_script(9999) is None


async def test_latest_script_for_run_returns_most_recent() -> None:
    repo = NarrationRepository()
    first = await repo.save_script("run-2", _script("primera version"), collection="tech")
    second = await repo.save_script(
        "run-2", _script("segunda version corregida"), collection="tech"
    )

    latest = await repo.latest_script_for_run("run-2")
    assert latest is not None
    assert latest.segments[0].text == "segunda version corregida"
    assert first != second


async def test_latest_script_for_run_returns_none_when_absent() -> None:
    assert await NarrationRepository().latest_script_for_run("missing-run") is None


async def test_scripts_are_scoped_per_run() -> None:
    repo = NarrationRepository()
    await repo.save_script("run-a", _script("del run a"), collection="tech")
    await repo.save_script("run-b", _script("del run b"), collection="sport")

    for run_id, expected in (("run-a", "del run a"), ("run-b", "del run b")):
        script = await repo.latest_script_for_run(run_id)
        assert script is not None
        assert script.segments[0].text == expected


async def test_run_records_narration_path_alongside_digest_audio() -> None:
    runs = RunsRepository()

    run_id = await runs.record_run(
        "run-3",
        collection="tech",
        status="ok",
        article_count=40,
        cluster_count=5,
        digest_preview="preview",
        audio_path="data/tts_cache/digest.m4a",
        narration_path="data/narration/narrate_tech_20260926.m4a",
    )

    run = await runs.get_run(run_id)
    assert run is not None
    assert run["audio_path"] == "data/tts_cache/digest.m4a"
    assert run["narration_path"] == "data/narration/narrate_tech_20260926.m4a"

    listed = await runs.list_runs(collection="tech")
    assert listed[0]["narration_path"] == "data/narration/narrate_tech_20260926.m4a"


async def test_later_run_update_keeps_an_existing_narration_path() -> None:
    """The digest workflow must not erase the path written by the narration worker."""
    runs = RunsRepository()
    run_id = await runs.record_run(
        "run-4",
        collection="tech",
        status="ok",
        article_count=40,
        cluster_count=5,
        narration_path="data/narration/narrate_tech_20260926.m4a",
    )

    await runs.record_run(
        "run-4",
        collection="tech",
        status="ok",
        article_count=41,
        cluster_count=5,
        audio_path="data/tts_cache/digest.m4a",
    )

    run = await runs.get_run(run_id)
    assert run is not None
    assert run["narration_path"] == "data/narration/narrate_tech_20260926.m4a"
    assert run["audio_path"] == "data/tts_cache/digest.m4a"


async def test_run_without_narration_reports_null_path() -> None:
    runs = RunsRepository()
    run_id = await runs.record_run("run-5", "tech", "ok", 0, 0)

    run = await runs.get_run(run_id)
    assert run is not None
    assert run["narration_path"] is None
