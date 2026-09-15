"""Temporal schedule administration for the admin GUI.

Provides schedule reconciliation (``resync_schedules``), fire-and-forget workflow
starts (``trigger_collection``), running-workflow detection, termination, and
execution listing via the Temporal visibility API.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, datetime

import structlog
from temporalio.client import Client, RPCError, ScheduleHandle

from src.core.collections import CollectionConfig
from src.workers.run_worker import build_client
from src.workers.schedule import build_schedule, pipeline_config_for

logger = structlog.get_logger(__name__)

SCHEDULE_PREFIX = "daily-digest-"
WORKFLOW_PREFIX = "digest-pipeline-"


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
            async for handle in client.list_schedules():
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
        handle: ScheduleHandle = await client.get_schedule_handle(schedule_id)
        try:
            await handle.describe()
            await handle.update(schedule)
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
                    handle: ScheduleHandle = await client.get_schedule_handle(schedule_id)
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
