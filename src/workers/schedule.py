"""Register the daily digest schedules on the Temporal server.

Creates one Temporal Schedule per collection from `collections.toml`, each
running the digest pipeline every day at the collection's configured time
(HH:MM UTC). If no collections file exists, the legacy behavior is preserved:
a single daily schedule for the "default" collection from `RSS_FEED_URLS`.

Examples:
    python -m src.workers.schedule
    python -m src.workers.schedule --collection tech
    python -m src.workers.schedule --collections-file collections.toml
"""

import argparse
import asyncio
from datetime import timedelta

from temporalio.client import (
    Client,
    RPCError,
    Schedule,
    ScheduleActionStartWorkflow,
    ScheduleAlreadyRunningError,
    ScheduleCalendarSpec,
    ScheduleIntervalSpec,
    ScheduleRange,
    ScheduleSpec,
    ScheduleUpdate,
)

from src.core.collections import CollectionConfig, get_collections
from src.core.config import settings
from src.workers.run_worker import build_client
from src.workers.workflows import (
    CollectionsMonitorWorkflow,
    DigestPipelineWorkflow,
    PipelineConfig,
)

# Schedule id of the recurring collections-config monitor workflow.
MONITOR_SCHEDULE_ID = "collections-config-monitor"


def pipeline_config_for(collection: CollectionConfig, tts_flag: bool | None) -> PipelineConfig:
    """Build the PipelineConfig for a collection.

    Args:
        collection: The collection config.
        tts_flag: Optional CLI override for TTS; None uses the collection's value.

    Returns:
        A PipelineConfig wired from the collection's settings.
    """
    return PipelineConfig(
        feed_urls=collection.feed_urls,
        tts_enabled=collection.tts_enabled if tts_flag is None else tts_flag,
        deliver_audio=collection.deliver_audio,
        podcast_name=collection.podcast_name,
        podcast_behavior=collection.podcast_behavior or settings.tts_podcast_behavior,
        language=collection.language or settings.tts_language,
        voice=collection.voice or settings.tts_voice,
        collection=collection.name,
        prompt_template=collection.prompt_template,
        custom_prompt=collection.custom_prompt,
        chat_id=collection.chat_id,
        email_recipients=collection.email_recipients,
        delivery_channels=collection.delivery_channels,
    )


def build_schedule(collection: CollectionConfig, config: PipelineConfig) -> Schedule:
    """Create the Schedule object for a single collection.

    Args:
        collection: Collection metadata (for its time).
        config: PipelineConfig used as the workflow input.

    Returns:
        A Temporal Schedule starting DigestPipelineWorkflow at the collection's time.
    """
    hour, minute = (int(part) for part in collection.time.split(":"))
    return Schedule(
        action=ScheduleActionStartWorkflow(
            DigestPipelineWorkflow.run,
            config,
            id=f"digest-pipeline-{collection.name}",
            task_queue=settings.temporal_task_queue,
        ),
        spec=ScheduleSpec(
            calendars=[
                ScheduleCalendarSpec(
                    hour=[ScheduleRange(start=hour, end=hour)],
                    minute=[ScheduleRange(start=minute, end=minute)],
                )
            ]
        ),
    )


def monitor_schedule() -> Schedule:
    """Create the recurring collections-config monitor Schedule object.

    Wakes the ``CollectionsMonitorWorkflow`` every configured number of minutes
    so it can re-register the digest schedules when ``collections.toml`` changes.

    Returns:
        A Schedule starting CollectionsMonitorWorkflow on a fixed interval.
    """
    interval_minutes = settings.collections_monitor_interval_minutes
    return Schedule(
        action=ScheduleActionStartWorkflow(
            CollectionsMonitorWorkflow.run,
            None,
            id="collections-monitor",
            task_queue=settings.temporal_task_queue,
        ),
        spec=ScheduleSpec(
            intervals=[
                ScheduleIntervalSpec(every=timedelta(minutes=interval_minutes)),
            ]
        ),
    )


