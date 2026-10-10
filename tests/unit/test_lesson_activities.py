"""Unit tests for the Phase 7.12 lesson generation activity (format retries)."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from temporalio.exceptions import ApplicationError
from temporalio.testing import ActivityEnvironment

from src.api.db.database import AudioJobsRepository
from src.api.models.narration import NarrationSpec
from src.api.services.lesson import lesson_script_path, lesson_text_path
from src.workers.lesson_activities import LESSON_JOB_KIND, generate_lesson_activity

RUN_ID = "lesson-act1"

VALID_LESSON = {
    "title": "La negacion",
    "markdown": "# La negacion\n\nExplicacion.",
    "segments": [{"role": "profe", "text": "Bonjour, on va pratiquer."}],
}

SPEC = NarrationSpec(
    language="fr",
    roles=[{"name": "profe", "voice_id": "voice-1"}],  # type: ignore[arg-type]
)


@pytest.fixture(autouse=True)
def _lesson_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point lesson storage at tmp_path for every test."""
    monkeypatch.setattr("src.core.config.settings.teacher_dir", str(tmp_path / "lessons"))
    return tmp_path / "lessons"


def _payload(notes_path: Path) -> dict:
    """Build a generate_lesson_activity payload with a one-voice cast."""
    return {
        "run_id": RUN_ID,
        "notes_path": str(notes_path),
        "mode": "mixed",
        "spec": SPEC.model_dump(),
        "target_language": "fr",
        "native_language": "es",
        "collection": "tech",
    }


async def _prepare_notes(tmp_path: Path) -> Path:
    """Write notes and open the audio_jobs row the submit would create."""
    notes = tmp_path / "notas.md"
    notes.write_text("**Tu veux venir ?** - Oui, je veux.", encoding="utf-8")
    await AudioJobsRepository().start_job(RUN_ID, collection="tech", kind=LESSON_JOB_KIND)
    return notes


async def test_generate_retries_then_succeeds_on_malformed_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A malformed reply is retried with a fresh sample instead of failing."""
    notes = await _prepare_notes(tmp_path)
    malformed = '{"title": "x", "markdown": "linea\nrota", "segments": []}'
    good = json.dumps(VALID_LESSON)
    completion = AsyncMock(side_effect=[malformed, good])
    monkeypatch.setattr("src.core.llm.generate_completion", completion)

    result = await ActivityEnvironment().run(generate_lesson_activity, _payload(notes))

    assert completion.await_count == 2
    assert result["title"] == "La negacion"
    assert Path(result["text_path"]).is_file()
    job = await AudioJobsRepository().get_job(RUN_ID, kind=LESSON_JOB_KIND)
    # Generation leaves the job GENERATING; the render activity finishes it.
    assert job is not None and job["status"] == "GENERATING"


async def test_generate_uses_json_mode(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The LLM is called in JSON mode so a stray newline cannot break the reply."""
    notes = await _prepare_notes(tmp_path)
    completion = AsyncMock(return_value=json.dumps(VALID_LESSON))
    monkeypatch.setattr("src.core.llm.generate_completion", completion)

    await ActivityEnvironment().run(generate_lesson_activity, _payload(notes))

    _, kwargs = completion.call_args
    assert kwargs["response_format"] == "json_object"


async def test_generate_gives_up_after_the_format_retries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every attempt malformed: the job records the reason and stays permanent."""
    notes = await _prepare_notes(tmp_path)
    malformed = '{"title": "x", "markdown": "linea\nrota", "segments": []}'
    completion = AsyncMock(return_value=malformed)
    monkeypatch.setattr("src.core.llm.generate_completion", completion)

    with pytest.raises(ApplicationError, match="not valid JSON") as excinfo:
        await ActivityEnvironment().run(generate_lesson_activity, _payload(notes))

    assert completion.await_count == 3
    assert excinfo.value.non_retryable is True
    job = await AudioJobsRepository().get_job(RUN_ID, kind=LESSON_JOB_KIND)
    assert job is not None and job["status"] == "FINISHED_ERROR"


async def test_generate_keeps_a_transport_failure_retryable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failing LLM call is retried by Temporal after the in-activity attempts."""
    notes = await _prepare_notes(tmp_path)
    completion = AsyncMock(side_effect=RuntimeError("connection reset"))
    monkeypatch.setattr("src.core.llm.generate_completion", completion)

    with pytest.raises(ApplicationError, match="LLM call failed") as excinfo:
        await ActivityEnvironment().run(generate_lesson_activity, _payload(notes))

    assert completion.await_count == 3
    assert excinfo.value.non_retryable is False


async def test_generate_writes_script_and_meta(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A clean reply persists the Markdown and the spoken script."""
    notes = await _prepare_notes(tmp_path)
    completion = AsyncMock(return_value=json.dumps(VALID_LESSON))
    monkeypatch.setattr("src.core.llm.generate_completion", completion)

    result = await ActivityEnvironment().run(generate_lesson_activity, _payload(notes))

    assert Path(lesson_text_path(RUN_ID)).is_file()
    assert Path(lesson_script_path(RUN_ID)).is_file()
    assert result["segments"] == 1
