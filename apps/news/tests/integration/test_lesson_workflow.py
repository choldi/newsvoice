"""Integration tests for the Phase 7.12 LessonWorkflow."""

from __future__ import annotations

from typing import Any

import pytest
from temporalio import activity
from temporalio.client import WorkflowFailureError
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from src.workers.lesson_workflows import LessonConfig, LessonWorkflow

GENERATED = {
    "text_path": "/data/lessons/text/lesson-test.md",
    "script_path": "/data/lessons/text/lesson-test.script.json",
    "title": "Subjuntivo en 10 minutos",
    "segments": 12,
    "char_count": 2400,
    "markdown": "# Subjuntivo en 10 minutos\n\nContenido de la clase.",
}
RENDERED = {
    "audio_path": "/data/narration/lesson_test.m4a",
    "wav_path": "/data/narration/lesson_test.wav",
    "duration_seconds": 180.0,
    "cached": False,
    "voices": ["clone-a"],
    "segments": 12,
    "char_count": 2400,
    "attach_audio": True,
    "audio_url": "/api/audio/lesson/lesson-test?exp=99&sig=abc",
    "elapsed_seconds": 42.0,
}
DELIVERED: list[dict[str, Any]] = []
RENDERED_CALLS: list[dict[str, Any]] = []
EMAIL_ARGS: list[list[Any]] = []
TELEGRAM_DOCS: list[list[Any]] = []
TELEGRAM_AUDIO: list[list[Any]] = []
GENERATE_FAILS: list[bool] = []
LONG_AUDIO = False


@activity.defn(name="generate_lesson_activity")
async def stub_generate(payload: dict[str, Any]) -> dict[str, Any]:
    if GENERATE_FAILS:
        from temporalio.exceptions import ApplicationError

        raise ApplicationError("Lesson LLM call failed: boom", non_retryable=True)
    return GENERATED


@activity.defn(name="render_lesson_activity")
async def stub_render(payload: dict[str, Any]) -> dict[str, Any]:
    RENDERED_CALLS.append(payload)
    if LONG_AUDIO:
        return {**RENDERED, "attach_audio": False, "duration_seconds": 640.0}
    return RENDERED


@activity.defn(name="deliver_email_activity")
async def stub_email(
    recipients: list[str],
    markdown_text: str,
    collection: str,
    audio_path: str | None = None,
    attachments: list[str] | None = None,
) -> dict[str, Any]:
    EMAIL_ARGS.append([recipients, markdown_text, collection, audio_path, attachments])
    DELIVERED.append({"channel": "email"})
    return {"status": "ok", "recipients": len(recipients)}


@activity.defn(name="deliver_telegram_document_activity")
async def stub_tg_document(
    document_path: str, caption: str | None = None, chat_id: str | None = None
) -> dict[str, Any]:
    TELEGRAM_DOCS.append([document_path, caption, chat_id])
    DELIVERED.append({"channel": "telegram_document"})
    return {"status": "ok", "message_id": 1}


@activity.defn(name="deliver_telegram_audio_activity")
async def stub_tg_audio(
    audio_path: str, caption: str | None = None, chat_id: str | None = None
) -> dict[str, Any]:
    TELEGRAM_AUDIO.append([audio_path, caption, chat_id])
    DELIVERED.append({"channel": "telegram_audio"})
    return {"status": "ok", "message_id": 2}


TEACHER_STUBS = [stub_generate, stub_render]
DELIVERY_STUBS = [stub_email, stub_tg_document, stub_tg_audio]


def _reset(*, long_audio: bool = False) -> None:
    """Clear the per-test capture lists (and the render shape)."""
    global LONG_AUDIO
    DELIVERED.clear()
    RENDERED_CALLS.clear()
    EMAIL_ARGS.clear()
    TELEGRAM_DOCS.clear()
    TELEGRAM_AUDIO.clear()
    GENERATE_FAILS.clear()
    LONG_AUDIO = long_audio


def _config(**overrides: Any) -> LessonConfig:
    """Build a lesson config whose deliveries run on the test queue."""
    base: dict[str, Any] = dict(
        notes_path="/data/lessons/uploads/lesson-test.md",
        filename="apuntes.md",
        mode="mixed",
        spec={"roles": [{"name": "profe", "voice_id": "clone-a"}], "language": "en"},
        target_language="en",
        native_language="es",
        collection="tech",
        delivery_task_queue="tq-lesson",
        delivery_channels=["email", "telegram"],
        chat_id="42",
        email_recipients=["me@example.com"],
    )
    base.update(overrides)
    return LessonConfig(**base)


