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
import logging
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

from src.api.models.monitoring import MonitorRunConfig
from src.api.models.narration import NarrationSpec
from src.core.collections import CollectionConfig, get_collections
from src.core.config import settings
from src.workers.podcast_target import podcast_target
from src.workers.run_worker import build_client
from src.workers.workflows import (
    CleanupWorkflow,
    CollectionsMonitorWorkflow,
    DigestPipelineWorkflow,
    PipelineConfig,
    SystemMonitorWorkflow,
    UrlSummarizeConfig,
)

logger = logging.getLogger(__name__)

# Schedule id of the recurring collections-config monitor workflow.
MONITOR_SCHEDULE_ID = "collections-config-monitor"
# Schedule id of the recurring system-monitor workflow (health checks).
SYSTEM_MONITOR_SCHEDULE_ID = "system-monitor"
# Schedule id of the recurring disk-cleanup workflow.
CLEANUP_SCHEDULE_ID = "disk-cleanup"


def _narration_task_queue(spec: NarrationSpec | None) -> str:
    """Pick the narration task queue for a collection.

    Modal backends run on their own queue so the worker holding Modal
    credentials and spend is separately scalable and restartable; everything
    else stays on the local queue.

    Args:
        spec: The collection's narration spec, or None.

    Returns:
        The task queue name.
    """
    return podcast_target(spec.model_dump() if spec else None).task_queue


def _narration_namespace(spec: NarrationSpec | None) -> str:
    """Pick the podcast namespace for a collection.

    Phase 7.9b: podcast generation owns one namespace per backend, so the
    render can never share history with the digest pipeline. The namespace
    travels in the workflow input because a workflow cannot read settings.

    Args:
        spec: The collection's narration spec, or None.

    Returns:
        The namespace name.
    """
    return podcast_target(spec.model_dump() if spec else None).namespace


def pipeline_config_for(collection: CollectionConfig, tts_flag: bool | None) -> PipelineConfig:
    """Build the PipelineConfig for a collection.

    Args:
        collection: The collection config.
        tts_flag: Optional CLI override for TTS; None uses the collection's value.

    Returns:
        A PipelineConfig wired from the collection's settings.
    """
    narration_payload = None
    if collection.narration_enabled and collection.narration_spec:
        narration_payload = {
            "namespace": _narration_namespace(collection.narration_spec),
            "task_queue": _narration_task_queue(collection.narration_spec),
            "spec": collection.narration_spec.model_dump(),
            "strategy": collection.narration_strategy,
            # Phase 7.14: resolved here (workflow-safe) and consumed by the
            # render activity on the narration worker; only when the narration
            # audio would actually be delivered.
            "audio_verify": (
                collection.resolve_audio_verify() if collection.narration_deliver_audio else None
            ),
        }

    # Phase 7.13: drop feeds the operator does not trust before they reach the
    # pipeline. A source below the reliability floor must not be able to
    # influence the digest at all, so it is excluded here rather than scored
    # later. The collections signature keeps every URL so a change in an
    # excluded feed still triggers a re-sync.
    trusted_feed_urls = collection.trusted_feed_urls()
    if len(trusted_feed_urls) != len(collection.feed_urls):
        logger.warning(
            f"Excluding {len(collection.feed_urls) - len(trusted_feed_urls)} "
            f"unreliable feed(s) below min_source_reliability for "
            f"collection '{collection.name}'"
        )

    return PipelineConfig(
        feed_urls=trusted_feed_urls,
        tts_enabled=collection.tts_enabled if tts_flag is None else tts_flag,
        deliver_audio=collection.deliver_audio,
        podcast_name=collection.podcast_name,
        podcast_behavior=collection.podcast_behavior or settings.tts_podcast_behavior,
        language=collection.language or settings.tts_language,
        voice=collection.voice or settings.tts_voice,
        tts_backend=collection.tts_backend or settings.tts_default_backend,
        collection=collection.name,
        prompt_template=collection.prompt_template,
        custom_prompt=collection.custom_prompt,
        chat_id=collection.chat_id,
        email_recipients=collection.email_recipients,
        whatsapp_recipients=collection.whatsapp_recipients,
        whatsapp_group_id=collection.whatsapp_group_id,
        delivery_channels=collection.delivery_channels,
        logic_delivery=(
            collection.logic_delivery.model_dump() if collection.logic_delivery else None
        ),
        narration=narration_payload,
        # Phase 7.14: verification protects delivery, so it only runs when the
        # collection would deliver audio at all.
        audio_verify=(collection.resolve_audio_verify() if collection.deliver_audio else None),
        main_story_count=collection.main_story_count,
        headline_count=collection.headline_count,
        narration_deliver_audio=collection.narration_deliver_audio,
        verify=collection.resolve_llm_verify(),
    )


