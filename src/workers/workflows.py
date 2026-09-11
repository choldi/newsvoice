"""Temporal workflow definitions for the news-engine pipeline.

Workflows orchestrate the daily digest pipeline. They must be deterministic:
no network I/O, no randomness, no external process calls. All side effects
live in activities.
"""

from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from temporalio import workflow

with workflow.unsafe.imports_passed_through():
    from src.workers.activities import (
        cluster_activity,
        deliver_telegram_audio_activity,
        deliver_telegram_message_activity,
        fetch_rss_activity,
        filter_new_activity,
        generate_digest_activity,
        generate_tts_activity,
    )

# Reasonable per-step timeouts for activity execution.
RSS_FETCH_TIMEOUT = timedelta(seconds=120)
FILTER_TIMEOUT = timedelta(seconds=30)
CLUSTER_TIMEOUT = timedelta(seconds=300)
DIGEST_TIMEOUT = timedelta(seconds=300)
TTS_TIMEOUT = timedelta(seconds=180)
DELIVERY_TIMEOUT = timedelta(seconds=60)


@dataclass
class PipelineConfig:
    """Input configuration for a single digest pipeline run."""

    feed_urls: list[str] = field(default_factory=list)
    tts_enabled: bool = True
    deliver_audio: bool = True


@workflow.defn
class DigestPipelineWorkflow:
    """Daily digest pipeline: fetch -> dedup -> cluster -> summarize -> deliver."""

    @workflow.run
    async def run(self, config: PipelineConfig) -> dict[str, Any]:
        """Execute the full digest pipeline.

        Args:
            config: PipelineConfig with feed URLs and delivery options.

        Returns:
            Dict summarizing the run outcome (digest, counts, delivery status).
        """
        workflow.logger.info(f"Pipeline started with {len(config.feed_urls)} feeds")

        articles = await workflow.execute_activity(
            fetch_rss_activity,
            config.feed_urls,
            start_to_close_timeout=RSS_FETCH_TIMEOUT,
        )
        workflow.logger.info(f"RSS fetched: {len(articles)} articles")

        new_articles = await workflow.execute_activity(
            filter_new_activity,
            articles,
            start_to_close_timeout=FILTER_TIMEOUT,
        )
        workflow.logger.info(f"Filtering done: {len(new_articles)} new")

        if not new_articles:
            workflow.logger.info("No new articles; skipping digest")
            return {"status": "no_new_articles", "article_count": 0}

        clusters = await workflow.execute_activity(
            cluster_activity,
            new_articles,
            start_to_close_timeout=CLUSTER_TIMEOUT,
        )
        workflow.logger.info(f"Clustering done: {len(clusters)} clusters")

        digest = await workflow.execute_activity(
            generate_digest_activity,
            {"clusters": clusters},
            start_to_close_timeout=DIGEST_TIMEOUT,
        )
        workflow.logger.info(f"Digest generated for {digest['article_count']} articles")

        delivery_status: list[dict[str, Any]] = []

        if config.tts_enabled:
            workflow.logger.info("Generating TTS audio")
            audio = await workflow.execute_activity(
                generate_tts_activity,
                digest,
                start_to_close_timeout=TTS_TIMEOUT,
            )
            if config.deliver_audio and audio.get("audio_path"):
                audio_delivery = await workflow.execute_activity(
                    deliver_telegram_audio_activity,
                    args=[audio["audio_path"], digest["digest"][:1000]],
                    start_to_close_timeout=DELIVERY_TIMEOUT,
                )
                delivery_status.append(audio_delivery)

        text_delivery = await workflow.execute_activity(
            deliver_telegram_message_activity,
            digest["digest"],
            start_to_close_timeout=DELIVERY_TIMEOUT,
        )
        delivery_status.append(text_delivery)

        workflow.logger.info("Pipeline completed")
        return {
            "status": "ok",
            "article_count": digest["article_count"],
            "cluster_count": len(clusters),
            "digest": digest["digest"],
            "delivery_status": delivery_status,
        }
