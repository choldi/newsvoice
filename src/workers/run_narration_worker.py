"""Temporal worker entry point for the local podcast namespace (Phase 7.9b).

Runs a worker on the local narration task queue hosting the narration workflow
and activities, so a long multi-speaker render can never starve the digest
pipeline's worker slots and so the narration code deploys independently.

Since Phase 7.9b it serves its own namespace (``news-engine-podcast-local``)
rather than the digest one: a Temporal activity executes in the namespace of
the workflow that scheduled it, so giving podcast generation a namespace per
backend requires the render to be its own workflow, started by the digest
through ``dispatch_narration_activity`` or by the standalone starters.

Example:
    python -m src.workers.run_narration_worker
"""

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
from src.workers import narration_activities
from src.workers.narration_workflows import NarrationWorkflow
from src.workers.run_worker import build_client, configure_logging

logger = structlog.get_logger(__name__)


def worker_activities() -> list[object]:
    """Return every Temporal activity the narration worker must register.

    Each activity added in ``src.workers.narration_activities`` must be
    registered here, otherwise a scheduled narration fails at runtime with a
    NotFoundError.

    Returns:
        Ordered list of activity functions for the Temporal Worker.
    """
    return [
        narration_activities.resolve_narration_script_activity,
        narration_activities.render_narration_activity,
    ]


@asynccontextmanager
async def run_narration_worker(
    client: Client, task_queue: str | None = None
) -> AsyncIterator[Worker]:
    """Context manager that creates and starts the narration Worker.

    Args:
        client: Connected Temporal client.
        task_queue: Task queue name to poll. Defaults to the configured one.

    Yields:
        The running narration Worker instance.
    """
    queue = task_queue or settings.narration_local_task_queue
    with ThreadPoolExecutor(max_workers=settings.narration_max_concurrency) as activity_executor:
        worker = Worker(
            client,
            task_queue=queue,
            workflows=[NarrationWorkflow],
            activities=worker_activities(),
            activity_executor=activity_executor,
        )
        logger.info(
            "starting_narration_worker",
            task_queue=queue,
            concurrency=settings.narration_max_concurrency,
            namespace=settings.temporal_podcast_local_namespace,
        )
        async with worker:
            yield worker


async def main() -> None:
    """Run DB migrations, start the narration worker, and run until stopped."""
    parser = argparse.ArgumentParser(description="Narration worker (Phase 7)")
    parser.add_argument(
        "--queue",
        default=None,
        help="Task queue to poll. Defaults to the configured narration queue.",
    )
    args = parser.parse_args()
    configure_logging()
    await run_migrations()
    # Phase 7.9b: podcast generation owns one namespace per backend. This
    # worker serves the local one; the Modal side has its own worker.
    client: Client = await build_client(settings.temporal_podcast_local_namespace)
    async with run_narration_worker(client, task_queue=args.queue):
        logger.info(
            "narration_worker_running",
            task_queue=args.queue or settings.narration_local_task_queue,
            namespace=settings.temporal_podcast_local_namespace,
        )
        await asyncio.Event().wait()


if __name__ == "__main__":
    asyncio.run(main())
