"""Temporal worker entry point.

Runs a Temporal worker that hosts the news-engine workflows and activities.
Connect to a Temporal server (local dev via docker-compose, or remote).

Example:
    python -m src.workers.run_worker
"""

import asyncio
from collections.abc import AsyncIterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager

import structlog
from temporalio.client import Client
from temporalio.worker import Worker

from src.api.db.database import run_migrations
from src.core.config import settings
from src.workers import activities
from src.workers.client import build_client, configure_logging
from src.workers.workflows import (
    CollectionsMonitorWorkflow,
    DigestPipelineWorkflow,
    SystemMonitorWorkflow,
)

logger = structlog.get_logger(__name__)

__all__ = ["build_client", "configure_logging", "run_worker", "worker_activities"]


def worker_activities() -> list[object]:
    """Return every Temporal activity the worker must register.

    Each activity added in ``src.workers.activities`` must be registered here,
    otherwise the matching workflow step fails at runtime with a NotFoundError
    when a scheduled digest tries to run it.

    Returns:
        Ordered list of activity functions for the Temporal Worker.
    """
    return [
        activities.fetch_rss_activity,
        activities.filter_new_activity,
        activities.cluster_activity,
        activities.generate_digest_activity,
        activities.generate_logic_activity,
        activities.generate_tts_activity,
        activities.compress_audio_activity,
        activities.deliver_telegram_message_activity,
        activities.deliver_telegram_audio_activity,
        activities.deliver_email_activity,
        activities.deliver_whatsapp_message_activity,
        activities.deliver_whatsapp_audio_activity,
        activities.deliver_logic_activity,
        activities.record_run_activity,
        activities.dispatch_narration_activity,
        activities.read_collections_signature_activity,
        activities.get_synced_signature_activity,
        activities.sync_collections_schedules_activity,
        activities.collections_changes_activity,
        activities.get_monitor_state_activity,
        activities.set_monitor_state_activity,
        activities.run_monitor_checks_activity,
        activities.send_monitor_alert_activity,
    ]


@asynccontextmanager
async def run_worker(
    client: Client, task_queue: str = "news-engine-task-queue"
) -> AsyncIterator[Worker]:
    """Context manager that creates and starts a Temporal Worker.

    Args:
        client: Connected Temporal client.
        task_queue: Task queue name to poll for tasks. Defaults to configured queue.

    Yields:
        The running Worker instance.
    """
    queue = task_queue or settings.temporal_task_queue
    with ThreadPoolExecutor(max_workers=8) as activity_executor:
        worker = Worker(
            client,
            task_queue=queue,
            workflows=[
                DigestPipelineWorkflow,
                CollectionsMonitorWorkflow,
                SystemMonitorWorkflow,
            ],
            activities=worker_activities(),
            activity_executor=activity_executor,
        )
        logger.info("starting_worker", task_queue=queue)
        async with worker:
            yield worker


async def main() -> None:
    """Run DB migrations, start the worker, and run until stopped."""
    configure_logging()
    await run_migrations()
    client = await build_client()
    async with run_worker(client):
        logger.info("worker_running")
        await asyncio.Event().wait()


if __name__ == "__main__":
    asyncio.run(main())
