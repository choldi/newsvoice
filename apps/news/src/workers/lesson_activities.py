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
from src.api.models.lesson import LessonContent
from src.api.models.narration import NarrationScript, NarrationSpec, TTSBackend
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
from src.api.services.tts_lease import PRIORITY_RENDER, RenderGate, TTSLeaseService
from src.core.config import get_settings
from src.core.respelling import respell_lesson_script

logger = structlog.get_logger(__name__)

#: ``audio_jobs.kind`` discriminator owned by this module: lesson jobs never
#: collide with the digest ``'tts'``, the ``'narration'`` or the ``'doc_tts'``
#: rows (the unique index is ``UNIQUE(run_id, kind)``).
LESSON_JOB_KIND = "lesson"

#: Temperature for lesson generation: teaching is creative work, not a
#: faithful summary (see CONVENTIONS: 0.3 summaries, 0.7 clustering).
LESSON_TEMPERATURE = 0.7

#: A small local model asked for a whole class in one shot is unreliable: it
#: may wrap the JSON in prose, drop a delimiter, or flatten "segments" to a
#: list of bare strings. Those are format failures, so a fresh sample can fix
#: them; a bad cast cannot and is still raised non-retryable below.
_LESSON_FORMAT_ATTEMPTS = 3
#: The class is longer than a digest and JSON mode still needs headroom.
_LESSON_MAX_TOKENS = 8192

#: Strict schema handed to the provider (Ollama structured outputs). JSON mode
#: alone only guarantees *valid* JSON: qwen3:8b still returned "segments" as an
#: array of strings about half the time, which the model casually reshaped on a
#: fresh sample. A strict schema pins the object shape at the sampling step, so
#: the reply matches ``LessonContent`` on the first try.
_LESSON_JSON_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "title": {"type": "string"},
        "markdown": {"type": "string"},
        "segments": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "role": {"type": "string"},
                    "text": {"type": "string"},
                },
                "required": ["role", "text"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["title", "markdown", "segments"],
    "additionalProperties": False,
}


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
            'target_language', 'native_language', optional 'title',
            'collection' and 'phonetic_adaptation' (Phase 7.16).

    Returns:
        Dict with 'text_path', 'title', 'segments' and 'char_count'.

    Raises:
        ApplicationError: Non-retryable for unusable notes, an invalid mode, a
            failed phonetic adaptation or an unparsable LLM reply after the
            format retries (the job row records the reason first); retryable
            when the LLM call itself fails on every attempt.
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
    from src.core.llm import generate_completion

    # A small local model asked for a whole class in one JSON object sometimes
    # drops a delimiter, wraps the answer in prose, or flattens "segments" to
    # bare strings. A strict schema pins the shape and a fresh sample fixes the
    # rest; the notes and the cast are validated before this point, so only
    # transient formatting can land here.
    content: LessonContent | None = None
    last_error = ""
    transient = False
    for attempt in range(1, _LESSON_FORMAT_ATTEMPTS + 1):
        try:
            reply = await generate_completion(
                messages=messages,
                temperature=LESSON_TEMPERATURE,
                response_format="json_schema",
                json_schema=_LESSON_JSON_SCHEMA,
                max_tokens=_LESSON_MAX_TOKENS,
            )
        except Exception as exc:
            last_error = f"LLM call failed: {exc}"
            transient = True
            activity.logger.warning(f"lesson_generate_retry attempt={attempt} reason={last_error}")
            continue
        try:
            content = parse_lesson_content(reply)
        except LessonError as exc:
            last_error = str(exc)
            transient = False
            activity.logger.warning(f"lesson_generate_retry attempt={attempt} reason={last_error}")
            continue
        break

    if content is None:
        await repo.finish_job(
            run_id,
            status="FINISHED_ERROR",
            error=last_error,
            elapsed_seconds=round(time.monotonic() - started, 2),
            kind=LESSON_JOB_KIND,
        )
        # A transport failure (the call itself failed) is worth a Temporal
        # retry; an unparsable reply is not — the model answered, badly.
        raise ApplicationError(
            f"Lesson generation rejected: {last_error}",
            non_retryable=not transient,
        )

    try:
        script = build_script(content, spec)
    except LessonError as exc:
        await repo.finish_job(
            run_id,
            status="FINISHED_ERROR",
            error=str(exc),
            elapsed_seconds=round(time.monotonic() - started, 2),
            kind=LESSON_JOB_KIND,
        )
        raise ApplicationError(f"Lesson generation rejected: {exc}", non_retryable=True) from exc

    # Phase 7.16: the teacher asks the LLM for the phonetic map of the
    # target-language terms the class quotes, per role, before the script is
    # stored: the render must speak `tts_text` and the metadata must carry the
    # full {term: respelling} map. Unlike narration this is strict — any
    # problem (feature off, wrong engine, unconfigured pair, LLM failure)
    # finishes the job as FINISHED_ERROR instead of shipping a half-adapted
    # class that would teach the wrong pronunciation.
    respelling_metadata: dict[str, Any] | None = None
    if payload.get("phonetic_adaptation"):
        try:
            settings = get_settings()
            if not settings.teacher_respelling_enabled:
                raise ValueError("TEACHER_RESPELLING_ENABLED=false")
            if spec.tts_backend is not TTSBackend.QWEN3_TTS:
                raise ValueError(
                    f"phonetic adaptation requires the qwen3_tts backend "
                    f"(the cast uses {spec.tts_backend})"
                )
            script, respelling_metadata = await respell_lesson_script(
                script,
                spec,
                mode=mode,
                target_language=str(payload.get("target_language") or ""),
                native_language=str(payload.get("native_language") or ""),
            )
        except Exception as exc:
            detail = f"phonetic adaptation failed: {exc}"
            await repo.finish_job(
                run_id,
                status="FINISHED_ERROR",
                error=detail,
                elapsed_seconds=round(time.monotonic() - started, 2),
                kind=LESSON_JOB_KIND,
            )
            raise ApplicationError(
                f"Lesson generation rejected: {detail}", non_retryable=True
            ) from exc

    try:
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

    meta_update: dict[str, Any] = {
        "title": content.title,
        "mode": mode,
        "target_language": str(payload.get("target_language") or ""),
        "native_language": str(payload.get("native_language") or ""),
        "char_count": len(content.markdown),
        "segments": len(content.segments),
        "notes_chars": notes.char_count,
        "text_path": text_path,
    }
    if respelling_metadata is not None:
        # The full term map, per segment and union, for auditing what the
        # voice actually said (Phase 7.16 requirement: store, don't preview).
        meta_update["respelling"] = respelling_metadata
    update_lesson_meta(run_id, meta_update)
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

    def _tick() -> None:
        # Heartbeat while queued for the engine and while paused so a
        # heartbeat_timeout never mistakes a deliberate wait for a stall.
        activity.heartbeat()

    async def _on_transition(paused: bool, reason: str) -> None:
        await repo.set_pause_state(run_id, reason if paused else None, kind=LESSON_JOB_KIND)

    lease = TTSLeaseService()
    try:
        spec = _validated_spec(payload.get("spec"))
        raw_script = Path(script_path).read_text(encoding="utf-8")
        script = NarrationScript.model_validate_json(raw_script, context={"spec": spec})
        async with lease.hold(run_id, PRIORITY_RENDER, on_tick=_tick):
            gate = RenderGate(
                lease,
                run_id,
                PRIORITY_RENDER,
                on_tick=_tick,
                on_transition=_on_transition,
            )
            result = await NarrationTTSService().render(
                spec,
                script,
                collection=collection,
                strategy=RenderStrategy.TURNS,
                gate=gate,
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
