"""Temporal worker entry point for the voice catalog namespace (Phase 7.9).

The whole voice flow lives in its own namespace because a Temporal activity
always executes in the namespace of the workflow that scheduled it: the design
step cannot be split into a namespace of its own, so the workflow, the
store/status activities and the design activity share one.

Two roles, two queues, two machines:

* ``--role design`` runs on the GPU box and registers only
  ``design_voice_activity`` on the voice-design queue. It holds the Qwen3-TTS
  model and neither the database nor ``data/voices``.
* ``--role catalog`` runs on the server and registers the workflow plus the
  status/store/upload activities on the voice-catalog queue. It owns
  ``data/voices`` and no GPU.

Neither role touches the database, so neither runs migrations.

Example:
    python -m src.workers.run_voice_worker --role design
    python -m src.workers.run_voice_worker --role catalog
"""

from __future__ import annotations

import argparse
import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import structlog
from temporalio.client import Client
from temporalio.worker import Worker

from src.core.config import settings
from src.workers import voice_activities
from src.workers.client import build_client, configure_logging
from src.workers.voice_workflows import VoiceCatalogWorkflow

logger = structlog.get_logger(__name__)

#: Roles this entry point can serve.
ROLES = ("design", "catalog")


def registration(role: str) -> tuple[str, list[object], list[object]]:
    """Return the task queue, workflows and activities for a role.

    Args:
        role: One of :data:`ROLES`.

    Returns:
        Tuple of ``(task_queue, workflows, activities)``.

    Raises:
        ValueError: When the role is unknown.
    """
    if role == "design":
        return (
            settings.voice_design_task_queue,
            [],
            [voice_activities.design_voice_activity],
        )
    if role == "catalog":
        return (
            settings.voice_catalog_task_queue,
            [VoiceCatalogWorkflow],
            [
                voice_activities.voice_catalog_status_activity,
                voice_activities.store_voice_activity,
                voice_activities.upload_voice_activity,
            ],
        )
    raise ValueError(f"unknown voice worker role: {role!r}")


@asynccontextmanager
async def run_voice_worker(
    client: Client, role: str, task_queue: str | None = None
) -> AsyncIterator[Worker]:
    """Context manager that creates and starts a voice Worker.

    Args:
        client: Connected Temporal client in the voices namespace.
        role: One of :data:`ROLES`.
        task_queue: Override for the role's queue.

    Yields:
        The running voice Worker instance.

    Raises:
        ValueError: When the role is unknown.
    """
    queue_name, workflows, activities = registration(role)
    queue = task_queue or queue_name
    worker = Worker(
        client,
        task_queue=queue,
        workflows=workflows,
        activities=activities,
    )
    logger.info(
        "starting_voice_worker",
        role=role,
        task_queue=queue,
        namespace=settings.temporal_voices_namespace,
    )
    async with worker:
        yield worker


async def main() -> None:
    """Start a voice worker for the requested role, and run until stopped."""
    parser = argparse.ArgumentParser(description="Voice catalog worker (Phase 7.9)")
    parser.add_argument(
        "--role",
        required=True,
        choices=ROLES,
        help="design: synthesize on the GPU box. catalog: host the workflow and store.",
    )
    parser.add_argument("--queue", default=None, help="Override the role's task queue.")
    args = parser.parse_args()
    configure_logging()
    client: Client = await build_client(settings.temporal_voices_namespace)
    async with run_voice_worker(client, role=args.role, task_queue=args.queue):
        logger.info("voice_worker_running", role=args.role)
        await asyncio.Event().wait()


if __name__ == "__main__":
    asyncio.run(main())
