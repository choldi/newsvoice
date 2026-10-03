"""Temporal workflow for a language-teacher lesson (Phase 7.12).

generate (LLM) -> render (NarrationTTSService) -> deliver on the digest queue.

The teacher queue only runs the long, local steps: every delivery activity is
addressed **by name** on ``delivery_task_queue`` (resolved outside the
workflow, because a workflow cannot read settings), exactly like the
cross-queue pattern documented for narration in CONVENTIONS.md.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError, ApplicationError

with workflow.unsafe.imports_passed_through():
    from src.workers.lesson_activities import (
        generate_lesson_activity,
        render_lesson_activity,
    )

# A local LLM on a page of notes: bounded generously, retries are cheap
# because generation is deterministic enough to re-run.
LESSON_GENERATE_TIMEOUT = timedelta(minutes=30)
# Same ceiling the narration render uses: a full class can be 10+ minutes of
# audio across many gateway requests.
LESSON_RENDER_TIMEOUT = timedelta(seconds=7200)
LESSON_DELIVERY_TIMEOUT = timedelta(seconds=120)
LESSON_RETRY_POLICY = RetryPolicy(maximum_attempts=3)


@dataclass
class LessonConfig:
    """Input configuration for one lesson run (Phase 7.12).

    Everything a workflow cannot read (settings, collection channels, the
    target queue) is resolved by the submitting API. The workflow id
    (``lesson-<hex>``) comes from ``workflow.info()``: it keys the upload, the
    metadata and the audio job row.
    """

    notes_path: str = ""
    filename: str = ""
    mode: str = "mixed"
    spec: dict = field(default_factory=dict)
    """NarrationSpec dump: the cast that voices the class."""
    target_language: str = "en"
    native_language: str = "es"
    title: str = ""
    collection: str = "default"
    # --- resolved outside the workflow ---
    delivery_task_queue: str = ""
    """Digest queue, where the delivery activities are registered."""
    delivery_channels: list[str] = field(default_factory=lambda: ["telegram"])
    chat_id: str | None = None
    email_recipients: list[str] = field(default_factory=list)


@workflow.defn
class LessonWorkflow:
    """Generate, voice and deliver one language class."""

    @workflow.run
    async def run(self, config: LessonConfig) -> dict[str, Any]:
        """Run generate -> render -> deliver for one lesson.

        Args:
            config: Paths, cast and delivery options resolved by the API.

        Returns:
            Dict with status, title, text_path, audio_path, duration_seconds,
            attach_audio, audio_url and the per-channel delivery status.

        Raises:
            ApplicationError: When generation or rendering fails. The failing
                activity has already recorded the job as FINISHED_ERROR, so
                the status endpoint reports the reason either way.
        """
        run_id = workflow.info().workflow_id
        workflow.logger.info(f"Lesson started: {config.filename} mode={config.mode} ({run_id})")

        step = "generate"
        try:
            generated = await workflow.execute_activity(
                generate_lesson_activity,
                {
                    "run_id": run_id,
                    "notes_path": config.notes_path,
                    "mode": config.mode,
                    "spec": config.spec,
                    "target_language": config.target_language,
                    "native_language": config.native_language,
                    "title": config.title,
                    "collection": config.collection,
                },
                start_to_close_timeout=LESSON_GENERATE_TIMEOUT,
                retry_policy=LESSON_RETRY_POLICY,
            )
            step = "render"
            rendered = await workflow.execute_activity(
                render_lesson_activity,
                {
                    "run_id": run_id,
                    "script_path": generated["script_path"],
                    "spec": config.spec,
                    "collection": config.collection,
                },
                start_to_close_timeout=LESSON_RENDER_TIMEOUT,
                retry_policy=LESSON_RETRY_POLICY,
            )
        except (ActivityError, ApplicationError) as exc:
            detail = str(getattr(exc, "cause", None) or exc)
            raise ApplicationError(f"Lesson {step} failed: {detail}", non_retryable=True) from exc

        delivery_status = await self._deliver(config, generated, rendered)
        workflow.logger.info(
            f"Lesson completed: {run_id} duration={rendered.get('duration_seconds')}s "
            f"attached={rendered.get('attach_audio')}"
        )
        return {
            "status": "ok",
            "run_id": run_id,
            "title": generated.get("title") or config.title,
            "text_path": generated.get("text_path"),
            "audio_path": rendered.get("audio_path"),
            "duration_seconds": rendered.get("duration_seconds"),
            "attach_audio": bool(rendered.get("attach_audio")),
            "audio_url": rendered.get("audio_url"),
            "delivery_status": delivery_status,
        }

    async def _deliver(
        self, config: LessonConfig, generated: dict[str, Any], rendered: dict[str, Any]
    ) -> list[dict[str, Any]]:
        """Deliver the lesson on the digest queue (Phase 7.12 rules).

        The Markdown always ships (email attachment + Telegram document). The
        audio ships as a file only when it fits under the configured limit;
        otherwise the delivered text carries the signed URL instead.

        Args:
            config: Resolved lesson configuration.
            generated: Result of the generation activity.
            rendered: Result of the render activity.

        Returns:
            One entry per attempted delivery, or [] when there is no queue.
        """
        if not config.delivery_task_queue:
            return []
        channels = config.delivery_channels or ["telegram"]
        attach = bool(rendered.get("attach_audio"))
        audio_path = str(rendered.get("audio_path") or "") or None
        audio_url = str(rendered.get("audio_url") or "")
        text_path = str(generated.get("text_path") or "")
        title = str(generated.get("title") or config.title or "Leccion")
        duration = int(float(rendered.get("duration_seconds") or 0))
        footer = "" if attach or not audio_url else f"\n\n---\nAudio ({duration}s): {audio_url}"
        body = str(generated.get("markdown") or "") + footer
        queue = config.delivery_task_queue
        delivery_status: list[dict[str, Any]] = []

        if "email" in channels and config.email_recipients:
            delivery_status.append(
                await self._try_deliver(
                    "email",
                    "deliver_email_activity",
                    queue,
                    [
                        config.email_recipients,
                        body,
                        config.collection,
                        audio_path if attach else None,
                        [text_path] if text_path else [],
                    ],
                )
            )
        if "telegram" in channels:
            caption = title if attach or not audio_url else f"{title}\nAudio: {audio_url}"
            delivery_status.append(
                await self._try_deliver(
                    "telegram_document",
                    "deliver_telegram_document_activity",
                    queue,
                    [text_path, caption, config.chat_id],
                )
            )
            if attach and audio_path:
                delivery_status.append(
                    await self._try_deliver(
                        "telegram_audio",
                        "deliver_telegram_audio_activity",
                        queue,
                        [audio_path, title, config.chat_id],
                    )
                )
        return delivery_status

    @staticmethod
    async def _try_deliver(
        channel: str, activity_name: str, task_queue: str, args: list[Any]
    ) -> dict[str, Any]:
        """Run one delivery activity on its queue, tolerating its failure.

        A channel outage must not sink the lesson: the class exists on disk
        and the status endpoint still reports FINISHED_OK.

        Args:
            channel: Label used in the failure entry.
            activity_name: Name of the delivery activity (addressed by name
                because it lives on another queue).
            task_queue: Queue hosting the delivery activities.
            args: Positional arguments of the activity.

        Returns:
            The activity result, or a dict with status 'error'.
        """
        try:
            return await workflow.execute_activity(
                activity_name,
                args=args,
                task_queue=task_queue,
                start_to_close_timeout=LESSON_DELIVERY_TIMEOUT,
                retry_policy=LESSON_RETRY_POLICY,
            )
        except (ActivityError, ApplicationError) as exc:
            detail = str(getattr(exc, "cause", None) or exc)
            workflow.logger.warning(f"{channel} delivery failed; lesson kept: {detail}")
            return {"channel": channel, "status": "error", "error": detail}
