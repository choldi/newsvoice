"""One-shot script to manually trigger a digest pipeline run.

Useful for debugging or back-filling a digest outside the daily schedule.

Example:
    python -m src.workers.trigger --tts
"""

import argparse
import asyncio
import json
from datetime import UTC, datetime

from src.core.config import settings
from src.workers.run_worker import build_client
from src.workers.workflows import DigestPipelineWorkflow, PipelineConfig


async def main() -> None:
    """Start and wait for a manual digest pipeline workflow."""
    parser = argparse.ArgumentParser(description="Run one digest pipeline manually")
    parser.add_argument("--tts", action="store_true", help="Enable TTS audio generation")
    parser.add_argument(
        "--podcast-behavior",
        default=None,
        help="Podcaster behavior prompt. Defaults to TTS_PODCAST_BEHAVIOR env config.",
    )
    parser.add_argument(
        "--feed",
        action="append",
        default=[],
        help="RSS feed URL (repeatable). Defaults to RSS_FEED_URLS env config.",
    )
    args = parser.parse_args()

    feed_urls = args.feed or settings.rss_feed_urls
    if not feed_urls:
        raise SystemExit("No RSS feeds configured (use --feed or RSS_FEED_URLS).")

    config = PipelineConfig(
        feed_urls=feed_urls,
        tts_enabled=args.tts,
        podcast_behavior=args.podcast_behavior or settings.tts_podcast_behavior,
    )
    workflow_id = f"digest-pipeline-manual-{datetime.now(UTC).strftime('%Y%m%d%H%M%S')}"

    client = await build_client()
    result = await client.execute_workflow(
        DigestPipelineWorkflow.run,
        config,
        id=workflow_id,
        task_queue=settings.temporal_task_queue,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