async def create_monitor_schedule(client: Client) -> str:
    """Create (or update) the recurring collections-config monitor schedule.

    Args:
        client: Connected Temporal client.

    Returns:
        The schedule id.

    Raises:
        RPCError: If the create/update call fails.
    """
    schedule = monitor_schedule()
    schedule_id = MONITOR_SCHEDULE_ID
    interval_minutes = settings.collections_monitor_interval_minutes
    try:
        await client.create_schedule(schedule_id, schedule)
        print(f"Created schedule '{schedule_id}' (every {interval_minutes} min UTC)")
    except ScheduleAlreadyRunningError:
        handle = client.get_schedule_handle(schedule_id)
        await handle.update(lambda _: ScheduleUpdate(schedule=schedule))
        print(f"Updated schedule '{schedule_id}' (every {interval_minutes} min UTC)")
    except RPCError as exc:
        print(f"Could not create schedule '{schedule_id}': {exc}")
        print("It may already exist; update handle instead.")
        raise
    return schedule_id


async def create_schedules(
    collections: list[CollectionConfig] | None = None,
    tts_flag: bool | None = None,
) -> list[str]:
    """Create one schedule per collection on the Temporal server.

    Args:
        collections: Collections to schedule. Defaults to get_collections().
        tts_flag: Optional global TTS override for all collections.

    Returns:
        List of created schedule IDs.

    Raises:
        RPCError: If a schedule cannot be created or updated.
    """
    targets = collections if collections is not None else get_collections()
    client = await build_client()
    created: list[str] = []
    for collection in targets:
        config = pipeline_config_for(collection, tts_flag)
        schedule = build_schedule(collection, config)
        schedule_id = f"daily-digest-{collection.name}"
        try:
            handle = await client.create_schedule(schedule_id, schedule)
            print(
                f"Created schedule '{schedule_id}' for collection '{collection.name}' "
                f"(daily at {collection.time} UTC)"
            )
            print(await handle.describe())
        except ScheduleAlreadyRunningError:
            handle = client.get_schedule_handle(schedule_id)
            await handle.update(lambda _: ScheduleUpdate(schedule=schedule))
            print(
                f"Updated schedule '{schedule_id}' for collection '{collection.name}' "
                f"(daily at {collection.time} UTC)"
            )
        except RPCError as exc:
            print(f"Could not create schedule '{schedule_id}': {exc}")
            print("It may already exist; update handle instead.")
            raise
        created.append(schedule_id)
    created.append(await create_monitor_schedule(client))
    return created


async def main() -> None:
    """Create (or update) the per-collection schedules on Temporal."""
    parser = argparse.ArgumentParser(description="Register per-collection digest schedules")
    parser.add_argument(
        "--collection",
        default=None,
        help="Schedule only this collection (by name). Defaults to all collections.",
    )
    parser.add_argument(
        "--collections-file",
        default=None,
        help="Path to collections.toml. Defaults to COLLECTIONS_FILE env config.",
    )
    parser.add_argument(
        "--tts",
        action="store_true",
        default=None,
        help="Force-enable TTS for all scheduled collections (overrides collections.toml)",
    )
    parser.add_argument(
        "--hour",
        type=int,
        default=None,
        help="Legacy time override (hour, UTC) for the implicit default collection.",
    )
    parser.add_argument(
        "--minute",
        type=int,
        default=None,
        help="Legacy time override (minute, UTC) for the implicit default collection.",
    )
    args = parser.parse_args()

    collections = get_collections(args.collections_file)
    if not collections:
        raise SystemExit("No collections configured (add collections.toml).")

    if args.collection:
        collections = [c for c in collections if c.name == args.collection]
        if not collections:
            raise SystemExit(f"Collection '{args.collection}' not found or disabled.")

    if args.hour is not None or args.minute is not None:
        hour = args.hour if args.hour is not None else 8
        minute = args.minute if args.minute is not None else 0
        collections = [
            c.model_copy(update={"time": f"{hour:02d}:{minute:02d}"}) for c in collections
        ]

    await create_schedules(collections, tts_flag=args.tts)


if __name__ == "__main__":
    asyncio.run(main())