async def _run(config: LessonConfig, *, workflow_id: str = "lesson-test") -> dict[str, Any]:
    """Run LessonWorkflow once against the time-skipping server."""
    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with Worker(
            env.client,
            task_queue="tq-lesson",
            workflows=[LessonWorkflow],
            activities=[*TEACHER_STUBS, *DELIVERY_STUBS],
        ):
            return await env.client.execute_workflow(
                LessonWorkflow.run,
                config,
                id=workflow_id,
                task_queue="tq-lesson",
            )


async def test_lesson_short_audio_ships_everything() -> None:
    """Audio under the limit: email carries the .md + audio, Telegram both."""
    _reset()
    result = await _run(_config())

    assert result["status"] == "ok"
    assert result["title"] == GENERATED["title"]
    assert result["attach_audio"] is True
    assert result["duration_seconds"] == 180.0
    assert result["audio_url"].startswith("/api/audio/lesson/")

    assert len(EMAIL_ARGS) == 1
    recipients, body, collection, email_audio, attachments = EMAIL_ARGS[0]
    assert recipients == ["me@example.com"]
    assert collection == "tech"
    assert body.startswith("# Subjuntivo"), "the readable lesson is the email body"
    assert email_audio == RENDERED["audio_path"], "short audio is attached"
    assert attachments == [GENERATED["text_path"]], "the .md ships as attachment"

    assert len(TELEGRAM_DOCS) == 1
    assert TELEGRAM_DOCS[0][0] == GENERATED["text_path"]
    assert TELEGRAM_DOCS[0][1] == GENERATED["title"]
    assert len(TELEGRAM_AUDIO) == 1
    assert TELEGRAM_AUDIO[0][0] == RENDERED["audio_path"]
    assert [d["channel"] for d in DELIVERED] == [
        "email",
        "telegram_document",
        "telegram_audio",
    ]


async def test_lesson_long_audio_becomes_a_link() -> None:
    """Over the limit the audio is not attached: the text carries the URL."""
    _reset(long_audio=True)
    result = await _run(_config())

    assert result["attach_audio"] is False
    _, body, _, email_audio, attachments = EMAIL_ARGS[0]
    assert email_audio is None, "long audio is never attached"
    assert attachments == [GENERATED["text_path"]]
    assert RENDERED["audio_url"] in body, "the signed URL travels in the text"
    assert TELEGRAM_AUDIO == [], "no audio message for a long lesson"
    captions = [doc[1] for doc in TELEGRAM_DOCS]
    assert any(RENDERED["audio_url"] in caption for caption in captions)


async def test_lesson_generation_failure_stops_the_run() -> None:
    """A failed LLM call fails the workflow before any render or delivery."""
    _reset()
    GENERATE_FAILS.append(True)

    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with Worker(
            env.client,
            task_queue="tq-lesson-fail",
            workflows=[LessonWorkflow],
            activities=[*TEACHER_STUBS, *DELIVERY_STUBS],
        ):
            with pytest.raises(WorkflowFailureError) as excinfo:
                await env.client.execute_workflow(
                    LessonWorkflow.run,
                    _config(delivery_task_queue="tq-lesson-fail"),
                    id="lesson-fail",
                    task_queue="tq-lesson-fail",
                )

    assert "Lesson generate failed" in str(excinfo.value.cause)
    assert RENDERED_CALLS == []
    assert DELIVERED == []


async def test_lesson_without_delivery_queue_returns_only_the_result() -> None:
    """An empty delivery queue means no channels are touched at all."""
    _reset()
    result = await _run(_config(delivery_task_queue=""))

    assert result["status"] == "ok"
    assert result["delivery_status"] == []
    assert DELIVERED == []


async def test_lesson_deliveries_are_addressed_on_the_digest_queue() -> None:
    """The workflow schedules deliveries on delivery_task_queue, not its own.

    The delivery activities live on the digest queue by design (the teacher
    worker must not register them), so this test runs two workers to prove the
    cross-queue hop happens.
    """
    _reset()
    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with Worker(
            env.client,
            task_queue="tq-teacher-real",
            workflows=[LessonWorkflow],
            activities=TEACHER_STUBS,
        ):
            async with Worker(
                env.client,
                task_queue="tq-digest-real",
                activities=DELIVERY_STUBS,
            ):
                result = await env.client.execute_workflow(
                    LessonWorkflow.run,
                    _config(delivery_task_queue="tq-digest-real"),
                    id="lesson-cross",
                    task_queue="tq-teacher-real",
                )

    assert result["status"] == "ok"
    assert [d["channel"] for d in DELIVERED] == [
        "email",
        "telegram_document",
        "telegram_audio",
    ], "deliveries must run on the digest queue where they are registered"
