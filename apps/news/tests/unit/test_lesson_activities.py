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


async def test_generate_uses_strict_schema(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The reply is constrained by a strict schema so its shape cannot drift."""
    notes = await _prepare_notes(tmp_path)
    completion = AsyncMock(return_value=json.dumps(VALID_LESSON))
    monkeypatch.setattr("src.core.llm.generate_completion", completion)

    await ActivityEnvironment().run(generate_lesson_activity, _payload(notes))

    _, kwargs = completion.call_args
    assert kwargs["response_format"] == "json_schema"
    segment = kwargs["json_schema"]["properties"]["segments"]["items"]
    assert segment["required"] == ["role", "text"]


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


# ---------------------------------------------------------------------------
# Phase 7.16: phonetic adaptation
# ---------------------------------------------------------------------------

#: Dual cast on the only engine the adaptation feeds (qwen3_tts tts_text).
DUAL_SPEC: dict = {
    "language": "en",
    "tts_backend": "qwen3_tts",
    "roles": [
        {"name": "profe_native", "voice_id": "voice-es", "language": "es"},
        {"name": "profe_target", "voice_id": "voice-en", "language": "en"},
    ],
}
DUAL_LESSON: dict = {
    "title": "Saludos",
    "markdown": "# Saludos\n\nSe dice startup.",
    "segments": [
        {"role": "profe_native", "text": "En inglés decimos startup para una empresa nueva."},
        {"role": "profe_target", "text": "A startup is a young company."},
    ],
}


def _dual_payload(notes_path: Path, **overrides: object) -> dict:
    """Build a dual-mode generate payload with the adaptation requested.

    Args:
        notes_path: Path of the uploaded notes.
        **overrides: Payload fields replacing the defaults.

    Returns:
        The activity payload.
    """
    payload: dict = {
        "run_id": RUN_ID,
        "notes_path": str(notes_path),
        "mode": "dual",
        "spec": DUAL_SPEC,
        "target_language": "en",
        "native_language": "es",
        "collection": "tech",
        "phonetic_adaptation": True,
    }
    payload.update(overrides)
    return payload


async def test_generate_adapts_the_script_when_requested(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The stored script carries tts_text and the meta the full term map."""
    from src.api.models.narration import NarrationScript

    notes = await _prepare_notes(tmp_path)
    generate = AsyncMock(return_value=json.dumps(DUAL_LESSON))
    respell = AsyncMock(return_value='{"startup": "estartap"}')
    monkeypatch.setattr("src.core.llm.generate_completion", generate)
    monkeypatch.setattr("src.core.respelling.generate_completion", respell)

    result = await ActivityEnvironment().run(generate_lesson_activity, _dual_payload(notes))

    respell.assert_awaited_once()
    spec = NarrationSpec.model_validate(DUAL_SPEC)
    script = NarrationScript.model_validate_json(
        Path(lesson_script_path(RUN_ID)).read_text(encoding="utf-8"), context={"spec": spec}
    )
    assert script.segments[0].tts_text == ("En inglés decimos estartap para una empresa nueva.")
    assert script.segments[0].text.startswith("En inglés decimos startup")
    assert script.segments[1].tts_text is None, "the target-language role speaks natively"
    assert result["segments"] == 2

    from src.api.services.lesson import read_lesson_meta

    meta = read_lesson_meta(RUN_ID)
    assert meta["respelling"]["terms"] == {"startup": "estartap"}
    assert meta["respelling"]["language_origin"] == "en"
    assert meta["respelling"]["language_target"] == "es"
    entry = meta["respelling"]["segments"][0]
    assert entry["role"] == "profe_native"
    assert entry["term_map"] == {"startup": "estartap"}
    job = await AudioJobsRepository().get_job(RUN_ID, kind=LESSON_JOB_KIND)
    assert job is not None and job["status"] == "GENERATING"


async def test_generate_skips_the_adaptation_when_not_requested(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without the flag the respelling LLM is never asked."""
    notes = await _prepare_notes(tmp_path)
    generate = AsyncMock(return_value=json.dumps(DUAL_LESSON))
    respell = AsyncMock()
    monkeypatch.setattr("src.core.llm.generate_completion", generate)
    monkeypatch.setattr("src.core.respelling.generate_completion", respell)
    payload = {**_dual_payload(notes), "phonetic_adaptation": False}

    await ActivityEnvironment().run(generate_lesson_activity, payload)

    respell.assert_not_awaited()
    from src.api.services.lesson import read_lesson_meta

    assert "respelling" not in read_lesson_meta(RUN_ID)


async def test_generate_reports_an_unconfigured_pair_as_a_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No heuristics for the pair: the job fails loudly instead of guessing."""
    notes = await _prepare_notes(tmp_path)
    generate = AsyncMock(return_value=json.dumps(DUAL_LESSON))
    respell = AsyncMock()
    monkeypatch.setattr("src.core.llm.generate_completion", generate)
    monkeypatch.setattr("src.core.respelling.generate_completion", respell)
    payload = _dual_payload(notes, target_language="de")

    with pytest.raises(ApplicationError, match="phonetic adaptation failed") as excinfo:
        await ActivityEnvironment().run(generate_lesson_activity, payload)

    assert excinfo.value.non_retryable is True
    respell.assert_not_awaited()
    job = await AudioJobsRepository().get_job(RUN_ID, kind=LESSON_JOB_KIND)
    assert job is not None and job["status"] == "FINISHED_ERROR"
    assert "no phonetic heuristics for de -> es" in str(job["error"])


async def test_generate_fails_when_the_adaptation_llm_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failing adaptation is an error: no half-adapted class is stored."""
    notes = await _prepare_notes(tmp_path)
    generate = AsyncMock(return_value=json.dumps(DUAL_LESSON))
    respell = AsyncMock(side_effect=RuntimeError("llm down"))
    monkeypatch.setattr("src.core.llm.generate_completion", generate)
    monkeypatch.setattr("src.core.respelling.generate_completion", respell)

    with pytest.raises(ApplicationError, match="phonetic adaptation failed") as excinfo:
        await ActivityEnvironment().run(generate_lesson_activity, _dual_payload(notes))

    assert excinfo.value.non_retryable is True
    job = await AudioJobsRepository().get_job(RUN_ID, kind=LESSON_JOB_KIND)
    assert job is not None and job["status"] == "FINISHED_ERROR"
    assert "llm down" in str(job["error"])


async def test_generate_rejects_adaptation_on_the_wrong_backend(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only qwen3_tts reads tts_text, so any other cast is refused up front."""
    notes = await _prepare_notes(tmp_path)
    generate = AsyncMock(return_value=json.dumps(DUAL_LESSON))
    respell = AsyncMock()
    monkeypatch.setattr("src.core.llm.generate_completion", generate)
    monkeypatch.setattr("src.core.respelling.generate_completion", respell)
    payload = _dual_payload(notes, spec={**DUAL_SPEC, "tts_backend": "omni_voice"})

    with pytest.raises(ApplicationError, match="qwen3_tts backend"):
        await ActivityEnvironment().run(generate_lesson_activity, payload)

    respell.assert_not_awaited()
    job = await AudioJobsRepository().get_job(RUN_ID, kind=LESSON_JOB_KIND)
    assert job is not None and job["status"] == "FINISHED_ERROR"
