from src.core.tracing import trace_function

"""Deduplication and URL normalization service."""

from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

import structlog

from src.api.db.database import ArticleRepository
from src.api.models.article import ArticleIn

logger = structlog.get_logger()

# Set of tracking / marketing query parameters to remove
TRACKING_PARAMS: set[str] = {
    "utm_source",
    "utm_medium",
    "utm_campaign",
    "utm_term",
    "utm_content",
    "utm_id",
    "fbclid",
    "gclid",
    "msclkid",
    "dclid",
    "ref",
    "ref_src",
    "source",
    "ysclid",
    "mc_cid",
    "mc_eid",
    "ncid",
    "igshid",
}


def normalize_url(raw_url: str) -> str:
    """Normalizes a URL by removing tracking query parameters, fragments, and standardizing casing.

    Args:
        raw_url: The raw URL string from the RSS feed.

    Returns:
        A canonicalized URL string.
    """
    if not raw_url:
        return ""

    parsed = urlparse(raw_url.strip())
    scheme = parsed.scheme.lower()
    netloc = parsed.netloc.lower()

    # Remove standard port specifications
    if (scheme == "http" and netloc.endswith(":80")) or (
        scheme == "https" and netloc.endswith(":443")
    ):
        netloc = netloc.rsplit(":", 1)[0]

    # Normalize path (strip trailing slash if not root)
    path = parsed.path
    if path != "/" and path.endswith("/"):
        path = path.rstrip("/")

    # Filter out tracking query parameters
    query_params = parse_qsl(parsed.query, keep_blank_values=True)
    filtered_params = [(k, v) for k, v in query_params if k.lower() not in TRACKING_PARAMS]
    filtered_params.sort(key=lambda x: x[0])
    new_query = urlencode(filtered_params)

    # Reconstruct URL without fragment (#)
    normalized = urlunparse((scheme, netloc, path, parsed.params, new_query, ""))
    return normalized


class DedupService:
    """Service to handle article URL normalization and database-backed deduplication."""

    def __init__(self, repository: ArticleRepository | None = None) -> None:
        """Initializes the DedupService with an ArticleRepository instance.

        Args:
            repository: Optional ArticleRepository instance. Defaults to new instance.
        """
        self.repo = repository or ArticleRepository()

    @trace_function(component="api.services.dedup")
    async def filter_new_articles(
        self,
        articles: list[ArticleIn],
        days_window: int = 7,
        collection: str = "default",
    ) -> list[ArticleIn]:
        """Filters a list of articles to return only those not seen within the days_window.

        Args:
            articles: List of incoming ArticleIn objects.
            days_window: Time window in days to consider an article seen. Defaults to 7.
            collection: Collection name to scope deduplication to.

        Returns:
            List of ArticleIn objects with normalized links that are new/unseen.
        """
        if not articles:
            return []

        # 1. Normalize URLs and deduplicate within the current batch
        batch_seen_urls: set[str] = set()
        normalized_articles: list[tuple[str, ArticleIn]] = []

        for article in articles:
            clean_url = normalize_url(article.link)
            if not clean_url or clean_url in batch_seen_urls:
                continue
            batch_seen_urls.add(clean_url)
            # Create a copy of the article with the normalized link
            clean_article = article.model_copy(update={"link": clean_url})
            normalized_articles.append((clean_url, clean_article))

        if not normalized_articles:
            return []

        all_urls = [url for url, _ in normalized_articles]

        # 2. Query repository for seen URLs within the retention window
        seen_in_db = await self.repo.get_seen_urls(
            all_urls, days_window=days_window, collection=collection
        )

        # 3. Filter out seen URLs
        unseen_pairs = [
            (url, article) for url, article in normalized_articles if url not in seen_in_db
        ]

        logger.info(
            "articles_deduplicated",
            received=len(articles),
            unique_in_batch=len(normalized_articles),
            already_in_db=len(seen_in_db),
            new_articles=len(unseen_pairs),
        )

        if not unseen_pairs:
            return []

        # 4. Save newly discovered articles to database
        articles_to_save = [
            {
                "url": url,
                "title": art.title,
                "content": art.content,
                "source": art.source,
            }
            for url, art in unseen_pairs
        ]
        await self.repo.save_articles(articles_to_save, collection=collection)

        return [art for _, art in unseen_pairs]
