"""Unit tests for deduplication and URL normalization logic."""

from unittest.mock import AsyncMock

import pytest

from src.api.models.article import ArticleIn
from src.api.services.dedup import DedupService, normalize_url


def test_normalize_url_removes_tracking_params() -> None:
    """Tests that marketing and tracking parameters are stripped from URLs."""
    raw = "https://example.com/article?utm_source=twitter&utm_medium=social&utm_campaign=launch"
    expected = "https://example.com/article"
    assert normalize_url(raw) == expected


def test_normalize_url_removes_fragments_and_trailing_slashes() -> None:
    """Tests that fragments and trailing slashes are removed."""
    raw = "HTTPS://EXAMPLE.COM/news/item/#section-1"
    expected = "https://example.com/news/item"
    assert normalize_url(raw) == expected


def test_normalize_url_preserves_legitimate_query_params() -> None:
    """Tests that non-tracking query parameters are preserved and sorted."""
    raw = "https://example.com/search?q=ai&category=tech&fbclid=12345"
    expected = "https://example.com/search?category=tech&q=ai"
    assert normalize_url(raw) == expected


def test_normalize_url_empty_input() -> None:
    """Tests normalization with empty or whitespace URL."""
    assert normalize_url("") == ""
    assert normalize_url("   ") == ""


@pytest.mark.asyncio
async def test_dedup_service_empty_input() -> None:
    """Tests DedupService with empty article list."""
    service = DedupService()
    result = await service.filter_new_articles([])
    assert result == []


@pytest.mark.asyncio
async def test_dedup_service_in_batch_duplicates() -> None:
    """Tests that duplicate articles within the same batch are deduplicated."""
    mock_repo = AsyncMock()
    mock_repo.get_seen_urls.return_value = set()
    mock_repo.save_articles.return_value = None

    service = DedupService(repository=mock_repo)
    articles = [
        ArticleIn(
            title="Article A",
            link="https://example.com/news/1?utm_source=feed",
        ),
        ArticleIn(
            title="Article A Duplicate",
            link="https://example.com/news/1?utm_medium=email",
        ),
    ]

    new_articles = await service.filter_new_articles(articles)

    assert len(new_articles) == 1
    assert new_articles[0].link == "https://example.com/news/1"
    mock_repo.save_articles.assert_called_once()


@pytest.mark.asyncio
async def test_dedup_service_filters_already_seen_urls() -> None:
    """Tests that URLs returned by repository as seen are filtered out."""
    mock_repo = AsyncMock()
    mock_repo.get_seen_urls.return_value = {"https://example.com/news/seen"}
    mock_repo.save_articles.return_value = None

    service = DedupService(repository=mock_repo)
    articles = [
        ArticleIn(title="Seen Article", link="https://example.com/news/seen"),
        ArticleIn(title="New Article", link="https://example.com/news/new"),
    ]

    new_articles = await service.filter_new_articles(articles, days_window=7)

    assert len(new_articles) == 1
    assert new_articles[0].link == "https://example.com/news/new"
    mock_repo.save_articles.assert_called_once()
