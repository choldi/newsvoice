"""Temporal worker entry point for the language-teacher queue (Phase 7.12).

Runs a worker on the teacher task queue hosting :class:`LessonWorkflow`: an
LLM call plus a full narration render must never occupy the digest worker's
slots. It serves the **default** namespace (same as the digest): the API that
accepted the upload schedules the workflow there, and an activity always
executes in the namespace of the workflow that scheduled it. Deliveries are
not registered here: the workflow addresses them by name on the digest queue.

Example:
    python -m src.workers.run_teacher_worker
"""

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
from src.workers import lesson_activities
from src.workers.lesson_workflows import LessonWorkflow
from src.workers.run_worker import build_client, configure_logging

logger = structlog.get_logger(__name__)


def worker_activities() -> list[object]:
    """Return every Temporal activity the teacher worker must register.

    Each activity added in ``src.workers.lesson_activities`` must be
    registered here, otherwise the workflow step invoking it fails at runtime
    with a Temporal NotFoundError. ``tests/unit/test_run_teacher_worker.py``
    mirrors ``test_run_worker.py`` to keep both sets in sync.

    Returns:
        Ordered list of activity functions for the Temporal Worker.
    """
    return [
        lesson_activities.generate_lesson_activity,
        lesson_activities.render_lesson_activity,
    ]


@asynccontextmanager
async def run_teacher_worker(
    client: Client, task_queue: str | None = None
) -> AsyncIterator[Worker]:
    """Context manager that creates and starts the teacher Worker.

    Args:
        client: Connected Temporal client.
        task_queue: Task queue to poll. Defaults to the configured one.

    Yields:
        The running teacher Worker instance.
    """
    queue = task_queue or settings.teacher_task_queue
    with ThreadPoolExecutor(max_workers=2) as activity_executor:
        worker = Worker(
            client,
            task_queue=queue,
            workflows=[LessonWorkflow],
            activities=worker_activities(),
            activity_executor=activity_executor,
        )
        logger.info("starting_teacher_worker", task_queue=queue)
        async with worker:
            yield worker


async def main() -> None:
    """Run DB migrations, start the teacher worker, and run until stopped."""
    parser = argparse.ArgumentParser(description="Teacher worker (Phase 7.12)")
    parser.add_argument(
        "--queue",
        default=None,
        help="Task queue to poll. Defaults to the configured teacher queue.",
    )
    args = parser.parse_args()
    configure_logging()
    await run_migrations()
    client: Client = await build_client()
    async with run_teacher_worker(client, task_queue=args.queue):
        logger.info(
            "teacher_worker_running",
            task_queue=args.queue or settings.teacher_task_queue,
        )
        await asyncio.Event().wait()


if __name__ == "__main__":
    asyncio.run(main())
