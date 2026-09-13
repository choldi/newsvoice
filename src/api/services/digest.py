"""Digest service providing business logic for daily news summarization."""

import structlog

from src.api.db.database import ArticleRepository
from src.api.models.article import ArticleIn, DigestIn, DigestOut
from src.api.models.cluster import ClusterItem
from src.api.services.dedup import normalize_url
from src.core.config import settings
from src.core.llm import generate_completion
from src.core.prompts import (
    SUMMARIZE_DIGEST_USER_PROMPT_TEMPLATE,
    build_digest_system_prompt,
)

logger = structlog.get_logger()


class DigestService:
    """Service to process RSS articles / clusters and generate AI news digest."""

    def __init__(self, repository: ArticleRepository | None = None) -> None:
        """Initializes the DigestService with an ArticleRepository instance.

        Args:
            repository: Optional ArticleRepository instance. Defaults to new instance.
        """
        self.repo = repository or ArticleRepository()

    def _format_articles(self, articles: list[ArticleIn]) -> str:
        """Formats a list of articles into a structured text string for the prompt.

        Args:
            articles: List of ArticleIn Pydantic instances.

        Returns:
            Formatted string of articles.
        """
        formatted_blocks: list[str] = []
        for i, article in enumerate(articles, 1):
            source_str = f" Source: {article.source}" if article.source else ""
            pub_date_str = f" Date: {article.pub_date}" if article.pub_date else ""
            content_str = f"\nSummary/Content: {article.content}" if article.content else ""
            block = (
                f"[{i}] Title: {article.title}\n"
                f"Link: {article.link}{source_str}{pub_date_str}{content_str}"
            )
            formatted_blocks.append(block)
        return "\n\n".join(formatted_blocks)

    def _format_clusters(self, clusters: list[ClusterItem]) -> str:
        """Formats a list of cluster groups into a structured text string for the prompt.

        Args:
            clusters: List of ClusterItem instances.

        Returns:
            Formatted string of story clusters.
        """
        formatted_blocks: list[str] = []
        for i, cluster in enumerate(clusters, 1):
            articles_info = []
            for art in cluster.articles:
                src = f" ({art.source})" if art.source else ""
                snippet = f" - {art.content[:150]}" if art.content else ""
                articles_info.append(f"  * {art.title}{src}: {art.link}{snippet}")

            joined_articles = "\n".join(articles_info)
            block = (
                f"[Story Cluster #{i}] Entity: {cluster.fingerprint.entity} | "
                f"Action: {cluster.fingerprint.action}\n"
                f"Related Coverage ({len(cluster.articles)} sources):\n"
                f"{joined_articles}"
            )
            formatted_blocks.append(block)
        return "\n\n".join(formatted_blocks)

    def _system_prompt(self, payload: DigestIn) -> str:
        """Build the digest system prompt, injecting template, behavior, and language.

        Args:
            payload: The DigestIn payload, which may carry template/behavior/language.

        Returns:
            The system prompt to use for digest generation.
        """
        behavior = payload.podcast_behavior or settings.tts_podcast_behavior
        language = payload.language or settings.tts_language
        template = payload.prompt_template or "default"
        return build_digest_system_prompt(behavior, language, template=template)

    async def generate_digest(self, payload: DigestIn) -> DigestOut:
        """Generates a news digest from incoming articles or clusters payload using LLM.

        Args:
            payload: DigestIn containing either articles or clusters.

        Returns:
            DigestOut containing execution status and generated Markdown text.
        """
        clusters = payload.clusters
        articles = payload.articles

        if clusters:
            # Filter only clusters that are material updates
            actionable_clusters = [c for c in clusters if c.is_material_update]
            if not actionable_clusters:
                actionable_clusters = clusters

            logger.info("processing_digest_clusters", count=len(actionable_clusters))
            stories_text = self._format_clusters(actionable_clusters)
            total_items = sum(len(c.articles) for c in actionable_clusters)

            user_prompt = SUMMARIZE_DIGEST_USER_PROMPT_TEMPLATE.format(articles_text=stories_text)
            messages = [
                {"role": "system", "content": self._system_prompt(payload)},
                {"role": "user", "content": user_prompt},
            ]

            digest_markdown = await generate_completion(messages=messages, temperature=0.3)

            # Record cluster deliveries
            for cluster in actionable_clusters:
                if cluster.cluster_id:
                    try:
                        await self.repo.record_cluster_delivery(
                            cluster.cluster_id,
                            material_update=cluster.is_material_update,
                            collection=payload.collection,
                        )
                    except Exception as exc:
                        logger.warning("record_cluster_delivery_failed", error=str(exc))

            return DigestOut(
                status="ok",
                digest=digest_markdown,
                article_count=total_items,
            )

        if not articles:
            logger.info("empty_articles_payload_received")
            return DigestOut(
                status="ok",
                digest="No articles provided in payload to generate digest.",
                article_count=0,
            )

        logger.info("processing_digest_articles", count=len(articles))
        articles_text = self._format_articles(articles)
        user_prompt = SUMMARIZE_DIGEST_USER_PROMPT_TEMPLATE.format(articles_text=articles_text)

        messages = [
            {"role": "system", "content": self._system_prompt(payload)},
            {"role": "user", "content": user_prompt},
        ]

        digest_markdown = await generate_completion(messages=messages, temperature=0.3)

        # Record delivery for delivered articles
        delivered_urls = [normalize_url(a.link) for a in articles if a.link]
        if delivered_urls:
            try:
                await self.repo.record_deliveries(delivered_urls, collection=payload.collection)
                logger.info("deliveries_recorded", count=len(delivered_urls))
            except Exception as exc:
                logger.warning("recording_deliveries_failed", error=str(exc))

        return DigestOut(
            status="ok",
            digest=digest_markdown,
            article_count=len(articles),
        )
