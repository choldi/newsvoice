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

from src.core.config import settings
from src.workers import activities

logger = structlog.get_logger(__name__)


async def build_client() -> Client:
    """Connect to the Temporal server.

    Returns:
        A Temporal Client connected to the configured server/namespace.
    """
    logger.info(
        "connecting_to_temporal",
        host=settings.temporal_host,
        namespace=settings.temporal_namespace,
    )
    return await Client.connect(
        settings.temporal_host,
        namespace=settings.temporal_namespace,
    )


@asynccontextmanager
async def run_worker(
    client: Client, task_queue: str = "news-engine-task-queue"
) -> AsyncIterator[Worker]:
    """Context manager that creates and starts a Temporal Worker.

    Args:
        client: Connected Temporal Client.
        task_queue: Task queue name to poll for tasks. Defaults to configured queue.

    Yields:
        The running Worker instance.
    """
    queue = task_queue or settings.temporal_task_queue
    with ThreadPoolExecutor(max_workers=8) as activity_executor:
        worker = Worker(
            client,
            task_queue=queue,
            workflows=[],
            activities=[
                activities.fetch_rss_activity,
                activities.filter_new_activity,
                activities.cluster_activity,
                activities.generate_digest_activity,
                activities.generate_tts_activity,
                activities.deliver_telegram_message_activity,
                activities.deliver_telegram_audio_activity,
            ],
            activity_executor=activity_executor,
        )
        logger.info("starting_worker", task_queue=queue)
        async with worker:
            yield worker


async def main() -> None:
    """Start the worker and run until stopped."""
    client = await build_client()
    async with run_worker(client):
        logger.info("worker_running")
        await asyncio.Event().wait()


if __name__ == "__main__":
    asyncio.run(main())
