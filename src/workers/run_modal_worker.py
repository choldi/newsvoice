"""Temporal worker entry point for the Modal narration queue (Phase 7.8).

Runs a third worker on its own task queue hosting the same narration
activities as the local worker, but holding the Modal credentials and doing
the GPU spend. It is declared in docker-compose behind the ``modal`` profile,
so it is created but not started until a collection opts into a Modal backend.

Example:
    python -m src.workers.run_modal_worker
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
from src.workers import narration_activities
from src.workers.narration_workflows import NarrationWorkflow
from src.workers.run_worker import build_client, configure_logging

logger = structlog.get_logger(__name__)


def worker_activities() -> list[object]:
    """Return every Temporal activity the Modal worker must register.

    Returns:
        Ordered list of activity functions for the Temporal Worker.
    """
    return [
        narration_activities.resolve_narration_script_activity,
        narration_activities.render_narration_activity,
    ]


@asynccontextmanager
async def run_modal_worker(client: Client, task_queue: str | None = None) -> AsyncIterator[Worker]:
    """Context manager that creates and starts the Modal Worker.

    Args:
        client: Connected Temporal client.
        task_queue: Task queue name to poll. Defaults to the configured one.

    Yields:
        The running Modal Worker instance.
    """
    queue = task_queue or settings.narration_modal_task_queue
    with ThreadPoolExecutor(max_workers=settings.narration_max_concurrency) as activity_executor:
        worker = Worker(
            client,
            task_queue=queue,
            workflows=[NarrationWorkflow],
            activities=worker_activities(),
            activity_executor=activity_executor,
        )
        logger.info(
            "starting_modal_worker",
            task_queue=queue,
            concurrency=settings.narration_max_concurrency,
        )
        async with worker:
            yield worker


async def main() -> None:
    """Run DB migrations, start the Modal worker, and run until stopped."""
    configure_logging()
    await run_migrations()
    client: Client = await build_client()
    async with run_modal_worker(client):
        logger.info("modal_worker_running", task_queue=settings.narration_modal_task_queue)
        await asyncio.Event().wait()


if __name__ == "__main__":
    asyncio.run(main())
