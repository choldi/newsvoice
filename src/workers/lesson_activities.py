"""Temporal activities for the language-teacher worker (Phase 7.12).

Registered **only** on the teacher worker (see
``src/workers/run_teacher_worker.py``): the LLM call and the narration render
are long, and neither belongs on the digest queue. Deliveries are the
exception: they are addressed **by name** on the digest queue from
``LessonWorkflow``, because the delivery activities live there.

Everything with side effects lives here; the workflow only orchestrates.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import structlog
from temporalio import activity
from temporalio.exceptions import ApplicationError

from src.api.db.database import AudioJobsRepository
from src.api.models.narration import NarrationScript, NarrationSpec
from src.api.services.lesson import (
    LessonError,
    attachment_text,
    build_lesson_messages,
    build_script,
    lesson_audio_link,
    lesson_script_path,
    parse_lesson_content,
    read_lesson_meta,
    save_lesson,
    should_attach_audio,
    update_lesson_meta,
    validate_mode,
    validate_notes,
)
from src.api.services.narration_tts import NarrationTTSService, RenderStrategy

logger = structlog.get_logger(__name__)

#: ``audio_jobs.kind`` discriminator owned by this module: lesson jobs never
#: collide with the digest ``'tts'``, the ``'narration'`` or the ``'doc_tts'``
#: rows (the unique index is ``UNIQUE(run_id, kind)``).
LESSON_JOB_KIND = "lesson"

#: Temperature for lesson generation: teaching is creative work, not a
#: faithful summary (see CONVENTIONS: 0.3 summaries, 0.7 clustering).
LESSON_TEMPERATURE = 0.7


def _validated_spec(raw: Any) -> NarrationSpec:
    """Validate the cast carried by the payload.

    Args:
        raw: NarrationSpec dump (dict) from the config.

    Returns:
        The validated spec.

    Raises:
        ApplicationError: Non-retryable when the cast is missing or invalid.
    """
    if not isinstance(raw, dict):
        raise ApplicationError("lesson activity needs a 'spec' dict", non_retryable=True)
    try:
        return NarrationSpec.model_validate(raw)
    except Exception as exc:
        raise ApplicationError(f"Invalid narration spec: {exc}", non_retryable=True) from exc


@activity.defn
async def generate_lesson_activity(payload: dict[str, Any]) -> dict[str, Any]:
    """Turn uploaded notes into a lesson: Markdown for reading + spoken script.

    The result is stored on disk (``teacher_dir/text``) so only paths travel
    in the Temporal payload; a lesson script can be tens of kilobytes.

    Args:
        payload: Dict with 'run_id', 'notes_path', 'mode', 'spec',
            'target_language', 'native_language', optional 'title' and
            'collection'.

    Returns:
        Dict with 'text_path', 'title', 'segments' and 'char_count'.

    Raises:
        ApplicationError: Non-retryable for unusable notes, an invalid mode or
            an unparsable LLM reply (the job row records the reason first);
            retryable when the LLM call itself fails.
    """
    run_id = str(payload.get("run_id") or "")
    if not run_id:
        raise ApplicationError("generate_lesson_activity: missing run_id", non_retryable=True)
    collection = str(payload.get("collection") or "default")
    mode = str(payload.get("mode") or "mixed")
    notes_path = str(payload.get("notes_path") or "")
    activity.logger.info(f"Generating lesson {run_id} mode={mode} collection={collection}")
    repo = AudioJobsRepository()
    await repo.set_generating(run_id, kind=LESSON_JOB_KIND)

    try:
        notes_file = Path(notes_path)
        if not notes_file.is_file():
            raise LessonError("uploaded notes are missing on disk")
        notes = validate_notes(notes_file.read_bytes(), notes_file.name)
        spec = _validated_spec(payload.get("spec"))
        validate_mode(mode, spec)
    except LessonError as exc:
        await repo.finish_job(run_id, status="FINISHED_ERROR", error=str(exc), kind=LESSON_JOB_KIND)
        raise ApplicationError(f"Lesson notes rejected: {exc}", non_retryable=True) from exc
    except OSError as exc:
        await repo.finish_job(run_id, status="FINISHED_ERROR", error=str(exc), kind=LESSON_JOB_KIND)
        raise ApplicationError(f"Cannot read the notes: {exc}", non_retryable=True) from exc

    messages = build_lesson_messages(
        notes.text,
        mode,
        spec,
        target_language=str(payload.get("target_language") or ""),
        native_language=str(payload.get("native_language") or ""),
    )
    started = time.monotonic()
    try:
        from src.core.llm import generate_completion

        reply = await generate_completion(
            messages=messages,
            temperature=LESSON_TEMPERATURE,
        )
        content = parse_lesson_content(reply)
        script = build_script(content, spec)
        text_path = save_lesson(run_id, content, script)
        script_path = str(lesson_script_path(run_id))
        markdown = attachment_text(content)
    except LessonError as exc:
        await repo.finish_job(
            run_id,
            status="FINISHED_ERROR",
            error=str(exc),
            elapsed_seconds=round(time.monotonic() - started, 2),
            kind=LESSON_JOB_KIND,
        )
        raise ApplicationError(f"Lesson generation rejected: {exc}", non_retryable=True) from exc
    except Exception as exc:
        # Transient LLM failure: record it (so a run that never comes back is
        # not stuck in GENERATING) but allow Temporal to retry the activity.
        await repo.finish_job(
            run_id,
            status="FINISHED_ERROR",
            error=str(exc),
            elapsed_seconds=round(time.monotonic() - started, 2),
            kind=LESSON_JOB_KIND,
        )
        raise ApplicationError(f"Lesson LLM call failed: {exc}") from exc

    update_lesson_meta(
        run_id,
        {
            "title": content.title,
            "mode": mode,
            "target_language": str(payload.get("target_language") or ""),
            "native_language": str(payload.get("native_language") or ""),
            "char_count": len(content.markdown),
            "segments": len(content.segments),
            "notes_chars": notes.char_count,
            "text_path": text_path,
        },
    )
    activity.logger.info(
        f"Lesson generated: title={content.title!r} chars={len(content.markdown)} "
        f"segments={len(content.segments)}"
    )
    return {
        # Paths keep the payload small; 'markdown' is the one piece of content
        # the workflow genuinely needs in memory (it cannot read files) to hand
        # it to the delivery activities.
        "text_path": text_path,
        "script_path": script_path,
        "title": content.title,
        "segments": len(content.segments),
        "char_count": len(content.markdown),
        "markdown": markdown,
    }


@activity.defn
async def render_lesson_activity(payload: dict[str, Any]) -> dict[str, Any]:
    """Render the generated script with the requested cast (Phase 7.12 TTS).

    The audio goes through the same ``NarrationTTSService`` the digest uses,
    so the class is voiced by the operator's cloned voices. The signed link to
    the result is built **here** (activities may read settings; workflows may
    not), so the workflow never sees a secret.

    Args:
        payload: Dict with 'run_id', 'script_path', 'spec' and 'collection'.

    Returns:
        Dict with 'audio_path', 'duration_seconds', 'cached', 'voices',
        'attach_audio' (Phase 7.12 300 s rule) and 'audio_url'.

    Raises:
        ApplicationError: When the script is invalid or the render fails; the
            job row records FINISHED_ERROR first.
    """
    run_id = str(payload.get("run_id") or "")
    script_path = str(payload.get("script_path") or "")
    collection = str(payload.get("collection") or "default")
    if not run_id:
        raise ApplicationError("render_lesson_activity: missing run_id", non_retryable=True)
    activity.logger.info(f"Rendering lesson audio for {run_id}")
    repo = AudioJobsRepository()
    await repo.set_generating(run_id, kind=LESSON_JOB_KIND)
    started = time.monotonic()

    try:
        spec = _validated_spec(payload.get("spec"))
        raw_script = Path(script_path).read_text(encoding="utf-8")
        script = NarrationScript.model_validate_json(raw_script, context={"spec": spec})
        result = await NarrationTTSService().render(
            spec,
            script,
            collection=collection,
            strategy=RenderStrategy.TURNS,
        )
    except Exception as exc:
        await repo.finish_job(
            run_id,
            status="FINISHED_ERROR",
            error=str(exc),
            elapsed_seconds=round(time.monotonic() - started, 2),
            kind=LESSON_JOB_KIND,
        )
        raise ApplicationError(f"Lesson render failed: {exc}", non_retryable=True) from exc

    elapsed = round(time.monotonic() - started, 2)
    await repo.finish_job(
        run_id,
        status="FINISHED_OK",
        audio_path=result.audio_path,
        elapsed_seconds=elapsed,
        kind=LESSON_JOB_KIND,
    )
    attach = should_attach_audio(result.duration_seconds)
    audio_url = lesson_audio_link(run_id)
    meta = read_lesson_meta(run_id)
    meta.update(
        {
            "audio_path": result.audio_path,
            "duration_seconds": result.duration_seconds,
            "attach_audio": attach,
            "cached": result.cached,
            "voices": result.voices,
            "segments": result.segments,
        }
    )
    update_lesson_meta(run_id, meta)
    activity.logger.info(
        f"Lesson audio ready: {result.audio_path} "
        f"duration={result.duration_seconds}s attach={attach}"
    )
    return {
        "audio_path": result.audio_path,
        "wav_path": result.wav_path,
        "duration_seconds": result.duration_seconds,
        "cached": result.cached,
        "voices": result.voices,
        "segments": result.segments,
        "char_count": result.char_count,
        "attach_audio": attach,
        "audio_url": audio_url,
        "elapsed_seconds": elapsed,
    }
