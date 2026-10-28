"""Temporal schedule administration for the admin GUI.

Provides schedule reconciliation (``resync_schedules``), fire-and-forget workflow
starts (``trigger_collection``), running-workflow detection, termination, and
execution listing via the Temporal visibility API.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, datetime
from uuid import uuid4

import structlog
from temporalio.client import Client, RPCError, ScheduleHandle, ScheduleUpdate

from src.api.models.narration import NarrationScript, NarrationSpec
from src.api.services.submit import SubmitError
from src.core.collections import CollectionConfig
from src.core.config import get_settings
from src.workers.run_worker import build_client
from src.workers.schedule import build_schedule, pipeline_config_for, url_summarize_config_for

logger = structlog.get_logger(__name__)

SCHEDULE_PREFIX = "daily-digest-"
WORKFLOW_PREFIX = "digest-pipeline-"
DOC_TTS_WORKFLOW_PREFIX = "doc-tts-"
LESSON_WORKFLOW_PREFIX = "lesson-"
AUDIOBOOK_WORKFLOW_PREFIX = "audiobook-"


def new_document_run_id() -> str:
    """Return a fresh workflow id for a document TTS run (Phase 7.11)."""
    return f"{DOC_TTS_WORKFLOW_PREFIX}{uuid4().hex[:12]}"


def new_lesson_run_id() -> str:
    """Return a fresh workflow id for a teacher lesson run (Phase 7.12)."""
    return f"{LESSON_WORKFLOW_PREFIX}{uuid4().hex[:12]}"


def new_audiobook_run_id() -> str:
    """Return a fresh workflow id for an audiobook run."""
    return f"{AUDIOBOOK_WORKFLOW_PREFIX}{uuid4().hex[:12]}"


class TemporalAdminService:
    """Reconciles Temporal schedules with the collections configuration."""

    def __init__(self, client: Client | None = None) -> None:
        """Initialize with an optional pre-connected Temporal client.

        Args:
            client: Connected Temporal Client. Defaults to a client built from settings.
        """
        self._client: Client | None = client

    async def client(self) -> Client:
        """Return a connected Temporal client, connecting lazily if needed.

        Returns:
            A Temporal Client for the configured namespace.
        """
        if self._client is None:
            self._client = await build_client()
        return self._client

    async def schedule_id_for(self, collection: CollectionConfig) -> str:
        """Return the schedule id for a collection name.

        Args:
            collection: Collection config.

        Returns:
            The Temporal schedule id.
        """
        return f"{SCHEDULE_PREFIX}{collection.name}"

    async def list_schedule_ids(self) -> list[str]:
        """List all news-engine schedule ids on the server.

        Returns:
            Sorted list of schedule ids prefixed with 'daily-digest-'.
        """
        client = await self.client()
        ids: list[str] = []
        try:
            schedules = await client.list_schedules()
            async for handle in schedules:
                if handle.id.startswith(SCHEDULE_PREFIX):
                    ids.append(handle.id)
        except RPCError as exc:
            logger.warning("list_schedules_failed", error=str(exc))
        return sorted(ids)

    async def _upsert(self, collection: CollectionConfig) -> str:
        """Create or update the schedule for a single collection.

        Args:
            collection: Collection config to schedule.

        Returns:
            The schedule id.

        Raises:
            RPCError: If the create/update call fails.
        """
        client = await self.client()
        schedule_id = await self.schedule_id_for(collection)
        config = pipeline_config_for(collection, tts_flag=None)
        schedule = build_schedule(collection, config)
        handle: ScheduleHandle = client.get_schedule_handle(schedule_id)
        try:
            await handle.describe()
            await handle.update(lambda _: ScheduleUpdate(schedule=schedule))
            logger.info("schedule_updated", schedule_id=schedule_id)
        except RPCError as exc:
            if exc.status.value != 5:  # grpc STATUS_NOT_FOUND
                raise
            await client.create_schedule(schedule_id, schedule)
            logger.info("schedule_created", schedule_id=schedule_id)
        return schedule_id

    async def resync_schedules(
        self, collections: Iterable[CollectionConfig]
    ) -> dict[str, list[str]]:
        """Reconcile schedules with the given collections.

        Args:
            collections: The complete list of collections (from collections.toml).

        Returns:
            Dict with 'created_or_updated' and 'removed' schedule id lists.
        """
        targets = [c for c in collections if c.enabled]
        target_ids = {f"{SCHEDULE_PREFIX}{c.name}" for c in targets}

        existing = set(await self.list_schedule_ids())
        result: dict[str, list[str]] = {"created_or_updated": [], "removed": []}

        for collection in targets:
            try:
                schedule_id = await self._upsert(collection)
                result["created_or_updated"].append(schedule_id)
            except RPCError as exc:
                logger.error("schedule_upsert_failed", collection=collection.name, error=str(exc))

        for schedule_id in sorted(existing - target_ids):
            if schedule_id.startswith(SCHEDULE_PREFIX):
                try:
                    client = await self.client()
                    handle: ScheduleHandle = client.get_schedule_handle(schedule_id)
                    await handle.delete()
                    result["removed"].append(schedule_id)
                except RPCError as exc:
                    logger.warning(
                        "schedule_delete_failed", schedule_id=schedule_id, error=str(exc)
                    )

        return result

    async def trigger_collection(self, collection: CollectionConfig) -> str:
        """Start a digest pipeline for a collection immediately (fire-and-forget).

        Args:
            collection: Collection config to run now.

        Returns:
            The Temporal workflow id of the started run.
        """
        from src.core.config import settings
        from src.workers.workflows import DigestPipelineWorkflow

        config = pipeline_config_for(collection, tts_flag=None)
        workflow_id = (
            f"digest-pipeline-{collection.name}-{datetime.now(UTC).strftime('%Y%m%d%H%M%S')}"
        )
        client = await self.client()
        await client.start_workflow(
            DigestPipelineWorkflow.run,
            config,
            id=workflow_id,
            task_queue=settings.temporal_task_queue,
        )
        logger.info("workflow_started", workflow_id=workflow_id, collection=collection.name)
        return workflow_id

    async def start_url_summarize(
        self, collection: CollectionConfig, *, url: str, store: bool, deliver: bool
    ) -> str:
        """Start a single-URL summary workflow (Phase 7.10).

        Args:
            collection: Collection supplying the prompt and destinations.
            url: Article URL to summarize.
            store: Persist the extracted article in `articles`.
            deliver: Deliver to the collection's channels.

        Returns:
            The workflow id of the started run.
        """
        from src.core.config import settings as app_settings
        from src.workers.workflows import UrlSummarizeWorkflow

        config = url_summarize_config_for(collection, url=url, store=store, deliver=deliver)
        workflow_id = (
            f"url-summary-{collection.name}-{datetime.now(UTC).strftime('%Y%m%d%H%M%S')}"
            f"-{uuid4().hex[:8]}"
        )
        client = await self.client()
        await client.start_workflow(
            UrlSummarizeWorkflow.run,
            config,
            id=workflow_id,
            task_queue=app_settings.temporal_task_queue,
        )
        logger.info(
            "url_summary_workflow_started",
            workflow_id=workflow_id,
            collection=collection.name,
            url=url,
        )
        return workflow_id

    async def start_document_tts(self, config: object, workflow_id: str) -> str:
        """Start a DocumentTtsWorkflow on the dedicated doc-tts queue (Phase 7.11).

        Args:
            config: DocTTSConfig with the upload path and voice options.
            workflow_id: Id assigned by the caller: the upload, the metadata
                and the audio_jobs row are already keyed by it.

        Returns:
            The workflow id of the started run.
        """
        from src.core.config import settings as app_settings
        from src.workers.doc_tts_workflows import DocumentTtsWorkflow

        client = await self.client()
        await client.start_workflow(
            DocumentTtsWorkflow.run,
            config,
            id=workflow_id,
            task_queue=app_settings.doc_tts_task_queue,
        )
        logger.info("doc_tts_workflow_started", workflow_id=workflow_id)
        return workflow_id

    async def start_lesson(self, config: object, workflow_id: str) -> str:
        """Start a LessonWorkflow on the teacher queue (Phase 7.12)."""
        from src.core.config import settings as app_settings
        from src.workers.lesson_workflows import LessonWorkflow

        client = await self.client()
        await client.start_workflow(
            LessonWorkflow.run,
            config,
            id=workflow_id,
            task_queue=app_settings.teacher_task_queue,
        )
        logger.info("lesson_workflow_started", workflow_id=workflow_id)
        return workflow_id

    async def start_audiobook(self, config: object, workflow_id: str) -> str:
        """Start an AudiobookWorkflow."""
        from src.core.config import settings as app_settings
        from src.workers.audiobook_workflows import AudiobookWorkflow

        client = await self.client()
        queue = getattr(app_settings, "audiobook_task_queue", "news-engine-audiobook-queue")
        await client.start_workflow(
            AudiobookWorkflow.run,
            config,
            id=workflow_id,
            task_queue=queue,
        )
        logger.info("audiobook_workflow_started", workflow_id=workflow_id)
        return workflow_id

    async def start_narration_render(
        self,
        *,
        collection: str,
        spec: NarrationSpec,
        script: NarrationScript | None = None,
        script_id: int | None = None,
        strategy: str = "turns",
        run_id: str = "",
        force: bool = False,
    ) -> dict[str, str]:
        """Start a NarrationWorkflow manually (REST trigger + GUI, Phase 7.12).

        The script is optional: with neither ``script`` nor ``script_id`` the
        workflow generates one from ``run_id``'s digest, which is what the GUI
        "render the latest run" button relies on.

        Args:
            collection: Collection the render belongs to.
            spec: The cast/delivery spec that will be rendered.
            script: Inline script, when the caller has one.
            script_id: Id of a stored script (looked up here, so a missing one
                fails before a workflow is started).
            strategy: 'turns' or 'conversations'.
            run_id: Digest run the script is generated from / attached to.
            force: Render even when NARRATION_ENABLED is false.

        Returns:
            Dict with workflow_id, collection and status.

        Raises:
            SubmitError: 409 when narration is disabled, 404 when the stored
                script does not exist, 502 when Temporal cannot start it.
        """
        settings = get_settings()
        if not settings.narration_enabled and not force:
            raise SubmitError(
                "Narration is disabled (NARRATION_ENABLED=false)",
                status_code=409,
            )
        resolved = script
        if script_id is not None:
            from src.api.db.database import NarrationRepository

            stored = await NarrationRepository().get_script(script_id)
            if stored is None:
                raise SubmitError(f"Script {script_id} not found", status_code=404)
            resolved = stored

        from src.workers.narration_workflows import NarrationConfig, NarrationWorkflow
        from src.workers.podcast_target import podcast_target

        config = NarrationConfig(
            collection=collection,
            spec=spec.model_dump(),
            script=resolved.model_dump() if resolved else None,
            script_id=script_id,
            run_id=run_id,
            strategy=strategy,
        )
        workflow_id = f"narration-{collection}-{datetime.now(UTC).strftime('%Y%m%d%H%M%S')}"
        # Phase 7.9b: podcast generation owns one namespace per backend, so a
        # standalone render is started in the namespace the spec asks for.
        target = podcast_target(spec.model_dump())
        client = await build_client(target.namespace)
        try:
            await client.start_workflow(
                NarrationWorkflow.run,
                config,
                id=workflow_id,
                task_queue=target.task_queue,
            )
        except Exception as exc:
            logger.error(
                "start_narration_render_failed",
                collection=collection,
                error=str(exc),
                exc_info=True,
            )
            raise SubmitError(f"Failed to start narration: {exc}", status_code=502) from exc
        return {"workflow_id": workflow_id, "collection": collection, "status": "started"}

    async def is_collection_running(self, collection_name: str) -> bool:
        """Check if a collection already has a running digest workflow.

        Args:
            collection_name: Name of the collection.

        Returns:
            True if a workflow is currently running for the collection.
        """
        query = 'WorkflowType="DigestPipelineWorkflow" AND ExecutionStatus="Running"'
        prefix = f"{WORKFLOW_PREFIX}{collection_name}-"
        client = await self.client()
        async for info in client.list_workflows(query=query, limit=100):
            if info.id.startswith(prefix):
                return True
        return False

    async def list_executions(self, limit: int = 50) -> list[dict]:
        """List digest pipeline workflow executions from Temporal visibility.

        Args:
            limit: Maximum results to return.

        Returns:
            List of dicts with workflow_id, status, start_time, close_time,
            sorted by start time descending.
        """
        query = 'WorkflowType="DigestPipelineWorkflow"'
        client = await self.client()
        results: list[dict] = []
        async for info in client.list_workflows(query=query, limit=limit * 4):
            results.append(
                {
                    "workflow_id": info.id,
                    "status": info.status.name.lower(),
                    "start_time": info.start_time,
                    "close_time": info.close_time,
                }
            )
        results.sort(key=lambda r: r["start_time"] or datetime.min, reverse=True)
        return results[:limit]

    async def terminate_workflow(self, workflow_id: str, reason: str = "GUI") -> None:
        """Terminate a running workflow.

        Args:
            workflow_id: Temporal workflow id to terminate.
            reason: Termination reason.

        Raises:
            RPCError: If the terminate call fails.
        """
        client = await self.client()
        handle = client.get_workflow_handle(workflow_id)
        await handle.terminate(reason=reason)
        logger.info("workflow_terminated", workflow_id=workflow_id, reason=reason)

    async def execution_error(self, workflow_id: str) -> str | None:
        """Extract the error from a failed/terminated workflow.

        Skips running workflows (their ``result()`` call would block until
        completion). For finished workflows, attempts ``handle.result()`` and
        catches the failure, returning the exception message or None if no
        error can be extracted.

        Args:
            workflow_id: Temporal workflow id.

        Returns:
            Error message string, or None.
        """
        client = await self.client()
        try:
            async for info in client.list_workflows(query=f'WorkflowId="{workflow_id}"', limit=1):
                if info.status.name.upper() == "RUNNING":
                    return None
        except Exception:
            pass
        try:
            handle = client.get_workflow_handle(workflow_id)
            await handle.result()
            return None
        except Exception as exc:
            return str(exc)[:500]
