"""Temporal worker entry point for the document TTS queue (Phase 7.11).

Runs a worker on the doc-tts task queue hosting :class:`DocumentTtsWorkflow`,
so a 300-page render holds its own slots and can never delay the digest
pipeline's worker. It serves the **default** namespace (same as the digest):
the API that accepted the upload schedules the workflow there, and an activity
always executes in the namespace of the workflow that scheduled it.

Example:
    python -m src.workers.run_doc_tts_worker
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
from src.workers import doc_tts_activities
from src.workers.doc_tts_workflows import DocumentTtsWorkflow
from src.workers.run_worker import build_client, configure_logging

logger = structlog.get_logger(__name__)


def worker_activities() -> list[object]:
    """Return every Temporal activity the doc-tts worker must register.

    Each activity added in ``src.workers.doc_tts_activities`` must be
    registered here, otherwise the workflow step invoking it fails at runtime
    with a Temporal NotFoundError. ``tests/unit/test_run_doc_tts_worker.py``
    mirrors ``test_run_worker.py`` to keep both sets in sync.

    Returns:
        Ordered list of activity functions for the Temporal Worker.
    """
    return [
        doc_tts_activities.extract_document_activity,
        doc_tts_activities.synthesize_document_activity,
    ]


@asynccontextmanager
async def run_doc_tts_worker(
    client: Client, task_queue: str | None = None
) -> AsyncIterator[Worker]:
    """Context manager that creates and starts the doc-tts Worker.

    Args:
        client: Connected Temporal client.
        task_queue: Task queue to poll. Defaults to the configured one.

    Yields:
        The running doc-tts Worker instance.
    """
    queue = task_queue or settings.doc_tts_task_queue
    with ThreadPoolExecutor(max_workers=2) as activity_executor:
        worker = Worker(
            client,
            task_queue=queue,
            workflows=[DocumentTtsWorkflow],
            activities=worker_activities(),
            activity_executor=activity_executor,
        )
        logger.info("starting_doc_tts_worker", task_queue=queue)
        async with worker:
            yield worker


async def main() -> None:
    """Run DB migrations, start the doc-tts worker, and run until stopped."""
    parser = argparse.ArgumentParser(description="Document TTS worker (Phase 7.11)")
    parser.add_argument(
        "--queue",
        default=None,
        help="Task queue to poll. Defaults to the configured doc-tts queue.",
    )
    args = parser.parse_args()
    configure_logging()
    await run_migrations()
    client: Client = await build_client()
    async with run_doc_tts_worker(client, task_queue=args.queue):
        logger.info("doc_tts_worker_running", task_queue=args.queue or settings.doc_tts_task_queue)
        await asyncio.Event().wait()


if __name__ == "__main__":
    asyncio.run(main())
