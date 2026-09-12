"""Register the daily digest schedule on the Temporal server.

Creates a Temporal Schedule that starts the digest pipeline every day at
08:00 UTC (adjust via --hour/--minute or TEMPORAL_SCHEDULE_HOUR env).

Example:
    python -m src.workers.schedule --hour 8 --minute 0
"""

import argparse
import asyncio

from temporalio.client import (
    Schedule,
    ScheduleActionStartWorkflow,
    ScheduleCalendarSpec,
    ScheduleRange,
    ScheduleSpec,
    TemporalServiceError,
)

from src.core.config import settings
from src.workers.run_worker import build_client
from src.workers.workflows import DigestPipelineWorkflow, PipelineConfig


async def main() -> None:
    """Create (or update) the daily-digest schedule on Temporal."""
    parser = argparse.ArgumentParser(description="Register the daily digest schedule")
    parser.add_argument("--hour", type=int, default=8, help="Run hour (UTC)")
    parser.add_argument("--minute", type=int, default=0, help="Run minute (UTC)")
    parser.add_argument(
        "--tts",
        action="store_true",
        help="Enable TTS audio generation in the scheduled runs",
    )
    parser.add_argument(
        "--podcast-behavior",
        default=None,
        help="Podcaster behavior prompt. Defaults to TTS_PODCAST_BEHAVIOR env config.",
    )
    args = parser.parse_args()

    config = PipelineConfig(
        feed_urls=settings.rss_feed_urls,
        tts_enabled=args.tts,
        podcast_behavior=args.podcast_behavior or settings.tts_podcast_behavior,
    )
    schedule = Schedule(
        action=ScheduleActionStartWorkflow(
            DigestPipelineWorkflow.run,
            config,
            id="digest-pipeline-daily",
            task_queue=settings.temporal_task_queue,
        ),
        spec=ScheduleSpec(
            calendars=[
                ScheduleCalendarSpec(
                    hour=ScheduleRange(start=args.hour, end=args.hour),
                    minute=ScheduleRange(start=args.minute, end=args.minute),
                )
            ]
        ),
    )

    client = await build_client()
    schedule_id = "daily-digest"
    try:
        handle = await client.create_schedule(schedule_id, schedule)
        print(f"Created schedule '{schedule_id}' (daily at {args.hour:02d}:{args.minute:02d} UTC)")
        print(await handle.describe())
    except TemporalServiceError as exc:
        print(f"Could not create schedule: {exc}")
        print("It may already exist; delete it or update handle instead.")


if __name__ == "__main__":
    asyncio.run(main())
