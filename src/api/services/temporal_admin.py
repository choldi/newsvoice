"""Temporal schedule administration for the admin GUI.

Provides `resync_schedules` which reconciles the Temporal server's `daily-digest-*`
schedules with the current collections.toml configuration: schedules are created
for new collections, updated for existing ones, and deleted for removed ones.
"""

from __future__ import annotations

from collections.abc import Iterable

import structlog
from temporalio.client import Client, RPCError, ScheduleHandle

from src.core.collections import CollectionConfig
from src.workers.run_worker import build_client
from src.workers.schedule import build_schedule, pipeline_config_for

logger = structlog.get_logger(__name__)

SCHEDULE_PREFIX = "daily-digest-"


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
        """Start a digest pipeline for a collection immediately (outside its schedule).

        Args:
            collection: Collection config to run now.

        Returns:
            The Temporal workflow id of the started run.
        """
        from datetime import UTC, datetime

        from src.core.config import settings
        from src.workers.workflows import DigestPipelineWorkflow

        config = pipeline_config_for(collection, tts_flag=None)
        workflow_id = (
            f"digest-pipeline-{collection.name}-{datetime.now(UTC).strftime('%Y%m%d%H%M%S')}"
        )
        client = await self.client()
        await client.execute_workflow(
            DigestPipelineWorkflow.run,
            config,
            id=workflow_id,
            task_queue=settings.temporal_task_queue,
        )
        return workflow_id
