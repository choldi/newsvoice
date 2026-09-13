"""One-shot script to manually trigger a digest pipeline run.

Useful for debugging or back-filling a digest outside the daily schedule.

Examples:
    python -m src.workers.trigger --collection tech
    python -m src.workers.trigger --tts --feed https://example.com/rss.xml
"""

import argparse
import asyncio
import json
from datetime import UTC, datetime

from src.core.collections import CollectionConfig, get_collection
from src.core.config import settings
from src.workers.run_worker import build_client
from src.workers.schedule import pipeline_config_for
from src.workers.workflows import DigestPipelineWorkflow, PipelineConfig


def _feed_override(args: argparse.Namespace) -> list[str]:
    """Compute the effective feed URLs from CLI args or settings.

    Args:
        args: Parsed CLI arguments.

    Returns:
        List of feed URLs to use.
    """
    return args.feed or settings.rss_feed_urls


def _config_for_collection(collection: CollectionConfig, tts_flag: bool) -> PipelineConfig:
    """Build a PipelineConfig for a collection, forcing TTS via the CLI flag.

    Args:
        collection: The collection config.
        tts_flag: True/False from the --tts CLI flag.

    Returns:
        A PipelineConfig wired from the collection's settings.
    """
    return pipeline_config_for(collection, tts_flag=tts_flag)


async def main() -> None:
    """Start and wait for a manual digest pipeline workflow."""
    parser = argparse.ArgumentParser(description="Run one digest pipeline manually")
    parser.add_argument(
        "--collection",
        default=None,
        help="Collection name (from collections.toml) to run.",
    )
    parser.add_argument("--tts", action="store_true", help="Enable TTS audio generation")
    parser.add_argument(
        "--podcast-behavior",
        default=None,
        help="Podcaster behavior prompt. Defaults to TTS_PODCAST_BEHAVIOR env config.",
    )
    parser.add_argument(
        "--language",
        default=None,
        help="ISO 639-1 language code for this podcast. Defaults to TTS_LANGUAGE env config.",
    )
    parser.add_argument(
        "--voice",
        default=None,
        help="OmniVoice voice for this podcast. Defaults to TTS_VOICE env config.",
    )
    parser.add_argument(
        "--feed",
        action="append",
        default=[],
        help="RSS feed URL (repeatable). Defaults to RSS_FEED_URLS env config.",
    )
    args = parser.parse_args()

    if args.collection:
        collection = get_collection(args.collection)
        if collection is None:
            raise SystemExit(f"Collection '{args.collection}' not found in collections.toml.")
        config = _config_for_collection(collection, tts_flag=args.tts)
        if args.language:
            config.language = args.language
        if args.voice:
            config.voice = args.voice
        if args.podcast_behavior:
            config.podcast_behavior = args.podcast_behavior
    else:
        feed_urls = _feed_override(args)
        if not feed_urls:
            raise SystemExit("No RSS feeds configured (use --feed or RSS_FEED_URLS).")
        config = PipelineConfig(
            feed_urls=feed_urls,
            tts_enabled=args.tts,
            podcast_behavior=args.podcast_behavior or settings.tts_podcast_behavior,
            language=args.language or settings.tts_language,
            voice=args.voice or settings.tts_voice,
            collection="default",
            prompt_template="default",
        )

    workflow_id = (
        f"digest-pipeline-{config.collection}-{datetime.now(UTC).strftime('%Y%m%d%H%M%S')}"
    )

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
