"""Fetch one web article and extract its readable body (Phase 7.10).

The page is fetched with ``httpx`` (bounded by timeout and response size) and
the readable text is extracted with ``trafilatura``, the reference library for
news-page boilerplate removal. Extraction runs in a worker thread because it is
CPU-bound and would otherwise stall the event loop.
"""

from __future__ import annotations

import asyncio
import re
from html import unescape
from urllib.parse import urlparse

import httpx
import structlog
import trafilatura

from src.api.models.article import ArticleIn
from src.core.config import settings
from src.core.tracing import trace_function

logger = structlog.get_logger()

USER_AGENT = "news-engine/0.5 (self-hosted news digest)"
# A page that yields less than this is a shell (login wall, JS-only app), not an
# article: summarizing it would produce an LLM hallucination from navigation text.
MIN_BODY_CHARS = 80
MAX_URL_LENGTH = 2048
_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)


class UrlExtractionError(Exception):
    """A permanent failure: the URL is unusable or has no readable article."""


def validate_url(url: str) -> str:
    """Validate a source URL before any network call.

    Args:
        url: Raw URL from the request.

    Returns:
        The trimmed URL.

    Raises:
        UrlExtractionError: When the URL is blank, too long or not http(s).
    """
    cleaned = (url or "").strip()
    if not cleaned:
        raise UrlExtractionError("url must not be empty")
    if len(cleaned) > MAX_URL_LENGTH:
        raise UrlExtractionError(f"url longer than {MAX_URL_LENGTH} characters")
    parsed = urlparse(cleaned)
    if parsed.scheme not in ("http", "https"):
        raise UrlExtractionError("url scheme must be http or https")
    if not parsed.netloc:
        raise UrlExtractionError("url must include a host")
    return cleaned


def _metadata_value(metadata: object, field: str) -> str:
    """Read a metadata field from a trafilatura Document, tolerating dicts.

    Args:
        metadata: trafilatura Document (2.x) or dict (1.x), or None.
        field: Attribute/key to read.

    Returns:
        The value as a stripped string, or '' when absent.
    """
    if metadata is None:
        return ""
    if isinstance(metadata, dict):
        raw = metadata.get(field)
    else:
        raw = getattr(metadata, field, None)
    return str(raw).strip() if raw is not None else ""


def _title_from_html(html: str) -> str:
    """Return the <title> text of a document, or '' when there is none.

    Args:
        html: Raw HTML of the page.

    Returns:
        The trimmed, unescaped title text.
    """
    match = _TITLE_RE.search(html)
    if not match:
        return ""
    return unescape(re.sub(r"\s+", " ", match.group(1))).strip()


def extract_article(html: str, url: str) -> ArticleIn:
    """Turn a fetched page into a single summary-ready article.

    Runs the CPU-bound extraction; call it through ``asyncio.to_thread``.

    Args:
        html: Raw HTML of the article page.
        url: Canonical URL the page was fetched from.

    Returns:
        An ArticleIn with title, body, publication date and source.

    Raises:
        UrlExtractionError: When no readable body can be extracted.
    """
    body = trafilatura.extract(
        html,
        url=url,
        with_metadata=False,
        favor_precision=True,
        include_comments=False,
        output_format="txt",
    )
    if not body or len(body.strip()) < MIN_BODY_CHARS:
        body = trafilatura.extract(
            html,
            url=url,
            with_metadata=False,
            favor_recall=True,
            include_comments=False,
            output_format="txt",
        )
    if not body or len(body.strip()) < MIN_BODY_CHARS:
        raise UrlExtractionError("no readable article body found in the page")

    metadata = trafilatura.extract_metadata(html, default_url=url)
    title = _metadata_value(metadata, "title") or _title_from_html(html)
    if not title:
        title = urlparse(url).path.rsplit("/", 1)[-1] or urlparse(url).netloc
    source = _metadata_value(metadata, "sitename") or urlparse(url).netloc
    pub_date = _metadata_value(metadata, "date") or None
    content = body.strip()
    max_chars = settings.url_summary_max_chars
    if len(content) > max_chars:
        content = f"{content[:max_chars]}\n\n[...] texto truncado a {max_chars} caracteres"
    return ArticleIn(
        title=title,
        link=url,
        content=content,
        pub_date=pub_date,
        source=source,
    )


class UrlArticleService:
    """Download a URL and extract the article the digest will summarize."""

    async def fetch_html(self, url: str) -> str:
        """Download the page with bounded size and timeout.

        Args:
            url: Already validated http(s) URL.

        Returns:
            The decoded HTML document.

        Raises:
            UrlExtractionError: On a 4xx answer, an oversized body or an empty
                response (permanent failures).
            httpx.HTTPError: On timeouts, connection errors and 5xx answers,
                which Temporal retries.
        """
        async with httpx.AsyncClient(
            timeout=settings.url_fetch_timeout,
            follow_redirects=True,
            headers={"User-Agent": USER_AGENT},
        ) as client:
            response = await client.get(url)
        if response.status_code >= 400:
            if response.status_code < 500:
                raise UrlExtractionError(f"article server answered HTTP {response.status_code}")
            response.raise_for_status()
        declared = response.headers.get("content-length", "")
        if declared.isdigit() and int(declared) > settings.url_fetch_max_bytes:
            raise UrlExtractionError("article page larger than url_fetch_max_bytes")
        if len(response.content) > settings.url_fetch_max_bytes:
            raise UrlExtractionError("article page larger than url_fetch_max_bytes")
        if not response.content:
            raise UrlExtractionError("article server answered an empty body")
        return response.text

    @trace_function(component="api.services.url_article")
    async def extract_from_url(self, url: str) -> ArticleIn:
        """Fetch and extract one article, off the event loop.

        Args:
            url: The article URL to summarize.

        Returns:
            The extracted article ready for DigestService.

        Raises:
            UrlExtractionError: When the URL is invalid, the server refuses it,
                the page is too large or no body can be extracted.
            httpx.HTTPError: On retryable transport failures.
        """
        cleaned = validate_url(url)
        html = await self.fetch_html(cleaned)
        article = await asyncio.to_thread(extract_article, html, cleaned)
        logger.info(
            "url_article_extracted",
            url=cleaned,
            title=article.title[:80],
            chars=len(article.content or ""),
        )
        return article