def url_summarize_config_for(
    collection: CollectionConfig, *, url: str, store: bool, deliver: bool
) -> UrlSummarizeConfig:
    """Build the UrlSummarizeConfig for one single-URL summary (Phase 7.10).

    Everything a workflow cannot read (settings, collection options, the Phase
    7.13 verification payload) is resolved here, outside the workflow.

    Args:
        collection: Collection supplying the prompt and the delivery
            destinations.
        url: Article URL to summarize.
        store: Persist the extracted article in `articles`.
        deliver: Deliver to the collection's configured channels.

    Returns:
        A UrlSummarizeConfig ready to hand to UrlSummarizeWorkflow.
    """
    return UrlSummarizeConfig(
        url=url,
        collection=collection.name,
        store=store,
        deliver=deliver,
        prompt_template=collection.prompt_template,
        custom_prompt=collection.custom_prompt,
        podcast_behavior=collection.podcast_behavior or settings.tts_podcast_behavior,
        language=collection.language or settings.tts_language,
        main_story_count=collection.main_story_count,
        headline_count=collection.headline_count,
        delivery_channels=collection.delivery_channels,
        chat_id=collection.chat_id,
        email_recipients=collection.email_recipients,
        whatsapp_recipients=collection.whatsapp_recipients,
        whatsapp_group_id=collection.whatsapp_group_id,
        verify=collection.resolve_llm_verify(),
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


def system_monitor_schedule() -> Schedule:
    """Create the recurring system-monitor Schedule object.

    Wakes the ``SystemMonitorWorkflow`` every configured number of minutes so it
    can run the health checks, persist their outcomes, and alert when needed.

    Returns:
        A Schedule starting SystemMonitorWorkflow on a fixed interval.
    """
    config = MonitorRunConfig(
        daily_summary=settings.monitor_daily_summary,
        alert_on_run=settings.monitor_alert_on_run,
    )
    return Schedule(
        action=ScheduleActionStartWorkflow(
            SystemMonitorWorkflow.run,
            config,
            id="system-monitor",
            task_queue=settings.temporal_task_queue,
        ),
        spec=ScheduleSpec(
            intervals=[
                ScheduleIntervalSpec(every=timedelta(minutes=settings.monitor_interval_minutes)),
            ]
        ),
    )


async def ensure_monitor_schedules(client: Client) -> list[str]:
    """Create or update every recurring utility schedule.

    Ensures the collections-config monitor, the system-monitor and the
    disk-cleanup schedules exist so a fresh deploy always gets them without a
    manual call. They run on the fixed cadence of their interval settings;
    `create_schedules` updates them whenever the operator re-registers.

    Args:
        client: Connected Temporal client.

    Returns:
        The list of ensured schedule ids.
    """
    return [
        await create_monitor_schedule(client),
        await create_system_monitor_schedule(client),
        await create_cleanup_schedule(client),
    ]


def cleanup_schedule() -> Schedule:
    """Create the recurring disk-cleanup Schedule object.

    Wakes the ``CleanupWorkflow`` every configured number of minutes so it can
    delete temp files and finished audio older than ``cleanup_max_age_days``.

    Returns:
        A Schedule starting CleanupWorkflow on a fixed interval.
    """
    interval_minutes = settings.cleanup_interval_minutes
    return Schedule(
        action=ScheduleActionStartWorkflow(
            CleanupWorkflow.run,
            None,
            id="disk-cleanup",
            task_queue=settings.temporal_task_queue,
        ),
        spec=ScheduleSpec(
            intervals=[
                ScheduleIntervalSpec(every=timedelta(minutes=interval_minutes)),
            ]
        ),
    )


async def create_cleanup_schedule(client: Client) -> str:
    """Create (or update) the recurring disk-cleanup schedule.

    Args:
        client: Connected Temporal client.

    Returns:
        The schedule id.

    Raises:
        RPCError: If the create/update call fails.
    """
    schedule = cleanup_schedule()
    schedule_id = CLEANUP_SCHEDULE_ID
    interval_minutes = settings.cleanup_interval_minutes
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


async def create_system_monitor_schedule(client: Client) -> str:
    """Create (or update) the recurring system-monitor schedule.

    Args:
        client: Connected Temporal client.

    Returns:
        The schedule id.

    Raises:
        RPCError: If the create/update call fails.
    """
    schedule = system_monitor_schedule()
    schedule_id = SYSTEM_MONITOR_SCHEDULE_ID
    interval_minutes = settings.monitor_interval_minutes
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
    created.extend(await ensure_monitor_schedules(client))
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
