"""Temporal workflow definitions for the news-engine pipeline.

Workflows orchestrate the daily digest pipeline. They must be deterministic:
no network I/O, no randomness, no external process calls. All side effects
live in activities.
"""

from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from temporalio import activity, workflow
from temporalio.common import RetryPolicy

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

# Bounded retries for network-dependent activities so a transient failure does
# not replay forever. Deterministic errors (validation etc.) should be raised as
# non-retryable ApplicationErrors instead of relying on the default policy.
NETWORK_RETRY_POLICY = RetryPolicy(maximum_attempts=3)


@dataclass
class PipelineConfig:
    """Input configuration for a single digest pipeline run."""

    feed_urls: list[str] = field(default_factory=list)
    tts_enabled: bool = True
    deliver_audio: bool = True
    podcast_behavior: str = ""
    language: str = ""
    voice: str = ""


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
            retry_policy=NETWORK_RETRY_POLICY,
        )
        if not isinstance(articles, list):
            raise activity.ApplicationError(
                "fetch_rss_activity returned an unexpected shape",
                non_retryable=True,
            )
        workflow.logger.info(f"RSS fetched: {len(articles)} articles")

        new_articles = await workflow.execute_activity(
            filter_new_activity,
            articles,
            start_to_close_timeout=FILTER_TIMEOUT,
            retry_policy=NETWORK_RETRY_POLICY,
        )
        if not isinstance(new_articles, list):
            raise activity.ApplicationError(
                "filter_new_activity returned an unexpected shape",
                non_retryable=True,
            )
        workflow.logger.info(f"Filtering done: {len(new_articles)} new")

        if not new_articles:
            workflow.logger.info("No new articles; skipping digest")
            return {"status": "no_new_articles", "article_count": 0}

        clusters = await workflow.execute_activity(
            cluster_activity,
            new_articles,
            start_to_close_timeout=CLUSTER_TIMEOUT,
            retry_policy=NETWORK_RETRY_POLICY,
        )
        if not isinstance(clusters, list):
            raise activity.ApplicationError(
                "cluster_activity returned an unexpected shape",
                non_retryable=True,
            )
        workflow.logger.info(f"Clustering done: {len(clusters)} clusters")

        digest = await workflow.execute_activity(
            generate_digest_activity,
            {
                "clusters": clusters,
                "podcast_behavior": config.podcast_behavior,
                "language": config.language,
            },
            start_to_close_timeout=DIGEST_TIMEOUT,
            retry_policy=NETWORK_RETRY_POLICY,
        )
        digest_markdown = self._extract_digest_text(digest)
        article_count = self._extract_article_count(digest)
        workflow.logger.info(f"Digest generated for {article_count} articles")

        # Deliver the text digest FIRST; it is the primary deliverable and must
        # not be blocked by (optional) TTS/audio failures.
        text_delivery = await workflow.execute_activity(
            deliver_telegram_message_activity,
            digest_markdown,
            start_to_close_timeout=DELIVERY_TIMEOUT,
            retry_policy=NETWORK_RETRY_POLICY,
        )
        delivery_status: list[dict[str, Any]] = [text_delivery]

        if config.tts_enabled:
            workflow.logger.info("Generating TTS audio")
            try:
                audio = await workflow.execute_activity(
                    generate_tts_activity,
                    {
                        "digest": digest_markdown,
                        "language": config.language,
                        "voice": config.voice,
                    },
                    start_to_close_timeout=TTS_TIMEOUT,
                    retry_policy=NETWORK_RETRY_POLICY,
                )
                if config.deliver_audio and audio.get("audio_path"):
                    audio_delivery = await workflow.execute_activity(
                        deliver_telegram_audio_activity,
                        args=[audio["audio_path"], digest_markdown[:1000]],
                        start_to_close_timeout=DELIVERY_TIMEOUT,
                        retry_policy=NETWORK_RETRY_POLICY,
                    )
                    delivery_status.append(audio_delivery)
                else:
                    delivery_status.append({"status": "error", "error": "no audio generated"})
            except activity.ActivityError as exc:
                workflow.logger.warning(
                    f"TTS/audio step failed; text digest already delivered: {exc}"
                )
                delivery_status.append({"status": "error", "error": str(exc)})

        workflow.logger.info("Pipeline completed")
        return {
            "status": "ok",
            "article_count": article_count,
            "cluster_count": len(clusters),
            "digest": digest_markdown,
            "delivery_status": delivery_status,
        }

    @staticmethod
    def _extract_digest_text(digest: Any) -> str:
        """Return the digest markdown, failing fast on a malformed result.

        Args:
            digest: Dict produced by generate_digest_activity.

        Returns:
            The digest markdown string.

        Raises:
            ApplicationError: If the result is missing the digest text.
        """
        if not isinstance(digest, dict) or not digest.get("digest"):
            raise activity.ApplicationError(
                "generate_digest_activity returned no digest text",
                non_retryable=True,
            )
        return str(digest["digest"])

    @staticmethod
    def _extract_article_count(digest: Any) -> int:
        """Return the article count from a digest result.

        Args:
            digest: Dict produced by generate_digest_activity.

        Returns:
            The number of articles summarized.

        Raises:
            ApplicationError: If the result has no article count.
        """
        if not isinstance(digest, dict) or "article_count" not in digest:
            raise activity.ApplicationError(
                "generate_digest_activity returned no article_count",
                non_retryable=True,
            )
        return int(digest["article_count"])
