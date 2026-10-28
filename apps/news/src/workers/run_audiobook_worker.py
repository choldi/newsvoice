"""Temporal worker entry point for the audiobook queue."""

from __future__ import annotations

import argparse
import asyncio
from collections.abc import AsyncIterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager

import structlog
from temporalio.client import Client
from temporalio.worker import Worker

from src.api.db.database import run_migrations
from src.core.config import settings
from src.workers import audiobook_activities
from src.workers.audiobook_workflows import AudiobookWorkflow
from src.workers.run_worker import build_client, configure_logging

logger = structlog.get_logger(__name__)


def worker_activities() -> list[object]:
    return [
        audiobook_activities.extract_audiobook_activity,
        audiobook_activities.render_audiobook_activity,
    ]


@asynccontextmanager
async def run_audiobook_worker(
    client: Client, task_queue: str | None = None
) -> AsyncIterator[Worker]:
    queue = task_queue or getattr(settings, "audiobook_task_queue", "news-engine-audiobook-queue")
    with ThreadPoolExecutor(max_workers=2) as activity_executor:
        worker = Worker(
            client,
            task_queue=queue,
            workflows=[AudiobookWorkflow],
            activities=worker_activities(),
            activity_executor=activity_executor,
        )
        logger.info("starting_audiobook_worker", task_queue=queue)
        async with worker:
            yield worker


async def main() -> None:
    parser = argparse.ArgumentParser(description="Audiobook worker")
    parser.add_argument("--queue", default=None)
    args = parser.parse_args()
    configure_logging()
    await run_migrations()
    client: Client = await build_client()
    async with run_audiobook_worker(client, task_queue=args.queue):
        await asyncio.Event().wait()


if __name__ == "__main__":
    asyncio.run(main())
