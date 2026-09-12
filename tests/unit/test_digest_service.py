"""Unit tests for src/api/services/digest.py."""

from unittest.mock import patch

import pytest

from src.api.models.article import ArticleIn, DigestIn
from src.api.services.digest import DigestService


@pytest.mark.asyncio
async def test_digest_service_empty_articles() -> None:
    """Tests digest generation when an empty article list is provided."""
    service = DigestService()
    payload = DigestIn(articles=[])

    result = await service.generate_digest(payload)

    assert result.status == "ok"
    assert result.article_count == 0
    assert "No articles provided" in result.digest


@pytest.mark.asyncio
async def test_digest_service_with_articles() -> None:
    """Tests digest service processing valid articles list with LLM mock."""
    service = DigestService()
    articles = [
        ArticleIn(
            title="Tech News 1",
            link="https://example.com/1",
            content="Summary 1",
            source="Tech",
        ),
        ArticleIn(
            title="World News 2",
            link="https://example.com/2",
            content="Summary 2",
            source="World",
        ),
    ]
    payload = DigestIn(articles=articles)

    expected_markdown = "# Daily Digest\n\n## Main Stories\n- Story 1\n- Story 2"

    with patch(
        "src.api.services.digest.generate_completion", return_value=expected_markdown
    ) as mock_llm:
        result = await service.generate_digest(payload)

        assert result.status == "ok"
        assert result.article_count == 2
        assert result.digest == expected_markdown
        mock_llm.assert_called_once()


@pytest.mark.asyncio
async def test_digest_service_injects_podcast_behavior() -> None:
    """Digest service passes the podcast behavior into the system prompt."""
    service = DigestService()
    articles = [
        ArticleIn(
            title="Tech News 1",
            link="https://example.com/1",
            content="Summary 1",
            source="Tech",
        ),
    ]
    payload = DigestIn(articles=articles, podcast_behavior="eres un podcaster")

    with patch("src.api.services.digest.generate_completion", return_value="# Digest") as mock_llm:
        await service.generate_digest(payload)

    sent_messages = mock_llm.call_args.kwargs["messages"]
    system_content = sent_messages[0]["content"]
    assert "eres un podcaster" in system_content
    assert "[happy]" in system_content
