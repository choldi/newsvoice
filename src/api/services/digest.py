"""Digest service providing business logic for daily news summarization."""

import structlog

from src.api.db.database import ArticleRepository
from src.api.models.article import ArticleIn, DigestIn, DigestOut
from src.api.services.dedup import normalize_url
from src.core.llm import generate_completion
from src.core.prompts import (
    SUMMARIZE_DIGEST_SYSTEM_PROMPT,
    SUMMARIZE_DIGEST_USER_PROMPT_TEMPLATE,
)

logger = structlog.get_logger()


class DigestService:
    """Service to process RSS articles and generate AI news digest."""

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

    async def generate_digest(self, payload: DigestIn) -> DigestOut:
        """Generates a news digest from incoming articles payload using LLM.

        Args:
            payload: DigestIn containing a list of ArticleIn objects.

        Returns:
            DigestOut containing execution status and generated Markdown text.
        """
        articles = payload.articles
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
            {"role": "system", "content": SUMMARIZE_DIGEST_SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ]

        digest_markdown = await generate_completion(messages=messages, temperature=0.3)

        # Record delivery for delivered articles
        delivered_urls = [normalize_url(a.link) for a in articles if a.link]
        if delivered_urls:
            try:
                await self.repo.record_deliveries(delivered_urls)
                logger.info("deliveries_recorded", count=len(delivered_urls))
            except Exception as exc:
                logger.warning("recording_deliveries_failed", error=str(exc))

        return DigestOut(
            status="ok",
            digest=digest_markdown,
            article_count=len(articles),
        )
